"""Read-only Juhe conversation helpers for the web UI."""

from __future__ import annotations

from typing import Any, Iterable

from gateway.juhe_cache import JuheCacheStore
from gateway.juhe_room_memory import get_room_memory_path, load_room_memory
from hermes_constants import get_hermes_home
from hermes_state import SessionDB

JUHE_PLATFORM = "juhe"
GROUP_PREFIX = "R:"
TEXT_MESSAGE_TYPE = 2

MSG_TYPE_LABELS: dict[int, str] = {
    1: "撤回",
    2: "文本",
    3: "位置",
    4: "链接",
    5: "图片",
    6: "语音",
    7: "视频",
    8: "文件",
    9: "红包",
    10: "表情",
    11: "名片",
    12: "App分享",
    13: "混合",
    14: "视频号",
    15: "文本卡片",
    16: "合并",
    1011: "系统",
    1012: "已读回执",
}


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_group_conversation(conversation_id: str) -> bool:
    return str(conversation_id or "").strip().upper().startswith(GROUP_PREFIX)


def _room_id_from_conversation(conversation_id: str) -> str:
    text = str(conversation_id or "").strip()
    return text[2:] if _is_group_conversation(text) else text


def _message_label(message_type: Any) -> str:
    msg_type = _safe_int(message_type) or 0
    return MSG_TYPE_LABELS.get(msg_type, f"类型{msg_type}")


def _message_preview(row: dict[str, Any]) -> str:
    preview = str(row.get("text_preview") or "").strip()
    if preview:
        return preview

    raw_payload = row.get("raw_payload")
    if isinstance(raw_payload, dict):
        for key in ("text", "content", "file_name", "filename", "name"):
            value = str(raw_payload.get(key) or "").strip()
            if value:
                return value
    return f"[{_message_label(row.get('message_type'))}]"


def _message_cursor(row: dict[str, Any]) -> str:
    message_id = str(row.get("message_id") or "").strip()
    if message_id:
        return message_id
    appinfo = str(row.get("appinfo") or "").strip()
    if appinfo:
        return f"appinfo:{appinfo}"
    row_id = _safe_int(row.get("id"))
    if row_id:
        return f"row:{row_id}"
    return f"cursor:{row.get('created_at', 0)}:{row.get('sender_id', '')}"


def _member_count(room: dict[str, Any] | None, members: list[dict[str, Any]]) -> int | None:
    if members:
        return len(members)
    if isinstance(room, dict):
        count = _safe_int(room.get("member_count"))
        if count is not None:
            return count
    return None


def _matches_query(summary: dict[str, Any], query: str) -> bool:
    normalized = str(query or "").strip().lower()
    if not normalized:
        return True

    haystack = " ".join(
        [
            str(summary.get("title") or ""),
            str(summary.get("room_id") or ""),
            str(summary.get("conversation_id") or ""),
            str(summary.get("last_message_preview") or ""),
            str(summary.get("last_message_sender_name") or ""),
        ]
    ).lower()
    return normalized in haystack


def _deserialize_rows(db: SessionDB, rows: Iterable[Any]) -> list[dict[str, Any]]:
    return [db._deserialize_room_message(row) for row in rows]


def _query_latest_rows(db: SessionDB) -> list[dict[str, Any]]:
    with db._lock:
        rows = db._conn.execute(
            """
            SELECT rm.*
            FROM room_messages AS rm
            INNER JOIN (
                SELECT conversation_id, MAX(id) AS max_id
                FROM room_messages
                WHERE platform = ? AND conversation_id LIKE 'R:%'
                GROUP BY conversation_id
            ) AS latest
              ON latest.max_id = rm.id
            ORDER BY rm.created_at DESC, rm.id DESC
            """,
            (JUHE_PLATFORM,),
        ).fetchall()
    return _deserialize_rows(db, rows)


def _query_latest_row_for_conversation(db: SessionDB, conversation_id: str) -> dict[str, Any] | None:
    with db._lock:
        row = db._conn.execute(
            """
            SELECT *
            FROM room_messages
            WHERE platform = ? AND conversation_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (JUHE_PLATFORM, conversation_id),
        ).fetchone()
    return db._deserialize_room_message(row) if row is not None else None


def _cursor_to_row_id(db: SessionDB, conversation_id: str, before_message_id: str | None) -> int | None:
    cursor = str(before_message_id or "").strip()
    if not cursor:
        return None
    if cursor.startswith("row:"):
        return _safe_int(cursor[4:])

    column = "message_id"
    value = cursor
    if cursor.startswith("appinfo:"):
        column = "appinfo"
        value = cursor[8:]

    with db._lock:
        row = db._conn.execute(
            f"""
            SELECT id
            FROM room_messages
            WHERE platform = ? AND conversation_id = ? AND {column} = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (JUHE_PLATFORM, conversation_id, value),
        ).fetchone()
    return int(row["id"]) if row is not None else None


