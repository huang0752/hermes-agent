#!/usr/bin/env python3
"""
SQLite State Store for Hermes Agent.

Provides persistent session storage with FTS5 full-text search, replacing
the per-session JSONL file approach. Stores session metadata, full message
history, and model configuration for CLI and gateway sessions.

Key design decisions:
- WAL mode for concurrent readers + one writer (gateway multi-platform)
- FTS5 virtual table for fast text search across all session messages
- Compression-triggered session splitting via parent_session_id chains
- Batch runner and RL trajectories are NOT stored here (separate systems)
- Session source tagging ('cli', 'telegram', 'discord', etc.) for filtering
"""

import json
import logging
import random
import re
import sqlite3
import threading
import time
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Any, Callable, Dict, List, Optional, TypeVar
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_DB_PATH = get_hermes_home() / "state.db"

SCHEMA_VERSION = 10

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    platform TEXT,
    chat_id TEXT,
    chat_type TEXT,
    thread_id TEXT,
    session_key TEXT,
    shared_session INTEGER DEFAULT 0,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);

CREATE TABLE IF NOT EXISTS room_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    message_key TEXT NOT NULL,
    message_id TEXT,
    appinfo TEXT,
    refer_id TEXT,
    seq TEXT,
    sender_id TEXT,
    sender_name TEXT,
    direction TEXT NOT NULL,
    message_type INTEGER DEFAULT 0,
    text_preview TEXT,
    raw_payload TEXT,
    created_at REAL NOT NULL,
    triggered INTEGER NOT NULL DEFAULT 0,
    consumed_for_context INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_room_messages_key
ON room_messages(platform, conversation_id, message_key);