def _query_room_history(
    db: SessionDB,
    conversation_id: str,
    *,
    before_message_id: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    params: list[Any] = [JUHE_PLATFORM, conversation_id]
    cursor_sql = ""
    cursor_row_id = _cursor_to_row_id(db, conversation_id, before_message_id)
    if cursor_row_id is not None:
        cursor_sql = "AND id < ?"
        params.append(cursor_row_id)

    fetch_limit = max(1, min(int(limit or 50), 100))

    with db._lock:
        rows = db._conn.execute(
            f"""
            SELECT *
            FROM room_messages
            WHERE platform = ? AND conversation_id = ?
            {cursor_sql}
            ORDER BY id DESC
            LIMIT ?
            """,
            [*params, fetch_limit + 1],
        ).fetchall()

    has_more = len(rows) > fetch_limit
    return list(reversed(_deserialize_rows(db, rows[:fetch_limit]))), has_more


def _build_summary(
    cache_store: JuheCacheStore,
    *,
    conversation_id: str,
    room: dict[str, Any] | None,
    latest_row: dict[str, Any] | None,
) -> dict[str, Any]:
    room_id = _room_id_from_conversation(conversation_id)
    members = cache_store.list_room_members(room_id)
    title = ""
    if isinstance(room, dict):
        title = str(room.get("roomname") or room.get("name") or "").strip()
    if not title:
        title = room_id or conversation_id

    last_preview = _message_preview(latest_row) if latest_row else None
    last_message_type = latest_row.get("message_type") if latest_row else None
    last_sender_name = (
        str(latest_row.get("sender_name") or latest_row.get("sender_id") or "").strip() or None
        if latest_row
        else None
    )
    last_message_id = _message_cursor(latest_row) if latest_row else None
    last_message_at = float(latest_row.get("created_at") or 0) if latest_row else None

    return {
        "conversation_id": conversation_id,
        "room_id": room_id,
        "title": title,
        "member_count": _member_count(room, members),
        "last_message_id": last_message_id,
        "last_message_preview": last_preview,
        "last_message_sender_name": last_sender_name,
        "last_message_at": last_message_at,
        "last_message_type": _safe_int(last_message_type),
        "last_message_type_label": _message_label(last_message_type) if latest_row else None,
        "has_messages": latest_row is not None,
    }


def _build_room_memory(room_id: str) -> dict[str, Any]:
    path = get_room_memory_path(room_id)
    content = load_room_memory(room_id)
    return {
        "exists": bool(content),
        "path": str(path) if path.exists() else None,
        "content": content,
    }


def list_group_conversations(query: str = "", *, limit: int = 200) -> dict[str, Any]:
    cache_store = JuheCacheStore()
    db = SessionDB(db_path=get_hermes_home() / "state.db")
    try:
        rooms = cache_store.list_rooms()
        latest_rows = _query_latest_rows(db)
    finally:
        db.close()

    rooms_by_conversation = {
        f"{GROUP_PREFIX}{str(room.get('room_id') or '').strip()}": room
        for room in rooms
        if str(room.get("room_id") or "").strip()
    }

    summaries: dict[str, dict[str, Any]] = {}
    for row in latest_rows:
        conversation_id = str(row.get("conversation_id") or "").strip()
        if not _is_group_conversation(conversation_id):
            continue
        summaries[conversation_id] = _build_summary(
            cache_store,
            conversation_id=conversation_id,
            room=rooms_by_conversation.get(conversation_id),
            latest_row=row,
        )

    for conversation_id, room in rooms_by_conversation.items():
        summaries.setdefault(
            conversation_id,
            _build_summary(
                cache_store,
                conversation_id=conversation_id,
                room=room,
                latest_row=None,
            ),
        )

    filtered = [item for item in summaries.values() if _matches_query(item, query)]
    filtered.sort(
        key=lambda item: (
            item.get("last_message_at") is None,
            -(item.get("last_message_at") or 0),
            str(item.get("title") or "").lower(),
        )
    )
    clamped_limit = max(1, min(int(limit or 200), 500))
    return {
        "conversations": filtered[:clamped_limit],
        "query": str(query or ""),
        "total": len(filtered),
    }


def get_group_conversation_detail(conversation_id: str) -> dict[str, Any] | None:
    normalized = str(conversation_id or "").strip()
    if not _is_group_conversation(normalized):
        return None

    cache_store = JuheCacheStore()
    room_id = _room_id_from_conversation(normalized)
    room = cache_store.get_room(room_id)
    members = cache_store.list_room_members(room_id)

    db = SessionDB(db_path=get_hermes_home() / "state.db")
    try:
        latest_row = _query_latest_row_for_conversation(db, normalized)
    finally:
        db.close()

    if room is None and latest_row is None:
        return None

    return {
        "conversation": _build_summary(
            cache_store,
            conversation_id=normalized,
            room=room,
            latest_row=latest_row,
        ),
        "members": members,
        "room_memory": _build_room_memory(normalized),
    }


def get_group_messages(
    conversation_id: str,
    *,
    before_message_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any] | None:
    normalized = str(conversation_id or "").strip()
    if not _is_group_conversation(normalized):
        return None

    cache_store = JuheCacheStore()
    room_exists = cache_store.get_room(_room_id_from_conversation(normalized)) is not None

    db = SessionDB(db_path=get_hermes_home() / "state.db")
    try:
        rows, has_more = _query_room_history(
            db,
            normalized,
            before_message_id=before_message_id,
            limit=limit,
        )
    finally:
        db.close()

    if not room_exists and not rows:
        return None

    messages = []
    for row in rows:
        message_type = _safe_int(row.get("message_type")) or 0
        preview = _message_preview(row)
        is_text = message_type == TEXT_MESSAGE_TYPE
        messages.append(
            {
                "id": _message_cursor(row),
                "message_id": str(row.get("message_id") or "").strip() or None,
                "timestamp": float(row.get("created_at") or 0),
                "direction": str(row.get("direction") or "inbound"),
                "sender_id": str(row.get("sender_id") or "").strip() or None,
                "sender_name": str(row.get("sender_name") or "").strip() or None,
                "message_type": message_type,
                "message_type_label": _message_label(message_type),
                "preview": preview,
                "text": preview if is_text else None,
                "is_text": is_text,
            }
        )

    return {
        "conversation_id": normalized,
        "messages": messages,
        "has_more": has_more,
    }