CREATE INDEX IF NOT EXISTS idx_room_messages_conversation_created
ON room_messages(platform, conversation_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_room_messages_unconsumed
ON room_messages(platform, conversation_id, direction, consumed_for_context, triggered, id DESC);

CREATE TABLE IF NOT EXISTS juhe_attachment_parse_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    identity_kind TEXT NOT NULL,
    identity_value TEXT NOT NULL,
    bucket TEXT,
    object_key TEXT,
    object_url TEXT,
    file_id TEXT,
    file_md5 TEXT,
    media_type TEXT,
    file_name TEXT,
    parser TEXT,
    extracted_text TEXT NOT NULL DEFAULT '',
    content_kind TEXT,
    display_description TEXT,
    parse_status TEXT,
    error_message TEXT,
    content_format TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_juhe_attachment_parse_cache_identity
ON juhe_attachment_parse_cache(platform, identity_kind, identity_value);

CREATE INDEX IF NOT EXISTS idx_juhe_attachment_parse_cache_created
ON juhe_attachment_parse_cache(platform, created_at DESC);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content=messages,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

ROOM_MESSAGES_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS room_messages_fts USING fts5(
    searchable_text,
    platform UNINDEXED,
    conversation_id UNINDEXED,
    sender_name UNINDEXED,
    text_preview UNINDEXED,
    direction UNINDEXED,
    message_type UNINDEXED,
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS room_messages_fts_insert AFTER INSERT ON room_messages BEGIN
    INSERT INTO room_messages_fts(
        rowid,
        searchable_text,
        platform,
        conversation_id,
        sender_name,
        text_preview,
        direction,
        message_type
    )
    VALUES (
        new.id,
        trim(
            coalesce(new.sender_name, '') || ' ' ||
            coalesce(new.text_preview, '') || ' ' ||
            CASE
                WHEN lower(coalesce(new.direction, '')) = 'outbound' THEN 'outbound sent assistant'
                ELSE 'inbound received user'
            END || ' ' ||
            CASE
                WHEN CAST(COALESCE(new.message_type, 0) AS INTEGER) = 2 THEN 'text message'
                WHEN CAST(COALESCE(new.message_type, 0) AS INTEGER) = 8 THEN 'file document attachment'
                ELSE 'message'
            END
        ),
        coalesce(new.platform, ''),
        coalesce(new.conversation_id, ''),
        coalesce(new.sender_name, ''),
        coalesce(new.text_preview, ''),
        coalesce(new.direction, ''),
        CAST(COALESCE(new.message_type, 0) AS TEXT)
    );
END;

CREATE TRIGGER IF NOT EXISTS room_messages_fts_delete AFTER DELETE ON room_messages BEGIN
    DELETE FROM room_messages_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS room_messages_fts_update AFTER UPDATE ON room_messages BEGIN
    DELETE FROM room_messages_fts WHERE rowid = old.id;
    INSERT INTO room_messages_fts(
        rowid,
        searchable_text,
        platform,
        conversation_id,
        sender_name,
        text_preview,
        direction,
        message_type
    )
    VALUES (
        new.id,
        trim(
            coalesce(new.sender_name, '') || ' ' ||
            coalesce(new.text_preview, '') || ' ' ||
            CASE
                WHEN lower(coalesce(new.direction, '')) = 'outbound' THEN 'outbound sent assistant'
                ELSE 'inbound received user'
            END || ' ' ||
            CASE
                WHEN CAST(COALESCE(new.message_type, 0) AS INTEGER) = 2 THEN 'text message'
                WHEN CAST(COALESCE(new.message_type, 0) AS INTEGER) = 8 THEN 'file document attachment'
                ELSE 'message'
            END
        ),
        coalesce(new.platform, ''),
        coalesce(new.conversation_id, ''),
        coalesce(new.sender_name, ''),
        coalesce(new.text_preview, ''),
        coalesce(new.direction, ''),
        CAST(COALESCE(new.message_type, 0) AS TEXT)
    );
END;
"""

ROOM_MESSAGES_FTS_REBUILD_SQL = """
DELETE FROM room_messages_fts;
INSERT INTO room_messages_fts(
    rowid,
    searchable_text,
    platform,
    conversation_id,
    sender_name,
    text_preview,
    direction,
    message_type
)
SELECT
    id,
    trim(
        coalesce(sender_name, '') || ' ' ||
        coalesce(text_preview, '') || ' ' ||
        CASE
            WHEN lower(coalesce(direction, '')) = 'outbound' THEN 'outbound sent assistant'
            ELSE 'inbound received user'
        END || ' ' ||
        CASE
            WHEN CAST(COALESCE(message_type, 0) AS INTEGER) = 2 THEN 'text message'
            WHEN CAST(COALESCE(message_type, 0) AS INTEGER) = 8 THEN 'file document attachment'
            ELSE 'message'
        END
    ),
    coalesce(platform, ''),
    coalesce(conversation_id, ''),
    coalesce(sender_name, ''),
    coalesce(text_preview, ''),
    coalesce(direction, ''),
    CAST(COALESCE(message_type, 0) AS TEXT)
FROM room_messages;
"""


class SessionDB:
    """
    SQLite-backed session storage with FTS5 search.

    Thread-safe for the common gateway pattern (multiple reader threads,
    single writer via WAL mode). Each method opens its own cursor.
    """

    # ── Write-contention tuning ──
    # With multiple hermes processes (gateway + CLI sessions + worktree agents)
    # all sharing one state.db, WAL write-lock contention causes visible TUI
    # freezes.  SQLite's built-in busy handler uses a deterministic sleep
    # schedule that causes convoy effects under high concurrency.
    #
    # Instead, we keep the SQLite timeout short (1s) and handle retries at the
    # application level with random jitter, which naturally staggers competing
    # writers and avoids the convoy.
    _WRITE_MAX_RETRIES = 15
    _WRITE_RETRY_MIN_S = 0.020   # 20ms
    _WRITE_RETRY_MAX_S = 0.150   # 150ms
    # Attempt a PASSIVE WAL checkpoint every N successful writes.
    _CHECKPOINT_EVERY_N_WRITES = 50
    _ROOM_HISTORY_TOKEN_RE = re.compile(r"[A-Za-z0-9_./-]{2,}|[\u4e00-\u9fff]{2,}")
    _ROOM_HISTORY_FILLER_PATTERNS = (
        r"@[\w\-\u4e00-\u9fff]+",
        r"有没有说过",
        r"有和你说过",
        r"跟你说过",
        r"是否聊过",
        r"有没有提过",
        r"还记得",
        r"记得",
        r"说过",
        r"聊过",
        r"提过",
        r"之前",
        r"以前",
        r"上次",
        r"刚才",
        r"是否",
        r"重新",
        r"那个",
        r"这个",
        r"那次",
        r"这次",
        r"的事[吗么呢啊呀]?",
        r"事情",
        r"这件事",
    )
    _ROOM_HISTORY_STOP_TOKENS = {
        "一下",
        "一下子",
        "一下哈",
        "请问",
        "麻烦",
        "帮忙",
        "看看",
        "确认",
        "一下吧",
        "什么",
        "怎么",
        "可以",
        "需要",
        "处理",
        "问题",
    }

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._write_count = 0
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            # Short timeout — application-level retry with random jitter
            # handles contention instead of sitting in SQLite's internal
            # busy handler for up to 30s.
            timeout=1.0,
            # Autocommit mode: Python's default isolation_level="" auto-starts
            # transactions on DML, which conflicts with our explicit
            # BEGIN IMMEDIATE.  None = we manage transactions ourselves.
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

        self._init_schema()

    # ── Core write helper ──

    def _execute_write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Execute a write transaction with BEGIN IMMEDIATE and jitter retry.

        *fn* receives the connection and should perform INSERT/UPDATE/DELETE
        statements.  The caller must NOT call ``commit()`` — that's handled
        here after *fn* returns.

        BEGIN IMMEDIATE acquires the WAL write lock at transaction start
        (not at commit time), so lock contention surfaces immediately.
        On ``database is locked``, we release the Python lock, sleep a
        random 20-150ms, and retry — breaking the convoy pattern that
        SQLite's built-in deterministic backoff creates.

        Returns whatever *fn* returns.
        """
        last_err: Optional[Exception] = None
        for attempt in range(self._WRITE_MAX_RETRIES):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        raise
                # Success — periodic best-effort checkpoint.
                self._write_count += 1
                if self._write_count % self._CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_wal_checkpoint()
                return result
            except sqlite3.OperationalError as exc:
                err_msg = str(exc).lower()
                if "locked" in err_msg or "busy" in err_msg:
                    last_err = exc
                    if attempt < self._WRITE_MAX_RETRIES - 1:
                        jitter = random.uniform(
                            self._WRITE_RETRY_MIN_S,
                            self._WRITE_RETRY_MAX_S,
                        )
                        time.sleep(jitter)
                        continue
                # Non-lock error or retries exhausted — propagate.
                raise
        # Retries exhausted (shouldn't normally reach here).
        raise last_err or sqlite3.OperationalError(
            "database is locked after max retries"
        )

    def _try_wal_checkpoint(self) -> None:
        """Best-effort PASSIVE WAL checkpoint.  Never blocks, never raises.

        Flushes committed WAL frames back into the main DB file for any
        frames that no other connection currently needs.  Keeps the WAL
        from growing unbounded when many processes hold persistent
        connections.
        """
        try:
            with self._lock:
                result = self._conn.execute(
                    "PRAGMA wal_checkpoint(PASSIVE)"
                ).fetchone()
                if result and result[1] > 0:
                    logger.debug(
                        "WAL checkpoint: %d/%d pages checkpointed",
                        result[2], result[1],
                    )
        except Exception:
            pass  # Best effort — never fatal.

    def close(self):
        """Close the database connection.

        Attempts a PASSIVE WAL checkpoint first so that exiting processes
        help keep the WAL file from growing unbounded.
        """
        with self._lock:
            if self._conn:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except Exception:
                    pass
                self._conn.close()
                self._conn = None

    def _init_schema(self):
        """Create tables and FTS if they don't exist, run migrations."""
        cursor = self._conn.cursor()
        rebuild_room_messages_fts = False

        cursor.executescript(SCHEMA_SQL)

        # Check schema version and run migrations
        cursor.execute("SELECT version FROM schema_version LIMIT 1")
        row = cursor.fetchone()
        if row is None:
            cursor.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        else:
            current_version = row["version"] if isinstance(row, sqlite3.Row) else row[0]
            if current_version < 2:
                # v2: add finish_reason column to messages
                try:
                    cursor.execute("ALTER TABLE messages ADD COLUMN finish_reason TEXT")
                except sqlite3.OperationalError:
                    pass  # Column already exists
                cursor.execute("UPDATE schema_version SET version = 2")
            if current_version < 3:
                # v3: add title column to sessions
                try:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
                except sqlite3.OperationalError:
                    pass  # Column already exists
                cursor.execute("UPDATE schema_version SET version = 3")
            if current_version < 4:
                # v4: add unique index on title (NULLs allowed, only non-NULL must be unique)
                try:
                    cursor.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique "
                        "ON sessions(title) WHERE title IS NOT NULL"
                    )
                except sqlite3.OperationalError:
                    pass  # Index already exists
                cursor.execute("UPDATE schema_version SET version = 4")
            if current_version < 5:
                new_columns = [
                    ("cache_read_tokens", "INTEGER DEFAULT 0"),
                    ("cache_write_tokens", "INTEGER DEFAULT 0"),
                    ("reasoning_tokens", "INTEGER DEFAULT 0"),
                    ("billing_provider", "TEXT"),
                    ("billing_base_url", "TEXT"),
                    ("billing_mode", "TEXT"),
                    ("estimated_cost_usd", "REAL"),
                    ("actual_cost_usd", "REAL"),
                    ("cost_status", "TEXT"),
                    ("cost_source", "TEXT"),
                    ("pricing_version", "TEXT"),
                ]
                for name, column_type in new_columns:
                    try:
                        # name and column_type come from the hardcoded tuple above,
                        # not user input. Double-quote identifier escaping is applied
                        # as defense-in-depth; SQLite DDL cannot be parameterized.
                        safe_name = name.replace('"', '""')
                        cursor.execute(f'ALTER TABLE sessions ADD COLUMN "{safe_name}" {column_type}')
                    except sqlite3.OperationalError:
                        pass
                cursor.execute("UPDATE schema_version SET version = 5")
            if current_version < 6:
                # v6: add reasoning columns to messages table — preserves assistant
                # reasoning text and structured reasoning_details across gateway
                # session turns.  Without these, reasoning chains are lost on
                # session reload, breaking multi-turn reasoning continuity for
                # providers that replay reasoning (OpenRouter, OpenAI, Nous).
                for col_name, col_type in [
                    ("reasoning", "TEXT"),
                    ("reasoning_details", "TEXT"),
                    ("codex_reasoning_items", "TEXT"),
                ]:
                    try:
                        safe = col_name.replace('"', '""')
                        cursor.execute(
                            f'ALTER TABLE messages ADD COLUMN "{safe}" {col_type}'
                        )
                    except sqlite3.OperationalError:
                        pass  # Column already exists
                cursor.execute("UPDATE schema_version SET version = 6")
            if current_version < 7:
                cursor.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS room_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        platform TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        message_key TEXT NOT NULL,
                        message_id TEXT,
                        appinfo TEXT,
                        refer_id TEXT,
                        seq TEXT,
                        sender_id TEXT,
                        sender_name TEXT,
                        direction TEXT NOT NULL,
                        message_type INTEGER DEFAULT 0,
                        text_preview TEXT,
                        raw_payload TEXT,
                        created_at REAL NOT NULL,
                        triggered INTEGER NOT NULL DEFAULT 0,
                        consumed_for_context INTEGER NOT NULL DEFAULT 0
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS idx_room_messages_key
                    ON room_messages(platform, conversation_id, message_key);

                    CREATE INDEX IF NOT EXISTS idx_room_messages_conversation_created
                    ON room_messages(platform, conversation_id, created_at DESC, id DESC);

                    CREATE INDEX IF NOT EXISTS idx_room_messages_unconsumed
                    ON room_messages(platform, conversation_id, direction, consumed_for_context, triggered, id DESC);
                    """
                )
                cursor.execute("UPDATE schema_version SET version = 7")
            if current_version < 8:
                cursor.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS juhe_attachment_parse_cache (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        platform TEXT NOT NULL,
                        identity_kind TEXT NOT NULL,
                        identity_value TEXT NOT NULL,
                        bucket TEXT,
                        object_key TEXT,
                        object_url TEXT,
                        file_id TEXT,
                        file_md5 TEXT,
                        media_type TEXT,
                        file_name TEXT,
                        parser TEXT,
                        extracted_text TEXT NOT NULL DEFAULT '',
                        content_kind TEXT,
                        display_description TEXT,
                        parse_status TEXT,
                        error_message TEXT,
                        content_format TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS idx_juhe_attachment_parse_cache_identity
                    ON juhe_attachment_parse_cache(platform, identity_kind, identity_value);

                    CREATE INDEX IF NOT EXISTS idx_juhe_attachment_parse_cache_created
                    ON juhe_attachment_parse_cache(platform, created_at DESC);
                    """
                )
                cursor.execute("UPDATE schema_version SET version = 8")
            if current_version < 9:
                for col_name, col_type in [
                    ("content_kind", "TEXT"),
                    ("display_description", "TEXT"),
                    ("parse_status", "TEXT"),
                    ("error_message", "TEXT"),
                    ("content_format", "TEXT"),
                ]:
                    try:
                        safe = col_name.replace('"', '""')
                        cursor.execute(
                            f'ALTER TABLE juhe_attachment_parse_cache ADD COLUMN "{safe}" {col_type}'
                        )
                    except sqlite3.OperationalError:
                        pass
                cursor.execute(
                    """
                    UPDATE juhe_attachment_parse_cache
                    SET parse_status = COALESCE(parse_status, 'success'),
                        content_kind = COALESCE(
                            content_kind,
                            CASE
                                WHEN lower(COALESCE(media_type, '')) LIKE 'image/%' THEN 'image'
                                ELSE 'document'
                            END
                        ),
                        content_format = COALESCE(
                            content_format,
                            CASE
                                WHEN lower(COALESCE(media_type, '')) LIKE 'image/%' THEN 'vision'
                                ELSE 'text'
                            END
                        )
                    """
                )
                cursor.execute("UPDATE schema_version SET version = 9")
            if current_version < 10:
                for col_name, col_type in [
                    ("platform", "TEXT"),
                    ("chat_id", "TEXT"),
                    ("chat_type", "TEXT"),
                    ("thread_id", "TEXT"),
                    ("session_key", "TEXT"),
                    ("shared_session", "INTEGER DEFAULT 0"),
                ]:
                    try:
                        safe = col_name.replace('"', '""')
                        cursor.execute(
                            f'ALTER TABLE sessions ADD COLUMN "{safe}" {col_type}'
                        )
                    except sqlite3.OperationalError:
                        pass
                try:
                    cursor.execute(
                        "CREATE INDEX IF NOT EXISTS idx_sessions_platform_chat "
                        "ON sessions(platform, chat_id, chat_type, thread_id, started_at DESC)"
                    )
                except sqlite3.OperationalError:
                    pass
                try:
                    cursor.execute(
                        "CREATE INDEX IF NOT EXISTS idx_sessions_session_key "
                        "ON sessions(session_key)"
                    )
                except sqlite3.OperationalError:
                    pass
                cursor.execute("UPDATE schema_version SET version = 10")
                rebuild_room_messages_fts = True

        # Unique title index — always ensure it exists (safe to run after migrations
        # since the title column is guaranteed to exist at this point)
        try:
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique "
                "ON sessions(title) WHERE title IS NOT NULL"
            )
        except sqlite3.OperationalError:
            pass  # Index already exists
        for index_sql in (
            "CREATE INDEX IF NOT EXISTS idx_sessions_platform_chat "
            "ON sessions(platform, chat_id, chat_type, thread_id, started_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_session_key ON sessions(session_key)",
        ):
            try:
                cursor.execute(index_sql)
            except sqlite3.OperationalError:
                pass

        # FTS5 setup (separate because CREATE VIRTUAL TABLE can't be in executescript with IF NOT EXISTS reliably)
        try:
            cursor.execute("SELECT * FROM messages_fts LIMIT 0")
        except sqlite3.OperationalError:
            cursor.executescript(FTS_SQL)

        try:
            cursor.execute("SELECT * FROM room_messages_fts LIMIT 0")
        except sqlite3.OperationalError:
            cursor.executescript(ROOM_MESSAGES_FTS_SQL)
            rebuild_room_messages_fts = True

        if rebuild_room_messages_fts:
            cursor.executescript(ROOM_MESSAGES_FTS_REBUILD_SQL)

        self._conn.commit()

    # =========================================================================
    # Session lifecycle
    # =========================================================================

    def create_session(
        self,
        session_id: str,
        source: str,
        model: str = None,
        model_config: Dict[str, Any] = None,
        system_prompt: str = None,
        user_id: str = None,
        parent_session_id: str = None,
        platform: str = None,
        chat_id: str = None,
        chat_type: str = None,
        thread_id: str = None,
        session_key: str = None,
        shared_session: bool = False,
    ) -> str:
        """Create a new session record. Returns the session_id."""
        def _do(conn):
            conn.execute(
                """INSERT OR IGNORE INTO sessions (
                   id, source, user_id, platform, chat_id, chat_type, thread_id,
                   session_key, shared_session, model, model_config, system_prompt,
                   parent_session_id, started_at
                )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    source,
                    user_id,
                    platform,
                    chat_id,
                    chat_type,
                    thread_id,
                    session_key,
                    int(bool(shared_session)),
                    model,
                    json.dumps(model_config) if model_config else None,
                    system_prompt,
                    parent_session_id,
                    time.time(),
                ),
            )
        self._execute_write(_do)
        return session_id

    def end_session(self, session_id: str, end_reason: str) -> None:
        """Mark a session as ended."""
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
                (time.time(), end_reason, session_id),
            )
        self._execute_write(_do)

    def reopen_session(self, session_id: str) -> None:
        """Clear ended_at/end_reason so a session can be resumed."""
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?",
                (session_id,),
            )
        self._execute_write(_do)

    def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
        """Store the full assembled system prompt snapshot."""
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET system_prompt = ? WHERE id = ?",
                (system_prompt, session_id),
            )
        self._execute_write(_do)

    def update_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: Optional[float] = None,
        actual_cost_usd: Optional[float] = None,
        cost_status: Optional[str] = None,
        cost_source: Optional[str] = None,
        pricing_version: Optional[str] = None,
        billing_provider: Optional[str] = None,
        billing_base_url: Optional[str] = None,
        billing_mode: Optional[str] = None,
        absolute: bool = False,
    ) -> None:
        """Update token counters and backfill model if not already set.

        When *absolute* is False (default), values are **incremented** — use
        this for per-API-call deltas (CLI path).

        When *absolute* is True, values are **set directly** — use this when
        the caller already holds cumulative totals (gateway path, where the
        cached agent accumulates across messages).
        """
        if absolute:
            sql = """UPDATE sessions SET
                   input_tokens = ?,
                   output_tokens = ?,
                   cache_read_tokens = ?,
                   cache_write_tokens = ?,
                   reasoning_tokens = ?,
                   estimated_cost_usd = COALESCE(?, 0),
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?)
                   WHERE id = ?"""
        else:
            sql = """UPDATE sessions SET
                   input_tokens = input_tokens + ?,
                   output_tokens = output_tokens + ?,
                   cache_read_tokens = cache_read_tokens + ?,
                   cache_write_tokens = cache_write_tokens + ?,
                   reasoning_tokens = reasoning_tokens + ?,
                   estimated_cost_usd = COALESCE(estimated_cost_usd, 0) + COALESCE(?, 0),
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE COALESCE(actual_cost_usd, 0) + ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?)
                   WHERE id = ?"""
        params = (
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
            reasoning_tokens,
            estimated_cost_usd,
            actual_cost_usd,
            actual_cost_usd,
            cost_status,
            cost_source,
            pricing_version,
            billing_provider,
            billing_base_url,
            billing_mode,
            model,
            session_id,
        )
        def _do(conn):
            conn.execute(sql, params)
        self._execute_write(_do)

    def ensure_session(
        self,
        session_id: str,
        source: str = "unknown",
        model: str = None,
    ) -> None:
        """Ensure a session row exists, creating it with minimal metadata if absent.

        Used by _flush_messages_to_session_db to recover from a failed
        create_session() call (e.g. transient SQLite lock at agent startup).
        INSERT OR IGNORE is safe to call even when the row already exists.
        """
        def _do(conn):
            conn.execute(
                """INSERT OR IGNORE INTO sessions
                   (id, source, model, started_at)
                   VALUES (?, ?, ?, ?)""",
                (session_id, source, model, time.time()),
            )
        self._execute_write(_do)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get a session by ID."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def resolve_session_id(self, session_id_or_prefix: str) -> Optional[str]:
        """Resolve an exact or uniquely prefixed session ID to the full ID.

        Returns the exact ID when it exists. Otherwise treats the input as a
        prefix and returns the single matching session ID if the prefix is
        unambiguous. Returns None for no matches or ambiguous prefixes.
        """
        exact = self.get_session(session_id_or_prefix)
        if exact:
            return exact["id"]

        escaped = (
            session_id_or_prefix
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\\' ORDER BY started_at DESC LIMIT 2",
                (f"{escaped}%",),
            )
            matches = [row["id"] for row in cursor.fetchall()]
        if len(matches) == 1:
            return matches[0]
        return None

    # Maximum length for session titles
    MAX_TITLE_LENGTH = 100

    @staticmethod
    def sanitize_title(title: Optional[str]) -> Optional[str]:
        """Validate and sanitize a session title.

        - Strips leading/trailing whitespace
        - Removes ASCII control characters (0x00-0x1F, 0x7F) and problematic
          Unicode control chars (zero-width, RTL/LTR overrides, etc.)
        - Collapses internal whitespace runs to single spaces
        - Normalizes empty/whitespace-only strings to None
        - Enforces MAX_TITLE_LENGTH

        Returns the cleaned title string or None.
        Raises ValueError if the title exceeds MAX_TITLE_LENGTH after cleaning.
        """
        if not title:
            return None

        # Remove ASCII control characters (0x00-0x1F, 0x7F) but keep
        # whitespace chars (\t=0x09, \n=0x0A, \r=0x0D) so they can be
        # normalized to spaces by the whitespace collapsing step below
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', title)

        # Remove problematic Unicode control characters:
        # - Zero-width chars (U+200B-U+200F, U+FEFF)
        # - Directional overrides (U+202A-U+202E, U+2066-U+2069)
        # - Object replacement (U+FFFC), interlinear annotation (U+FFF9-U+FFFB)
        cleaned = re.sub(
            r'[\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ufffc\ufff9-\ufffb]',
            '', cleaned,
        )

        # Collapse internal whitespace runs and strip
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()

        if not cleaned:
            return None

        if len(cleaned) > SessionDB.MAX_TITLE_LENGTH:
            raise ValueError(
                f"Title too long ({len(cleaned)} chars, max {SessionDB.MAX_TITLE_LENGTH})"
            )

        return cleaned

    def set_session_title(self, session_id: str, title: str) -> bool:
        """Set or update a session's title.

        Returns True if session was found and title was set.
        Raises ValueError if title is already in use by another session,
        or if the title fails validation (too long, invalid characters).
        Empty/whitespace-only strings are normalized to None (clearing the title).
        """
        title = self.sanitize_title(title)
        def _do(conn):
            if title:
                # Check uniqueness (allow the same session to keep its own title)
                cursor = conn.execute(
                    "SELECT id FROM sessions WHERE title = ? AND id != ?",
                    (title, session_id),
                )
                conflict = cursor.fetchone()
                if conflict:
                    raise ValueError(
                        f"Title '{title}' is already in use by session {conflict['id']}"
                    )
            cursor = conn.execute(
                "UPDATE sessions SET title = ? WHERE id = ?",
                (title, session_id),
            )
            return cursor.rowcount
        rowcount = self._execute_write(_do)
        return rowcount > 0

    def get_session_title(self, session_id: str) -> Optional[str]:
        """Get the title for a session, or None."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
        return row["title"] if row else None

    def get_session_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        """Look up a session by exact title. Returns session dict or None."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE title = ?", (title,)
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def resolve_session_by_title(self, title: str) -> Optional[str]:
        """Resolve a title to a session ID, preferring the latest in a lineage.

        If the exact title exists, returns that session's ID.
        If not, searches for "title #N" variants and returns the latest one.
        If the exact title exists AND numbered variants exist, returns the
        latest numbered variant (the most recent continuation).
        """
        # First try exact match
        exact = self.get_session_by_title(title)

        # Also search for numbered variants: "title #2", "title #3", etc.
        # Escape SQL LIKE wildcards (%, _) in the title to prevent false matches
        escaped = title.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id, title, started_at FROM sessions "
                "WHERE title LIKE ? ESCAPE '\\' ORDER BY started_at DESC",
                (f"{escaped} #%",),
            )
            numbered = cursor.fetchall()

        if numbered:
            # Return the most recent numbered variant
            return numbered[0]["id"]
        elif exact:
            return exact["id"]
        return None

    def get_next_title_in_lineage(self, base_title: str) -> str:
        """Generate the next title in a lineage (e.g., "my session" → "my session #2").

        Strips any existing " #N" suffix to find the base name, then finds
        the highest existing number and increments.
        """
        # Strip existing #N suffix to find the true base
        match = re.match(r'^(.*?) #(\d+)$', base_title)
        if match:
            base = match.group(1)
        else:
            base = base_title

        # Find all existing numbered variants
        # Escape SQL LIKE wildcards (%, _) in the base to prevent false matches
        escaped = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title FROM sessions WHERE title = ? OR title LIKE ? ESCAPE '\\'",
                (base, f"{escaped} #%"),
            )
            existing = [row["title"] for row in cursor.fetchall()]

        if not existing:
            return base  # No conflict, use the base name as-is

        # Find the highest number
        max_num = 1  # The unnumbered original counts as #1
        for t in existing:
            m = re.match(r'^.* #(\d+)$', t)
            if m:
                max_num = max(max_num, int(m.group(1)))

        return f"{base} #{max_num + 1}"

    def list_sessions_rich(
        self,
        source: str = None,
        exclude_sources: List[str] = None,
        limit: int = 20,
        offset: int = 0,
        include_children: bool = False,
    ) -> List[Dict[str, Any]]:
        """List sessions with preview (first user message) and last active timestamp.

        Returns dicts with keys: id, source, model, title, started_at, ended_at,
        message_count, preview (first 60 chars of first user message),
        last_active (timestamp of last message).

        Uses a single query with correlated subqueries instead of N+2 queries.

        By default, child sessions (subagent runs, compression continuations)
        are excluded.  Pass ``include_children=True`` to include them.
        """
        where_clauses = []
        params = []

        if not include_children:
            where_clauses.append("s.parent_session_id IS NULL")

        if source:
            where_clauses.append("s.source = ?")
            params.append(source)
        if exclude_sources:
            placeholders = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({placeholders})")
            params.extend(exclude_sources)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"""
            SELECT s.*,
                COALESCE(
                    (SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                     FROM messages m
                     WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                     ORDER BY m.timestamp, m.id LIMIT 1),
                    ''
                ) AS _preview_raw,
                COALESCE(
                    (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                    s.started_at
                ) AS last_active
            FROM sessions s
            {where_sql}
            ORDER BY s.started_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        with self._lock:
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()
        sessions = []
        for row in rows:
            s = dict(row)
            # Build the preview from the raw substring
            raw = s.pop("_preview_raw", "").strip()
            if raw:
                text = raw[:60]
                s["preview"] = text + ("..." if len(raw) > 60 else "")
            else:
                s["preview"] = ""
            sessions.append(s)

        return sessions

    # =========================================================================
    # Message storage
    # =========================================================================

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str = None,
        tool_name: str = None,
        tool_calls: Any = None,
        tool_call_id: str = None,
        token_count: int = None,
        finish_reason: str = None,
        reasoning: str = None,
        reasoning_details: Any = None,
        codex_reasoning_items: Any = None,
    ) -> int:
        """
        Append a message to a session. Returns the message row ID.

        Also increments the session's message_count (and tool_call_count
        if role is 'tool' or tool_calls is present).
        """
        # Serialize structured fields to JSON before entering the write txn
        reasoning_details_json = (
            json.dumps(reasoning_details)
            if reasoning_details else None
        )
        codex_items_json = (
            json.dumps(codex_reasoning_items)
            if codex_reasoning_items else None
        )
        tool_calls_json = json.dumps(tool_calls) if tool_calls else None

        # Pre-compute tool call count
        num_tool_calls = 0
        if tool_calls is not None:
            num_tool_calls = len(tool_calls) if isinstance(tool_calls, list) else 1

        def _do(conn):
            cursor = conn.execute(
                """INSERT INTO messages (session_id, role, content, tool_call_id,
                   tool_calls, tool_name, timestamp, token_count, finish_reason,
                   reasoning, reasoning_details, codex_reasoning_items)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    role,
                    content,
                    tool_call_id,
                    tool_calls_json,
                    tool_name,
                    time.time(),
                    token_count,
                    finish_reason,
                    reasoning,
                    reasoning_details_json,
                    codex_items_json,
                ),
            )
            msg_id = cursor.lastrowid

            # Update counters
            if num_tool_calls > 0:
                conn.execute(
                    """UPDATE sessions SET message_count = message_count + 1,
                       tool_call_count = tool_call_count + ? WHERE id = ?""",
                    (num_tool_calls, session_id),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                    (session_id,),
                )
            return msg_id

        return self._execute_write(_do)

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Load all messages for a session, ordered by timestamp."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp, id",
                (session_id,),
            )
            rows = cursor.fetchall()
        result = []
        for row in rows:
            msg = dict(row)
            if msg.get("tool_calls"):
                try:
                    msg["tool_calls"] = json.loads(msg["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Failed to deserialize tool_calls in get_messages, falling back to []")
                    msg["tool_calls"] = []
            result.append(msg)
        return result

    def get_messages_as_conversation(self, session_id: str) -> List[Dict[str, Any]]:
        """
        Load messages in the OpenAI conversation format (role + content dicts).
        Used by the gateway to restore conversation history.
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT role, content, tool_call_id, tool_calls, tool_name, "
                "reasoning, reasoning_details, codex_reasoning_items "
                "FROM messages WHERE session_id = ? ORDER BY timestamp, id",
                (session_id,),
            )
            rows = cursor.fetchall()
        messages = []
        for row in rows:
            msg = {"role": row["role"], "content": row["content"]}
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["tool_name"]:
                msg["tool_name"] = row["tool_name"]
            if row["tool_calls"]:
                try:
                    msg["tool_calls"] = json.loads(row["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Failed to deserialize tool_calls in conversation replay, falling back to []")
                    msg["tool_calls"] = []
            # Restore reasoning fields on assistant messages so providers
            # that replay reasoning (OpenRouter, OpenAI, Nous) receive
            # coherent multi-turn reasoning context.
            if row["role"] == "assistant":
                if row["reasoning"]:
                    msg["reasoning"] = row["reasoning"]
                if row["reasoning_details"]:
                    try:
                        msg["reasoning_details"] = json.loads(row["reasoning_details"])
                    except (json.JSONDecodeError, TypeError):
                        logger.warning("Failed to deserialize reasoning_details, falling back to None")
                        msg["reasoning_details"] = None
                if row["codex_reasoning_items"]:
                    try:
                        msg["codex_reasoning_items"] = json.loads(row["codex_reasoning_items"])
                    except (json.JSONDecodeError, TypeError):
                        logger.warning("Failed to deserialize codex_reasoning_items, falling back to None")
                        msg["codex_reasoning_items"] = None
            messages.append(msg)
        return messages

    # =========================================================================
    # Room raw-message window storage
    # =========================================================================

    @staticmethod
    def _room_message_key(
        *,
        message_id: Optional[str],
        appinfo: Optional[str],
        seq: Optional[str],
        sender_id: Optional[str],
        created_at: float,
    ) -> str:
        if message_id:
            return f"message_id:{message_id}"
        if appinfo:
            return f"appinfo:{appinfo}"
        if seq:
            return f"seq:{seq}"
        sender = str(sender_id or "").strip() or "unknown"
        return f"fallback:{sender}:{int(created_at * 1000)}"

    @staticmethod
    def _deserialize_room_message(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        raw_payload = item.get("raw_payload")
        if raw_payload:
            try:
                item["raw_payload"] = json.loads(raw_payload)
            except (json.JSONDecodeError, TypeError):
                item["raw_payload"] = raw_payload
        else:
            item["raw_payload"] = None
        item["triggered"] = bool(item.get("triggered"))
        item["consumed_for_context"] = bool(item.get("consumed_for_context"))
        return item

    def append_room_message(
        self,
        *,
        platform: str,
        conversation_id: str,
        message_id: Optional[str] = None,
        appinfo: Optional[str] = None,
        refer_id: Optional[str] = None,
        seq: Optional[str] = None,
        sender_id: Optional[str] = None,
        sender_name: Optional[str] = None,
        direction: str,
        message_type: int = 0,
        text_preview: Optional[str] = None,
        raw_payload: Any = None,
        created_at: Optional[float] = None,
        triggered: bool = False,
        consumed_for_context: bool = False,
        room_log_limit: int = 500,
    ) -> int:
        created_ts = float(created_at if created_at is not None else time.time())
        normalized_platform = str(platform or "").strip()
        normalized_conversation_id = str(conversation_id or "").strip()
        normalized_message_id = str(message_id or "").strip() or None
        normalized_appinfo = str(appinfo or "").strip() or None
        normalized_seq = str(seq or "").strip() or None
        message_key = self._room_message_key(
            message_id=normalized_message_id,
            appinfo=normalized_appinfo,
            seq=normalized_seq,
            sender_id=sender_id,
            created_at=created_ts,
        )
        raw_payload_json = (
            json.dumps(raw_payload, ensure_ascii=False, default=str)
            if raw_payload is not None
            else None
        )

        def _do(conn):
            conn.execute(
                """
                INSERT INTO room_messages (
                    platform, conversation_id, message_key, message_id, appinfo,
                    refer_id, seq, sender_id, sender_name, direction, message_type,
                    text_preview, raw_payload, created_at, triggered, consumed_for_context
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, conversation_id, message_key) DO UPDATE SET
                    message_id = COALESCE(excluded.message_id, room_messages.message_id),
                    appinfo = COALESCE(excluded.appinfo, room_messages.appinfo),
                    refer_id = COALESCE(excluded.refer_id, room_messages.refer_id),
                    seq = COALESCE(excluded.seq, room_messages.seq),
                    sender_id = COALESCE(excluded.sender_id, room_messages.sender_id),
                    sender_name = COALESCE(excluded.sender_name, room_messages.sender_name),
                    direction = excluded.direction,
                    message_type = excluded.message_type,
                    text_preview = COALESCE(excluded.text_preview, room_messages.text_preview),
                    raw_payload = COALESCE(excluded.raw_payload, room_messages.raw_payload),
                    created_at = excluded.created_at,
                    triggered = MAX(room_messages.triggered, excluded.triggered),
                    consumed_for_context = MAX(room_messages.consumed_for_context, excluded.consumed_for_context)
                """,
                (
                    normalized_platform,
                    normalized_conversation_id,
                    message_key,
                    normalized_message_id,
                    normalized_appinfo,
                    str(refer_id or "").strip() or None,
                    normalized_seq,
                    str(sender_id or "").strip() or None,
                    str(sender_name or "").strip() or None,
                    str(direction or "").strip() or "inbound",
                    int(message_type or 0),
                    str(text_preview or "") or None,
                    raw_payload_json,
                    created_ts,
                    int(bool(triggered)),
                    int(bool(consumed_for_context)),
                ),
            )
            row = conn.execute(
                """
                SELECT id FROM room_messages
                WHERE platform = ? AND conversation_id = ? AND message_key = ?
                """,
                (normalized_platform, normalized_conversation_id, message_key),
            ).fetchone()
            limit = max(int(room_log_limit or 0), 0)
            if limit > 0:
                conn.execute(
                    """
                    DELETE FROM room_messages
                    WHERE platform = ? AND conversation_id = ?
                      AND id NOT IN (
                          SELECT id FROM room_messages
                          WHERE platform = ? AND conversation_id = ?
                          ORDER BY id DESC
                          LIMIT ?
                      )
                    """,
                    (
                        normalized_platform,
                        normalized_conversation_id,
                        normalized_platform,
                        normalized_conversation_id,
                        limit,
                    ),
                )
            return int(row["id"]) if row else 0

        return self._execute_write(_do)

    def get_room_history(
        self,
        platform: str,
        conversation_id: str,
        *,
        before_message_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        normalized_platform = str(platform or "").strip()
        normalized_conversation_id = str(conversation_id or "").strip()
        fetch_limit = max(int(limit or 0), 1)
        params: List[Any] = [normalized_platform, normalized_conversation_id]
        cursor_sql = ""
        if before_message_id:
            cursor_sql = (
                "AND id < COALESCE(("
                "SELECT id FROM room_messages "
                "WHERE platform = ? AND conversation_id = ? AND message_id = ? "
                "ORDER BY id DESC LIMIT 1"
                "), 9223372036854775807)"
            )
            params.extend([normalized_platform, normalized_conversation_id, str(before_message_id)])

        with self._lock:
            cursor = self._conn.execute(
                f"""
                SELECT * FROM room_messages
                WHERE platform = ? AND conversation_id = ?
                {cursor_sql}
                ORDER BY id DESC
                LIMIT ?
                """,
                params + [fetch_limit],
            )
            rows = cursor.fetchall()
        return [self._deserialize_room_message(row) for row in reversed(rows)]

    def get_unconsumed_room_messages(
        self,
        platform: str,
        conversation_id: str,
        *,
        limit: int,
        char_limit: int,
    ) -> List[Dict[str, Any]]:
        normalized_platform = str(platform or "").strip()
        normalized_conversation_id = str(conversation_id or "").strip()
        max_messages = max(int(limit or 0), 0)
        max_chars = max(int(char_limit or 0), 0)
        if max_messages <= 0 or max_chars <= 0:
            return []

        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT * FROM room_messages
                WHERE platform = ? AND conversation_id = ?
                  AND direction = 'inbound'
                  AND triggered = 0
                  AND consumed_for_context = 0
                ORDER BY id DESC
                LIMIT 200
                """,
                (normalized_platform, normalized_conversation_id),
            )
            rows = cursor.fetchall()

        selected: List[Dict[str, Any]] = []
        total_chars = 0
        for row in rows:
            item = self._deserialize_room_message(row)
            preview_len = len(str(item.get("text_preview") or ""))
            if selected and (
                len(selected) >= max_messages
                or total_chars + preview_len > max_chars
            ):
                break
            if preview_len > max_chars and not selected:
                break
            selected.append(item)
            total_chars += preview_len

        return list(reversed(selected))

    @classmethod
    def _normalize_room_history_phrase(cls, query: str) -> str:
        normalized = re.sub(r"\s+", " ", str(query or "")).strip()
        return normalized.strip('"')

    @classmethod
    def _extract_room_history_terms(cls, query: str) -> List[str]:
        text = str(query or "").strip()
        if not text:
            return []
        cleaned = re.sub(r"[，。！？、,:;()（）“”\"'`]+", " ", text)
        for pattern in cls._ROOM_HISTORY_FILLER_PATTERNS:
            cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"[你我他她它和有是都还再又把给跟呢吗么呀啊哦哇啦]", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        terms: List[str] = []
        seen: set[str] = set()
        for token in cls._ROOM_HISTORY_TOKEN_RE.findall(cleaned):
            normalized = token.strip().lower()
            if (
                not normalized
                or normalized in cls._ROOM_HISTORY_STOP_TOKENS
                or len(normalized) < 2
            ):
                continue
            if normalized not in seen:
                seen.add(normalized)
                terms.append(normalized)
        return terms

    def _search_room_messages_fts(
        self,
        *,
        platform: str,
        conversation_id: str,
        match_query: str,
        limit: int,
    ) -> List[Dict[str, Any]]:
        if not match_query:
            return []
        with self._lock:
            try:
                cursor = self._conn.execute(
                    """
                    SELECT
                        rm.id,
                        rm.message_id,
                        rm.created_at,
                        rm.sender_name,
                        rm.text_preview,
                        rm.triggered,
                        rm.direction
                    FROM room_messages_fts
                    JOIN room_messages rm ON rm.id = room_messages_fts.rowid
                    WHERE room_messages_fts MATCH ?
                      AND rm.platform = ?
                      AND rm.conversation_id = ?
                    ORDER BY bm25(room_messages_fts), rm.created_at DESC, rm.id DESC
                    LIMIT ?
                    """,
                    (
                        match_query,
                        str(platform or "").strip(),
                        str(conversation_id or "").strip(),
                        max(int(limit or 0), 1),
                    ),
                )
                return [dict(row) for row in cursor.fetchall()]
            except sqlite3.OperationalError:
                return []

    @classmethod
    def _rank_search_hits(
        cls,
        *,
        hits: List[Dict[str, Any]],
        terms: List[str],
        phase_rank: int,
        bucket: Dict[int, Dict[str, Any]],
    ) -> None:
        for row in hits:
            row_id = int(row.get("id") or 0)
            haystack = " ".join(
                [
                    str(row.get("sender_name") or ""),
                    str(row.get("text_preview") or ""),
                    str(row.get("content") or ""),
                    str(row.get("role") or ""),
                ]
            ).lower()
            token_hits = sum(haystack.count(term) for term in terms if term)
            existing = bucket.get(row_id)
            candidate = dict(row)
            candidate["_phase_rank"] = phase_rank
            candidate["_token_hits"] = token_hits
            candidate["_score"] = phase_rank * 10 + token_hits * 10
            if existing is None or (
                candidate["_score"],
                float(row.get("created_at") or 0.0),
                float(row.get("timestamp") or 0.0),
                row_id,
            ) > (
                existing["_score"],
                float(existing.get("created_at") or 0.0),
                float(existing.get("timestamp") or 0.0),
                int(existing.get("id") or 0),
            ):
                bucket[row_id] = candidate

    def search_room_history(
        self,
        platform: str,
        conversation_id: str,
        *,
        query: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        if not str(query or "").strip():
            return []

        fetch_limit = max(int(limit or 0), 1)
        normalized_platform = str(platform or "").strip()
        normalized_conversation_id = str(conversation_id or "").strip()
        terms = self._extract_room_history_terms(query)
        phrase = self._normalize_room_history_phrase(query)
        ranked: Dict[int, Dict[str, Any]] = {}

        if phrase:
            phrase_query = self._sanitize_fts5_query(f'"{phrase}"')
            if phrase_query:
                self._rank_search_hits(
                    hits=self._search_room_messages_fts(
                        platform=normalized_platform,
                        conversation_id=normalized_conversation_id,
                        match_query=phrase_query,
                        limit=max(fetch_limit * 3, 10),
                    ),
                    terms=terms,
                    phase_rank=3,
                    bucket=ranked,
                )

        if terms:
            keyword_query = " OR ".join(
                token for token in (self._sanitize_fts5_query(term) for term in terms) if token
            )
            if keyword_query:
                self._rank_search_hits(
                    hits=self._search_room_messages_fts(
                        platform=normalized_platform,
                        conversation_id=normalized_conversation_id,
                        match_query=keyword_query,
                        limit=max(fetch_limit * 5, 15),
                    ),
                    terms=terms,
                    phase_rank=2,
                    bucket=ranked,
                )

        if not ranked:
            recent_messages = self.get_room_history(
                normalized_platform,
                normalized_conversation_id,
                limit=500,
            )
            lowered_phrase = phrase.lower()
            for item in reversed(recent_messages):
                haystack = " ".join(
                    [
                        str(item.get("sender_name") or ""),
                        str(item.get("text_preview") or ""),
                    ]
                ).lower()
                if lowered_phrase and lowered_phrase in haystack:
                    fallback_hits = terms
                else:
                    fallback_hits = [term for term in terms if term in haystack]
                if not fallback_hits:
                    continue
                ranked[int(item["id"])] = {
                    "id": int(item["id"]),
                    "message_id": item.get("message_id"),
                    "created_at": item.get("created_at"),
                    "sender_name": item.get("sender_name"),
                    "text_preview": item.get("text_preview"),
                    "triggered": item.get("triggered"),
                    "direction": item.get("direction"),
                    "_phase_rank": 1,
                    "_token_hits": len(fallback_hits),
                    "_score": 10 + len(fallback_hits) * 10,
                }

        ordered = sorted(
            ranked.values(),
            key=lambda item: (
                int(item.get("_score") or 0),
                float(item.get("created_at") or 0.0),
                int(item.get("id") or 0),
            ),
            reverse=True,
        )[:fetch_limit]

        return [
            {
                "message_id": item.get("message_id"),
                "created_at": item.get("created_at"),
                "sender_name": item.get("sender_name"),
                "text_preview": item.get("text_preview"),
                "score": int(item.get("_score") or 0),
                "triggered": bool(item.get("triggered")),
                "direction": item.get("direction"),
            }
            for item in ordered
        ]

    def _search_prior_session_messages_fts(
        self,
        *,
        platform: str,
        chat_id: str,
        chat_type: str,
        thread_id: Optional[str],
        exclude_session_id: str,
        match_query: str,
        limit: int,
    ) -> List[Dict[str, Any]]:
        thread_value = str(thread_id or "").strip()
        with self._lock:
            try:
                cursor = self._conn.execute(
                    """
                    SELECT
                        m.id,
                        m.session_id,
                        m.role,
                        m.content,
                        m.timestamp,
                        s.started_at,
                        s.session_key
                    FROM messages_fts
                    JOIN messages m ON m.id = messages_fts.rowid
                    JOIN sessions s ON s.id = m.session_id
                    WHERE messages_fts MATCH ?
                      AND s.platform = ?
                      AND s.chat_id = ?
                      AND s.chat_type = ?
                      AND COALESCE(s.thread_id, '') = ?
                      AND s.id != ?
                      AND m.role IN ('user', 'assistant')
                    ORDER BY bm25(messages_fts), m.timestamp DESC, m.id DESC
                    LIMIT ?
                    """,
                    (
                        match_query,
                        str(platform or "").strip(),
                        str(chat_id or "").strip(),
                        str(chat_type or "").strip(),
                        thread_value,
                        str(exclude_session_id or "").strip(),
                        max(int(limit or 0), 1),
                    ),
                )
                return [dict(row) for row in cursor.fetchall()]
            except sqlite3.OperationalError:
                return []

    def search_prior_session_messages(
        self,
        *,
        platform: str,
        chat_id: str,
        chat_type: str,
        exclude_session_id: str,
        query: str,
        limit: int = 6,
        thread_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not str(query or "").strip():
            return []

        fetch_limit = max(int(limit or 0), 1)
        terms = self._extract_room_history_terms(query)
        phrase = self._normalize_room_history_phrase(query)
        ranked: Dict[int, Dict[str, Any]] = {}

        if phrase:
            phrase_query = self._sanitize_fts5_query(f'"{phrase}"')
            if phrase_query:
                self._rank_search_hits(
                    hits=self._search_prior_session_messages_fts(
                        platform=platform,
                        chat_id=chat_id,
                        chat_type=chat_type,
                        thread_id=thread_id,
                        exclude_session_id=exclude_session_id,
                        match_query=phrase_query,
                        limit=max(fetch_limit * 3, 10),
                    ),
                    terms=terms,
                    phase_rank=3,
                    bucket=ranked,
                )

        if terms:
            keyword_query = " OR ".join(
                token for token in (self._sanitize_fts5_query(term) for term in terms) if token
            )
            if keyword_query:
                self._rank_search_hits(
                    hits=self._search_prior_session_messages_fts(
                        platform=platform,
                        chat_id=chat_id,
                        chat_type=chat_type,
                        thread_id=thread_id,
                        exclude_session_id=exclude_session_id,
                        match_query=keyword_query,
                        limit=max(fetch_limit * 5, 15),
                    ),
                    terms=terms,
                    phase_rank=2,
                    bucket=ranked,
                )

        ordered = sorted(
            ranked.values(),
            key=lambda item: (
                int(item.get("_score") or 0),
                float(item.get("timestamp") or 0.0),
                int(item.get("id") or 0),
            ),
            reverse=True,
        )[:fetch_limit]

        return [
            {
                "session_id": item.get("session_id"),
                "role": item.get("role"),
                "content": item.get("content"),
                "timestamp": item.get("timestamp"),
                "started_at": item.get("started_at"),
                "session_key": item.get("session_key"),
                "score": int(item.get("_score") or 0),
            }
            for item in ordered
        ]

    def mark_room_messages_consumed(
        self,
        platform: str,
        conversation_id: str,
        row_ids: List[int],
    ) -> None:
        normalized_ids = [int(row_id) for row_id in row_ids if row_id]
        if not normalized_ids:
            return

        placeholders = ", ".join("?" for _ in normalized_ids)

        def _do(conn):
            conn.execute(
                f"""
                UPDATE room_messages
                SET consumed_for_context = 1
                WHERE platform = ? AND conversation_id = ? AND id IN ({placeholders})
                """,
                [str(platform or "").strip(), str(conversation_id or "").strip(), *normalized_ids],
            )

        self._execute_write(_do)

    # =========================================================================
    # Juhe attachment parse cache
    # =========================================================================

    @staticmethod
    def _normalize_juhe_attachment_object_url(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = urlsplit(text)
        except ValueError:
            return text.split("?", 1)[0].split("#", 1)[0].strip()
        if not parsed.scheme or not parsed.netloc:
            return text.split("?", 1)[0].split("#", 1)[0].strip()
        return urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path,
                "",
                "",
            )
        )

    @staticmethod
    def _juhe_attachment_cache_candidates(
        *,
        bucket: Any = None,
        object_key: Any = None,
        object_url: Any = None,
        file_id: Any = None,
        file_md5: Any = None,
    ) -> List[tuple[str, str]]:
        candidates: List[tuple[str, str]] = []
        normalized_bucket = str(bucket or "").strip().strip("/")
        normalized_object_key = str(object_key or "").strip().lstrip("/")
        if normalized_bucket and normalized_object_key:
            candidates.append(("bucket_key", f"{normalized_bucket}/{normalized_object_key}"))

        normalized_object_url = SessionDB._normalize_juhe_attachment_object_url(object_url)
        if normalized_object_url:
            candidates.append(("object_url", normalized_object_url))

        normalized_file_id = str(file_id or "").strip()
        normalized_md5 = str(file_md5 or "").strip().lower()
        if normalized_file_id and normalized_md5:
            candidates.append(("file_id_md5", f"{normalized_file_id}:{normalized_md5}"))

        return candidates

    def get_juhe_attachment_parse_cache(
        self,
        *,
        platform: str,
        bucket: Any = None,
        object_key: Any = None,
        object_url: Any = None,
        file_id: Any = None,
        file_md5: Any = None,
    ) -> Optional[Dict[str, Any]]:
        normalized_platform = str(platform or "").strip()
        if not normalized_platform:
            return None
        candidates = self._juhe_attachment_cache_candidates(
            bucket=bucket,
            object_key=object_key,
            object_url=object_url,
            file_id=file_id,
            file_md5=file_md5,
        )
        if not candidates:
            return None

        with self._lock:
            for identity_kind, identity_value in candidates:
                row = self._conn.execute(
                    """
                    SELECT * FROM juhe_attachment_parse_cache
                    WHERE platform = ? AND identity_kind = ? AND identity_value = ?
                    LIMIT 1
                    """,
                    (normalized_platform, identity_kind, identity_value),
                ).fetchone()
                if row is not None:
                    return dict(row)
        return None

    def upsert_juhe_attachment_parse_cache(
        self,
        *,
        platform: str,
        bucket: Any = None,
        object_key: Any = None,
        object_url: Any = None,
        file_id: Any = None,
        file_md5: Any = None,
        media_type: Any = None,
        file_name: Any = None,
        parser: Any = None,
        extracted_text: Any = None,
        content_kind: Any = None,
        display_description: Any = None,
        parse_status: Any = None,
        error_message: Any = None,
        content_format: Any = None,
        created_at: Optional[float] = None,
    ) -> int:
        normalized_platform = str(platform or "").strip()
        if not normalized_platform:
            raise ValueError("platform is required")
        candidates = self._juhe_attachment_cache_candidates(
            bucket=bucket,
            object_key=object_key,
            object_url=object_url,
            file_id=file_id,
            file_md5=file_md5,
        )
        if not candidates:
            raise ValueError("a stable Juhe attachment identity is required")
        text = str(extracted_text or "").strip()
        normalized_media_type = str(media_type or "").strip().lower()
        normalized_status = str(parse_status or "").strip().lower()
        if normalized_status not in {"pending", "success", "failed"}:
            normalized_status = "success" if text else "pending"
        if normalized_status == "success" and not text:
            raise ValueError("extracted_text is required when parse_status=success")
        normalized_content_kind = str(content_kind or "").strip().lower()
        if normalized_content_kind not in {"image", "document"}:
            normalized_content_kind = "image" if normalized_media_type.startswith("image/") else "document"
        normalized_content_format = str(content_format or "").strip().lower()
        if normalized_content_format not in {"text", "markdown", "vision"}:
            if normalized_content_kind == "image":
                normalized_content_format = "vision"
            else:
                normalized_content_format = "text"

        identity_kind, identity_value = candidates[0]
        now = float(created_at if created_at is not None else time.time())
        normalized_object_url = self._normalize_juhe_attachment_object_url(object_url) or None
        normalized_bucket = str(bucket or "").strip().strip("/") or None
        normalized_object_key = str(object_key or "").strip().lstrip("/") or None
        normalized_file_id = str(file_id or "").strip() or None
        normalized_file_md5 = str(file_md5 or "").strip().lower() or None
        normalized_description = str(display_description or "").strip() or None
        normalized_error = str(error_message or "").strip() or None

        def _do(conn):
            conn.execute(
                """
                INSERT INTO juhe_attachment_parse_cache (
                    platform, identity_kind, identity_value, bucket, object_key,
                    object_url, file_id, file_md5, media_type, file_name, parser,
                    extracted_text, content_kind, display_description, parse_status,
                    error_message, content_format, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, identity_kind, identity_value) DO UPDATE SET
                    bucket = COALESCE(excluded.bucket, juhe_attachment_parse_cache.bucket),
                    object_key = COALESCE(excluded.object_key, juhe_attachment_parse_cache.object_key),
                    object_url = COALESCE(excluded.object_url, juhe_attachment_parse_cache.object_url),
                    file_id = COALESCE(excluded.file_id, juhe_attachment_parse_cache.file_id),
                    file_md5 = COALESCE(excluded.file_md5, juhe_attachment_parse_cache.file_md5),
                    media_type = COALESCE(excluded.media_type, juhe_attachment_parse_cache.media_type),
                    file_name = COALESCE(excluded.file_name, juhe_attachment_parse_cache.file_name),
                    parser = COALESCE(excluded.parser, juhe_attachment_parse_cache.parser),
                    extracted_text = excluded.extracted_text,
                    content_kind = COALESCE(excluded.content_kind, juhe_attachment_parse_cache.content_kind),
                    display_description = COALESCE(excluded.display_description, juhe_attachment_parse_cache.display_description),
                    parse_status = COALESCE(excluded.parse_status, juhe_attachment_parse_cache.parse_status),
                    error_message = COALESCE(excluded.error_message, juhe_attachment_parse_cache.error_message),
                    content_format = COALESCE(excluded.content_format, juhe_attachment_parse_cache.content_format),
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_platform,
                    identity_kind,
                    identity_value,
                    normalized_bucket,
                    normalized_object_key,
                    normalized_object_url,
                    normalized_file_id,
                    normalized_file_md5,
                    normalized_media_type or None,
                    str(file_name or "").strip() or None,
                    str(parser or "").strip() or None,
                    text,
                    normalized_content_kind,
                    normalized_description,
                    normalized_status,
                    normalized_error,
                    normalized_content_format,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT id FROM juhe_attachment_parse_cache
                WHERE platform = ? AND identity_kind = ? AND identity_value = ?
                LIMIT 1
                """,
                (normalized_platform, identity_kind, identity_value),
            ).fetchone()
            return int(row["id"]) if row else 0

        return self._execute_write(_do)

    # =========================================================================
    # Search
    # =========================================================================

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """Sanitize user input for safe use in FTS5 MATCH queries.

        FTS5 has its own query syntax where characters like ``"``, ``(``, ``)``,
        ``+``, ``*``, ``{``, ``}`` and bare boolean operators (``AND``, ``OR``,
        ``NOT``) have special meaning.  Passing raw user input directly to
        MATCH can cause ``sqlite3.OperationalError``.

        Strategy:
        - Preserve properly paired quoted phrases (``"exact phrase"``)
        - Strip unmatched FTS5-special characters that would cause errors
        - Wrap unquoted hyphenated and dotted terms in quotes so FTS5
          matches them as exact phrases instead of splitting on the
          hyphen/dot (e.g. ``chat-send``, ``P2.2``, ``my-app.config.ts``)
        """
        # Step 1: Extract balanced double-quoted phrases and protect them
        # from further processing via numbered placeholders.
        _quoted_parts: list = []

        def _preserve_quoted(m: re.Match) -> str:
            _quoted_parts.append(m.group(0))
            return f"\x00Q{len(_quoted_parts) - 1}\x00"

        sanitized = re.sub(r'"[^"]*"', _preserve_quoted, query)

        # Step 2: Strip remaining (unmatched) FTS5-special characters
        sanitized = re.sub(r'[+{}()\"^]', " ", sanitized)

        # Step 3: Collapse repeated * (e.g. "***") into a single one,
        # and remove leading * (prefix-only needs at least one char before *)
        sanitized = re.sub(r"\*+", "*", sanitized)
        sanitized = re.sub(r"(^|\s)\*", r"\1", sanitized)

        # Step 4: Remove dangling boolean operators at start/end that would
        # cause syntax errors (e.g. "hello AND" or "OR world")
        sanitized = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", sanitized.strip())
        sanitized = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", sanitized.strip())

        # Step 5: Wrap unquoted dotted and/or hyphenated terms in double
        # quotes.  FTS5's tokenizer splits on dots and hyphens, turning
        # ``chat-send`` into ``chat AND send`` and ``P2.2`` into ``p2 AND 2``.
        # Quoting preserves phrase semantics.  A single pass avoids the
        # double-quoting bug that would occur if dotted and hyphenated
        # patterns were applied sequentially (e.g. ``my-app.config``).
        sanitized = re.sub(r"\b(\w+(?:[.-]\w+)+)\b", r'"\1"', sanitized)

        # Step 6: Restore preserved quoted phrases
        for i, quoted in enumerate(_quoted_parts):
            sanitized = sanitized.replace(f"\x00Q{i}\x00", quoted)

        return sanitized.strip()

    def search_messages(
        self,
        query: str,
        source_filter: List[str] = None,
        exclude_sources: List[str] = None,
        role_filter: List[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Full-text search across session messages using FTS5.

        Supports FTS5 query syntax:
          - Simple keywords: "docker deployment"
          - Phrases: '"exact phrase"'
          - Boolean: "docker OR kubernetes", "python NOT java"
          - Prefix: "deploy*"

        Returns matching messages with session metadata, content snippet,
        and surrounding context (1 message before and after the match).
        """
        if not query or not query.strip():
            return []

        query = self._sanitize_fts5_query(query)
        if not query:
            return []

        # Build WHERE clauses dynamically
        where_clauses = ["messages_fts MATCH ?"]
        params: list = [query]

        if source_filter is not None:
            source_placeholders = ",".join("?" for _ in source_filter)
            where_clauses.append(f"s.source IN ({source_placeholders})")
            params.extend(source_filter)

        if exclude_sources is not None:
            exclude_placeholders = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({exclude_placeholders})")
            params.extend(exclude_sources)

        if role_filter:
            role_placeholders = ",".join("?" for _ in role_filter)
            where_clauses.append(f"m.role IN ({role_placeholders})")
            params.extend(role_filter)

        where_sql = " AND ".join(where_clauses)
        params.extend([limit, offset])

        sql = f"""
            SELECT
                m.id,
                m.session_id,
                m.role,
                snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                m.content,
                m.timestamp,
                m.tool_name,
                s.source,
                s.model,
                s.started_at AS session_started
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE {where_sql}
            ORDER BY rank
            LIMIT ? OFFSET ?
        """

        with self._lock:
            try:
                cursor = self._conn.execute(sql, params)
            except sqlite3.OperationalError:
                # FTS5 query syntax error despite sanitization — return empty
                return []
            matches = [dict(row) for row in cursor.fetchall()]

        # Add surrounding context (1 message before + after each match).
        # Done outside the lock so we don't hold it across N sequential queries.
        for match in matches:
            try:
                with self._lock:
                    ctx_cursor = self._conn.execute(
                        """SELECT role, content FROM messages
                           WHERE session_id = ? AND id >= ? - 1 AND id <= ? + 1
                           ORDER BY id""",
                        (match["session_id"], match["id"], match["id"]),
                    )
                    context_msgs = [
                        {"role": r["role"], "content": (r["content"] or "")[:200]}
                        for r in ctx_cursor.fetchall()
                    ]
                match["context"] = context_msgs
            except Exception:
                match["context"] = []

        # Remove full content from result (snippet is enough, saves tokens)
        for match in matches:
            match.pop("content", None)

        return matches

    def search_sessions(
        self,
        source: str = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List sessions, optionally filtered by source."""
        with self._lock:
            if source:
                cursor = self._conn.execute(
                    "SELECT * FROM sessions WHERE source = ? ORDER BY started_at DESC LIMIT ? OFFSET ?",
                    (source, limit, offset),
                )
            else:
                cursor = self._conn.execute(
                    "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # Utility
    # =========================================================================

    def session_count(self, source: str = None) -> int:
        """Count sessions, optionally filtered by source."""
        with self._lock:
            if source:
                cursor = self._conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE source = ?", (source,)
                )
            else:
                cursor = self._conn.execute("SELECT COUNT(*) FROM sessions")
            return cursor.fetchone()[0]

    def message_count(self, session_id: str = None) -> int:
        """Count messages, optionally for a specific session."""
        with self._lock:
            if session_id:
                cursor = self._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
                )
            else:
                cursor = self._conn.execute("SELECT COUNT(*) FROM messages")
            return cursor.fetchone()[0]

    # =========================================================================
    # Export and cleanup
    # =========================================================================

    def export_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Export a single session with all its messages as a dict."""
        session = self.get_session(session_id)
        if not session:
            return None
        messages = self.get_messages(session_id)
        return {**session, "messages": messages}

    def export_all(self, source: str = None) -> List[Dict[str, Any]]:
        """
        Export all sessions (with messages) as a list of dicts.
        Suitable for writing to a JSONL file for backup/analysis.
        """
        sessions = self.search_sessions(source=source, limit=100000)
        results = []
        for session in sessions:
            messages = self.get_messages(session["id"])
            results.append({**session, "messages": messages})
        return results

    def clear_messages(self, session_id: str) -> None:
        """Delete all messages for a session and reset its counters."""
        def _do(conn):
            conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
            conn.execute(
                "UPDATE sessions SET message_count = 0, tool_call_count = 0 WHERE id = ?",
                (session_id,),
            )
        self._execute_write(_do)

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages.

        Child sessions are orphaned (parent_session_id set to NULL) rather
        than cascade-deleted, so they remain accessible independently.
        Returns True if the session was found and deleted.
        """
        def _do(conn):
            cursor = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = ?", (session_id,)
            )
            if cursor.fetchone()[0] == 0:
                return False
            # Orphan child sessions so FK constraint is satisfied
            conn.execute(
                "UPDATE sessions SET parent_session_id = NULL "
                "WHERE parent_session_id = ?",
                (session_id,),
            )
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return True
        return self._execute_write(_do)

    def prune_sessions(self, older_than_days: int = 90, source: str = None) -> int:
        """Delete sessions older than N days. Returns count of deleted sessions.

        Only prunes ended sessions (not active ones).  Child sessions outside
        the prune window are orphaned (parent_session_id set to NULL) rather
        than cascade-deleted.
        """
        cutoff = time.time() - (older_than_days * 86400)

        def _do(conn):
            if source:
                cursor = conn.execute(
                    """SELECT id FROM sessions
                       WHERE started_at < ? AND ended_at IS NOT NULL AND source = ?""",
                    (cutoff, source),
                )
            else:
                cursor = conn.execute(
                    "SELECT id FROM sessions WHERE started_at < ? AND ended_at IS NOT NULL",
                    (cutoff,),
                )
            session_ids = set(row["id"] for row in cursor.fetchall())

            if not session_ids:
                return 0

            # Orphan any sessions whose parent is about to be deleted
            placeholders = ",".join("?" * len(session_ids))
            conn.execute(
                f"UPDATE sessions SET parent_session_id = NULL "
                f"WHERE parent_session_id IN ({placeholders})",
                list(session_ids),
            )

            for sid in session_ids:
                conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
                conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            return len(session_ids)

        return self._execute_write(_do)
