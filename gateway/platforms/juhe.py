"""Juhe platform adapter.

Connects Hermes Agent to enterprise WeChat conversations through the juhebot
aggregate-chat gateway. Supports one account, WebSocket callbacks, text
messages, and file delivery from externally reachable HTTP(S) URLs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency gate
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency gate
    httpx = None  # type: ignore[assignment]
    HTTPX_AVAILABLE = False

try:
    import qwsaas as _qwsaas
    from qwsaas import (
        DownloadedAttachment,
        JuheWsClient,
        QwSaasClient,
        download_callback_attachment,
        send_big_file_from_url,
        send_small_file_from_url,
    )
    from qwsaas.callbacks import (
        NOTIFY_BATCH_NEW_MESSAGE,
        NOTIFY_NEW_MESSAGE,
        TEXT_MESSAGE_TYPE,
        parse_callback_envelope,
        parse_callback_event,
    )
    from qwsaas.exceptions import (
        QwSaasError,
        QwSaasPrivateObjectAccessError,
        QwSaasRequestError,
    )

    batch_get_member_detail = getattr(_qwsaas, "batch_get_member_detail", None)
    batch_get_room_detail = getattr(_qwsaas, "batch_get_room_detail", None)
    batch_get_userinfo = getattr(_qwsaas, "batch_get_userinfo", None)
    confirm_msg = getattr(_qwsaas, "confirm_msg", None)
    get_room_list = getattr(_qwsaas, "get_room_list", None)
    report_unread = getattr(_qwsaas, "report_unread", None)
    revoke_msg = getattr(_qwsaas, "revoke_msg", None)
    search_contact = getattr(_qwsaas, "search_contact", None)
    send_quote_msg = getattr(_qwsaas, "send_quote_msg", None)
    send_room_at = getattr(_qwsaas, "send_room_at", None)
    sync_contact = getattr(_qwsaas, "sync_contact", None)
    sync_label_list = getattr(_qwsaas, "sync_label_list", None)
    sync_msg = getattr(_qwsaas, "sync_msg", None)
    sync_multi_data = getattr(_qwsaas, "sync_multi_data", None)
    sync_room_info = getattr(_qwsaas, "sync_room_info", None)

    QWSAAS_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency gate
    DownloadedAttachment = None  # type: ignore[assignment]
    JuheWsClient = None  # type: ignore[assignment]
    QwSaasClient = None  # type: ignore[assignment]
    download_callback_attachment = None  # type: ignore[assignment]
    send_big_file_from_url = None  # type: ignore[assignment]
    send_small_file_from_url = None  # type: ignore[assignment]
    parse_callback_envelope = None  # type: ignore[assignment]
    parse_callback_event = None  # type: ignore[assignment]
    QwSaasError = Exception  # type: ignore[assignment]
    QwSaasPrivateObjectAccessError = Exception  # type: ignore[assignment]
    QwSaasRequestError = Exception  # type: ignore[assignment]
    batch_get_member_detail = None  # type: ignore[assignment]
    batch_get_room_detail = None  # type: ignore[assignment]
    batch_get_userinfo = None  # type: ignore[assignment]
    confirm_msg = None  # type: ignore[assignment]
    get_room_list = None  # type: ignore[assignment]
    report_unread = None  # type: ignore[assignment]
    revoke_msg = None  # type: ignore[assignment]
    search_contact = None  # type: ignore[assignment]
    send_quote_msg = None  # type: ignore[assignment]
    send_room_at = None  # type: ignore[assignment]
    sync_contact = None  # type: ignore[assignment]
    sync_label_list = None  # type: ignore[assignment]
    sync_msg = None  # type: ignore[assignment]
    sync_multi_data = None  # type: ignore[assignment]
    sync_room_info = None  # type: ignore[assignment]
    QWSAAS_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.juhe_cache import JuheCacheStore
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_document_from_bytes,
    cache_image_from_bytes,
)
from gateway.platforms.helpers import MessageDeduplicator

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://chat-api.juhebot.com"
DEFAULT_WS_URL = "wss://chat-api.juhebot.com/ws/juwe"

MSG_TYPE_REVOKE = 1
MSG_TYPE_TEXT = 2
MSG_TYPE_LOCATION = 3
MSG_TYPE_LINK = 4
MSG_TYPE_IMAGE = 5
MSG_TYPE_VOICE = 6
MSG_TYPE_VIDEO = 7
MSG_TYPE_FILE = 8
MSG_TYPE_HONGBAO = 9
MSG_TYPE_GIF = 10
MSG_TYPE_PERSONAL_CARD = 11
MSG_TYPE_WEAPP = 12
MSG_TYPE_MIXED = 13
MSG_TYPE_SPH_FEED = 14
MSG_TYPE_APP_TEXT_CARD = 15
MSG_TYPE_MERGE_MSG = 16
MSG_TYPE_SYSTEM = 1011
MSG_TYPE_READ_REPORT = 1012

TEXT_MESSAGE_TYPE = MSG_TYPE_TEXT

MSG_TYPE_LABELS = {
    MSG_TYPE_REVOKE: "撤回",
    MSG_TYPE_TEXT: "文本",
    MSG_TYPE_LOCATION: "位置",
    MSG_TYPE_LINK: "链接",
    MSG_TYPE_IMAGE: "图片",
    MSG_TYPE_VOICE: "语音",
    MSG_TYPE_VIDEO: "视频",
    MSG_TYPE_FILE: "文件",
    MSG_TYPE_HONGBAO: "红包",
    MSG_TYPE_GIF: "表情",
    MSG_TYPE_PERSONAL_CARD: "名片",
    MSG_TYPE_WEAPP: "App分享",
    MSG_TYPE_MIXED: "混合",
    MSG_TYPE_SPH_FEED: "视频号",
    MSG_TYPE_APP_TEXT_CARD: "文本卡片",
    MSG_TYPE_MERGE_MSG: "合并",
    MSG_TYPE_SYSTEM: "系统",
    MSG_TYPE_READ_REPORT: "已读回执",
}

DEDUP_TTL_SECONDS = 30 * 60
DEDUP_MAX_SIZE = 1000
RECONNECT_BACKOFF_SECONDS = [2, 5, 10, 30, 60]
SMALL_FILE_LIMIT_BYTES = 20 * 1024 * 1024
DEFAULT_INBOUND_ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_TEMP_S3_URL_EXPIRES_SECONDS = 3600
DEFAULT_TEMP_S3_PREFIX = "juhe-temp"
SUPPORTED_ATTACHMENT_MESSAGE_TYPES = {MSG_TYPE_IMAGE, MSG_TYPE_VOICE, MSG_TYPE_VIDEO, MSG_TYPE_FILE}


def check_juhe_requirements() -> bool:
    """Return True when Juhe runtime dependencies are available."""
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE and QWSAAS_AVAILABLE


def _coerce_list(value: Any) -> List[str]:
    """Coerce comma-separated or iterable config values into trimmed strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return bool(value)


def _first_non_empty_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _normalize_endpoint_url(value: Any, *, use_https: bool = True) -> str | None:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return None
    if "://" in text:
        return text
    scheme = "https" if use_https else "http"
    return f"{scheme}://{text}"


def _preview_json(value: Any, *, limit: int = 1500) -> str:
    """Return a bounded string preview for diagnostic logs."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = repr(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated>"


def _strip_target_prefix(value: str) -> str:
    text = str(value or "").strip()
    if text.upper().startswith(("S:", "R:")):
        return text[2:]
    return text


def _is_zero_like_id(value: Any) -> bool:
    text = str(value or "").strip()
    return text in {"", "0", "0.0", "null", "None"}


def _is_self_echo_message(message: Dict[str, Any]) -> bool:
    """Juhe marks outbound echoes with send_flag=1."""
    try:
        return int(message.get("send_flag") or 0) == 1
    except (TypeError, ValueError):
        return False


def _msg_type_label(msg_type: int) -> str:
    return MSG_TYPE_LABELS.get(msg_type, f"类型{msg_type}")


def _normalize_user(value: str) -> str:
    return _strip_target_prefix(value).lower()


def _normalize_group(value: str) -> str:
    return _strip_target_prefix(value).lower()


def _is_http_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _source_name(value: str, default: str = "attachment") -> str:
    text = str(value or "").strip()
    if not text:
        return default
    if _is_http_url(text):
        path = unquote(urlparse(text).path or "")
        name = Path(path).name
        return name or default
    return Path(text).name or default


def _prefixed_dm(value: str) -> str:
    text = str(value or "").strip()
    return text if text.upper().startswith("S:") else f"S:{text}"


def _prefixed_group(value: str) -> str:
    text = str(value or "").strip()
    return text if text.upper().startswith("R:") else f"R:{text}"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _chunked(values: List[str], size: int) -> List[List[str]]:
    if size <= 0:
        return [values]
    return [values[index : index + size] for index in range(0, len(values), size)]


def _entry_matches(entries: List[str], target: str, *, group: bool = False) -> bool:
    normalized_target = _normalize_group(target) if group else _normalize_user(target)
    for entry in entries:
        normalized_entry = _normalize_group(entry) if group else _normalize_user(entry)
        if normalized_entry == "*" or normalized_entry == normalized_target:
            return True
    return False


class JuheAdapter(BasePlatformAdapter):
    """Juhebot WebSocket adapter for enterprise WeChat text conversations."""

    MAX_MESSAGE_LENGTH = 4000

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.JUHE)

        extra = config.extra or {}
        self._app_key = str(extra.get("app_key") or os.getenv("JUHE_APP_KEY", "")).strip()
        self._app_secret = str(extra.get("app_secret") or os.getenv("JUHE_APP_SECRET", "")).strip()
        self._guid = str(extra.get("guid") or os.getenv("JUHE_GUID", "")).strip()
        self._base_url = str(
            extra.get("base_url") or os.getenv("JUHE_BASE_URL", DEFAULT_BASE_URL)
        ).strip().rstrip("/") or DEFAULT_BASE_URL
        self._private_base_url = (
            str(extra.get("private_base_url") or os.getenv("JUHE_PRIVATE_BASE_URL", "")).strip().rstrip("/")
            or None
        )
        self._temp_s3_endpoint_url = (
            str(extra.get("temp_s3_endpoint_url") or os.getenv("JUHE_S3_ENDPOINT_URL", "")).strip().rstrip("/")
            or None
        )
        self._temp_s3_region = str(
            extra.get("temp_s3_region") or os.getenv("JUHE_S3_REGION", "")
        ).strip() or None
        self._temp_s3_bucket = str(
            extra.get("temp_s3_bucket") or os.getenv("JUHE_S3_BUCKET", "")
        ).strip() or None
        self._temp_s3_access_key = str(
            extra.get("temp_s3_access_key")
            or os.getenv("JUHE_S3_ACCESS_KEY")
            or os.getenv("QINIU_ACCESS_KEY", "")
        ).strip() or None
        self._temp_s3_secret_key = str(
            extra.get("temp_s3_secret_key")
            or os.getenv("JUHE_S3_SECRET_KEY")
            or os.getenv("QINIU_SECRET_KEY", "")
        ).strip() or None
        self._temp_s3_prefix = str(
            extra.get("temp_s3_prefix") or os.getenv("JUHE_S3_PREFIX", DEFAULT_TEMP_S3_PREFIX)
        ).strip().strip("/") or DEFAULT_TEMP_S3_PREFIX
        self._temp_s3_addressing_style = str(
            extra.get("temp_s3_addressing_style") or os.getenv("JUHE_S3_ADDRESSING_STYLE", "virtual")
        ).strip().lower() or "virtual"
        temp_s3_expires_raw = extra.get(
            "temp_s3_url_expires_seconds",
            os.getenv("JUHE_S3_URL_EXPIRES_SECONDS", str(DEFAULT_TEMP_S3_URL_EXPIRES_SECONDS)),
        )
        try:
            self._temp_s3_url_expires_seconds = max(60, int(temp_s3_expires_raw))
        except (TypeError, ValueError):
            self._temp_s3_url_expires_seconds = DEFAULT_TEMP_S3_URL_EXPIRES_SECONDS
        inbound_attachment_limit_raw = extra.get(
            "inbound_attachment_max_bytes",
            os.getenv("JUHE_INBOUND_ATTACHMENT_MAX_BYTES", str(DEFAULT_INBOUND_ATTACHMENT_MAX_BYTES)),
        )
        try:
            self._inbound_attachment_max_bytes = max(0, int(inbound_attachment_limit_raw))
        except (TypeError, ValueError):
            self._inbound_attachment_max_bytes = DEFAULT_INBOUND_ATTACHMENT_MAX_BYTES
        inbound_s3_use_https_value = (
            extra["inbound_s3_use_https"]
            if "inbound_s3_use_https" in extra
            else _first_non_empty_text(
                os.getenv("JUHE_INBOUND_S3_USE_HTTPS", ""),
                os.getenv("MINIO_USE_HTTPS", ""),
            )
        )
        self._inbound_s3_use_https = _truthy(inbound_s3_use_https_value, default=True)
        self._inbound_s3_endpoint_url = _normalize_endpoint_url(
            _first_non_empty_text(
                extra.get("inbound_s3_endpoint_url"),
                os.getenv("JUHE_INBOUND_S3_ENDPOINT_URL", ""),
                os.getenv("MINIO_ENDPOINT", ""),
            ),
            use_https=self._inbound_s3_use_https,
        )
        self._inbound_s3_region = _first_non_empty_text(
            extra.get("inbound_s3_region"),
            os.getenv("JUHE_INBOUND_S3_REGION", ""),
            os.getenv("MINIO_REGION", ""),
        )
        self._inbound_s3_bucket = _first_non_empty_text(
            extra.get("inbound_s3_bucket"),
            os.getenv("JUHE_INBOUND_S3_BUCKET", ""),
            os.getenv("MINIO_DEFAULT_BUCKET", ""),
        )
        self._inbound_s3_access_key = _first_non_empty_text(
            extra.get("inbound_s3_access_key"),
            os.getenv("JUHE_INBOUND_S3_ACCESS_KEY", ""),
            os.getenv("MINIO_ACCESS_KEY", ""),
        )
        self._inbound_s3_secret_key = _first_non_empty_text(
            extra.get("inbound_s3_secret_key"),
            os.getenv("JUHE_INBOUND_S3_SECRET_KEY", ""),
            os.getenv("MINIO_SECRET_KEY", ""),
        )
        self._inbound_s3_addressing_style = str(
            _first_non_empty_text(
                extra.get("inbound_s3_addressing_style"),
                os.getenv("JUHE_INBOUND_S3_ADDRESSING_STYLE", ""),
                "path",
            )
        ).strip().lower() or "path"
        self._ws_url = str(
            extra.get("websocket_url")
            or extra.get("ws_url")
            or os.getenv("JUHE_WEBSOCKET_URL", DEFAULT_WS_URL)
        ).strip().rstrip("/") or DEFAULT_WS_URL

        self._dm_policy = str(extra.get("dm_policy") or "allowlist").strip().lower()
        allow_from = extra.get("allow_from")
        if allow_from is None:
            allow_from = os.getenv("JUHE_ALLOWED_USERS", "")
        self._allow_from = _coerce_list(allow_from)

        self._group_policy = str(extra.get("group_policy") or "allowlist").strip().lower()
        group_allow_from = extra.get("group_allow_from")
        if group_allow_from is None:
            group_allow_from = os.getenv("JUHE_GROUP_ALLOWED_CHATS", "")
        self._group_allow_from = _coerce_list(group_allow_from)
        self._groups = extra.get("groups") if isinstance(extra.get("groups"), dict) else {}
        self._trace_payloads = _truthy(
            extra.get("trace_payloads", os.getenv("JUHE_TRACE_PAYLOADS")),
            default=False,
        )

        self._ws = None
        self._sdk_client: Optional["QwSaasClient"] = None
        self._ws_client: Optional["JuheWsClient"] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._dedup = MessageDeduplicator(max_size=DEDUP_MAX_SIZE, ttl_seconds=DEDUP_TTL_SECONDS)
        self._juhe_cache = JuheCacheStore()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        if not AIOHTTP_AVAILABLE:
            message = "Juhe startup failed: aiohttp not installed"
            self._set_fatal_error("juhe_missing_dependency", message, retryable=True)
            logger.warning("[%s] %s", self.name, message)
            return False
        if not HTTPX_AVAILABLE:
            message = "Juhe startup failed: httpx not installed"
            self._set_fatal_error("juhe_missing_dependency", message, retryable=True)
            logger.warning("[%s] %s", self.name, message)
            return False
        if not QWSAAS_AVAILABLE:
            message = "Juhe startup failed: qwsaas SDK not installed"
            self._set_fatal_error("juhe_missing_dependency", message, retryable=True)
            logger.warning("[%s] %s", self.name, message)
            return False
        if not self._app_key or not self._app_secret or not self._guid:
            message = "Juhe startup failed: JUHE_APP_KEY, JUHE_APP_SECRET, and JUHE_GUID are required"
            self._set_fatal_error("juhe_missing_credentials", message, retryable=True)
            logger.warning("[%s] %s", self.name, message)
            return False

        try:
            self._sdk_client = self._build_sdk_client()
            self._ws_client = JuheWsClient(
                app_key=self._app_key,
                app_secret=self._app_secret,
                guid=self._guid,
                ws_url=self._ws_url,
                reconnect_backoff_seconds=tuple(RECONNECT_BACKOFF_SECONDS),
            )
            await self._ws_client.connect()
            try:
                await self._run_startup_sync()
            except Exception as exc:
                logger.warning("[%s] Startup sync failed: %s", self.name, exc, exc_info=True)
            self._mark_connected()
            self._listen_task = asyncio.create_task(self._listen_loop())
            logger.info("[%s] Connected to %s", self.name, self._ws_url)
            return True
        except Exception as exc:
            await self._cleanup()
            message = f"Juhe startup failed: {exc}"
            self._set_fatal_error("juhe_connect_error", message, retryable=True)
            logger.error("[%s] Failed to connect: %s", self.name, exc, exc_info=True)
            return False

    async def disconnect(self) -> None:
        self._running = False
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None
        await self._cleanup()
        self._dedup.clear()
        self._mark_disconnected()
        logger.info("[%s] Disconnected", self.name)

    async def _cleanup(self) -> None:
        if self._ws_client:
            await self._ws_client.close()
        self._ws_client = None
        self._ws = None
        self._sdk_client = None

    async def _listen_loop(self) -> None:
        if self._ws_client is None:
            return
        try:
            await self._ws_client.listen_forever(self._dispatch_ws_payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._running:
                logger.warning("[%s] WebSocket receive loop stopped: %s", self.name, exc)

    # ------------------------------------------------------------------
    # WebSocket/callback dispatch
    # ------------------------------------------------------------------

    async def _dispatch_ws_payload(self, payload: Dict[str, Any]) -> bool:
        if self._trace_payloads:
            logger.info("[%s] WS payload %s", self.name, _preview_json(payload))
        msg_type = str(payload.get("type") or "").strip()
        if msg_type == "auth_success":
            logger.info("[%s] Authentication succeeded", self.name)
            return True
        if msg_type == "error":
            message = str(payload.get("message") or "unknown Juhe server error")
            logger.error("[%s] Server error: %s", self.name, message)
            return False
        if msg_type != "callback":
            return True

        parsed = parse_callback_envelope(payload) if parse_callback_envelope else None
        if self._trace_payloads:
            logger.info(
                "[%s] callback envelope event_id=%s parsed_event=%s",
                self.name,
                payload.get("event_id"),
                _preview_json(parsed.raw_event if parsed else None),
            )
        success = await self._handle_parsed_callback(parsed)
        await self._send_ack(payload.get("event_id"), success)
        return success

    async def _send_ack(self, event_id: Any, success: bool) -> None:
        if self._ws_client is not None:
            await self._ws_client.ack(event_id, success)
            return
        if self._ws and not self._ws.closed:
            await self._ws.send_json({"type": "ack", "event_id": event_id, "success": success})

    async def _handle_callback_event(self, event: Any) -> bool:
        if not isinstance(event, dict):
            logger.warning("[%s] Ignoring malformed callback event (type=%s)", self.name, type(event).__name__)
            return False
        if str(event.get("guid") or "").strip() != self._guid:
            logger.warning(
                "[%s] Ignoring callback for mismatched guid (expected=%s got=%s)",
                self.name,
                self._guid,
                str(event.get("guid") or "").strip(),
            )
            return False

        try:
            notify_type = int(event.get("notify_type"))
        except (TypeError, ValueError):
            if self._trace_payloads:
                logger.info("[%s] Ignoring callback without notify_type: %s", self.name, _preview_json(event))
            return True
        if notify_type not in {NOTIFY_NEW_MESSAGE, NOTIFY_BATCH_NEW_MESSAGE}:
            if self._trace_payloads:
                logger.info("[%s] Ignoring callback notify_type=%s", self.name, notify_type)
            return True
        parsed = parse_callback_event(event) if parse_callback_event else None
        return await self._handle_parsed_callback(parsed)

    async def _handle_parsed_callback(self, parsed: Any) -> bool:
        if parsed is None:
            logger.warning("[%s] Ignoring malformed callback event", self.name)
            return False
        if str(parsed.guid or "").strip() != self._guid:
            logger.warning(
                "[%s] Ignoring callback for mismatched guid (expected=%s got=%s)",
                self.name,
                self._guid,
                str(parsed.guid or "").strip(),
            )
            return False
        if parsed.notify_type not in {NOTIFY_NEW_MESSAGE, NOTIFY_BATCH_NEW_MESSAGE}:
            if self._trace_payloads:
                logger.info("[%s] Ignoring callback notify_type=%s", self.name, parsed.notify_type)
            return True
        if not parsed.messages:
            logger.warning("[%s] Callback notify_type=%s does not contain messages", self.name, parsed.notify_type)
            return False

        ok = True
        for message in parsed.messages:
            ok = await self._process_sdk_message(message, source="callback") and ok
        return ok

    async def _process_sdk_message(self, message: Any, *, source: str = "callback") -> bool:
        sender_id = str(message.sender_id or "").strip()
        if not sender_id:
            logger.warning("[%s] Ignoring message without sender: %s", self.name, _preview_json(message.raw_message))
            return False

        if bool(message.is_self_echo):
            if self._trace_payloads:
                logger.info("[%s] Ignoring self echo message id=%s", self.name, message.message_id)
            return True

        is_group = bool(message.is_group)
        if is_group:
            chat_id = str(message.conversation_id or "").strip()
            if not chat_id:
                logger.warning("[%s] Group message missing group id: %s", self.name, _preview_json(message.raw_message))
                return False
            chat_type = "group"
            chat_name = str(
                self._raw_message_field(message, "room_name")
                or self._raw_message_field(message, "roomname")
                or chat_id
            )
            self._upsert_message_directory_entries(
                message,
                chat_id=chat_id,
                chat_name=chat_name,
                chat_type=chat_type,
            )
            self._record_message_metadata(message, delivered=False, source=source)
            if not self._is_group_allowed(chat_id, sender_id):
                if self._trace_payloads:
                    logger.info(
                        "[%s] Ignoring group message due to allowlist sender=%s chat=%s",
                        self.name,
                        sender_id,
                        chat_id,
                    )
                return True
            if not self._passes_mention_gate(chat_id, list(message.at_list)):
                if self._trace_payloads:
                    logger.info("[%s] Ignoring group message without required mention chat=%s", self.name, chat_id)
                return True
        else:
            chat_id = str(message.conversation_id or _prefixed_dm(sender_id))
            chat_type = "dm"
            chat_name = str(message.sender_name or sender_id)
            self._upsert_message_directory_entries(
                message,
                chat_id=chat_id,
                chat_name=chat_name,
                chat_type=chat_type,
            )
            self._record_message_metadata(message, delivered=False, source=source)
            if not self._is_dm_allowed(sender_id):
                if self._trace_payloads:
                    logger.info("[%s] Ignoring DM due to allowlist sender=%s", self.name, sender_id)
                return True

        msg_type = int(message.message_type)
        text = str(message.text or "")
        message_id = str(message.message_id or "")
        if self._dedup.is_duplicate(message_id):
            if self._trace_payloads:
                logger.info("[%s] Dropping duplicate message id=%s", self.name, message_id)
            return True
        if self._is_message_already_delivered(message):
            if self._trace_payloads:
                logger.info("[%s] Dropping previously delivered message id=%s", self.name, message_id)
            return True
        if not _is_zero_like_id(self._raw_message_field(message, "refer_id")):
            if self._trace_payloads:
                logger.info(
                    "[%s] Ignoring secondary callback id=%s refer_id=%s",
                    self.name,
                    message_id,
                    self._raw_message_field(message, "refer_id"),
                )
            return True

        message_source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=sender_id,
            user_name=str(message.sender_name or sender_id),
        )

        if msg_type in SUPPORTED_ATTACHMENT_MESSAGE_TYPES:
            inbound = await self._build_attachment_event(message=message, source=message_source)
            logger.info(
                "[%s] inbound from=%s chat=%s type=%s attachment=%s",
                self.name,
                sender_id,
                chat_id,
                chat_type,
                inbound.message_type.value,
            )
            await self.handle_message(inbound)
            self._record_message_metadata(message, delivered=True, source=source)
            return True

        if msg_type != TEXT_MESSAGE_TYPE:
            label = _msg_type_label(msg_type)
            logger.warning(
                "[%s] Unsupported message type=%s (%s) from=%s chat=%s",
                self.name,
                msg_type,
                label,
                sender_id,
                chat_id,
            )
            if source != "sync":
                await self._send_unsupported_notice(chat_id, label)
            self._record_message_metadata(message, delivered=True, source=source)
            return True

        if not text:
            if self._trace_payloads:
                logger.info(
                    "[%s] Ignoring empty text message id=%s msg_type=%s",
                    self.name,
                    message_id,
                    msg_type,
                )
            return True

        inbound = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=message_source,
            raw_message=message.raw_message,
            message_id=message_id,
            timestamp=datetime.now(),
        )
        logger.info("[%s] inbound from=%s chat=%s type=%s", self.name, sender_id, chat_id, chat_type)
        await self.handle_message(inbound)
        self._record_message_metadata(message, delivered=True, source=source)
        return True

    async def _build_attachment_event(self, *, message: Any, source: Any) -> MessageEvent:
        download = None
        attachment_name = self._attachment_name(message)
        attachment_type = self._attachment_message_type(message)
        original_text = str(message.text or "").strip()
        media_urls: List[str] = []
        media_types: List[str] = []

        try:
            download = await self._download_inbound_attachment(message)
            attachment_name = str(download.file_name or attachment_name).strip() or attachment_name
            attachment_type = self._attachment_message_type(message, content_type=download.content_type)
            media_types = [download.content_type]
            media_urls = [self._cache_inbound_attachment(download.data, attachment_name, attachment_type)]
            text = original_text or self._attachment_notice_text(attachment_type, attachment_name)
        except Exception as exc:
            logger.warning(
                "[%s] Failed to download inbound attachment message_id=%s kind=%s: %s",
                self.name,
                getattr(message, "message_id", ""),
                getattr(message, "attachment_kind", None),
                exc,
            )
            fallback = self._attachment_metadata_only_text(attachment_type, attachment_name, exc)
            text = self._combine_attachment_text(original_text, fallback)

        return MessageEvent(
            text=text,
            message_type=attachment_type,
            source=source,
            raw_message=message.raw_message,
            message_id=str(message.message_id or ""),
            media_urls=media_urls,
            media_types=media_types,
            timestamp=datetime.now(),
        )

    async def _download_inbound_attachment(self, message: Any) -> Any:
        if download_callback_attachment is None:
            raise RuntimeError("qwsaas download helper is unavailable")
        try:
            return await download_callback_attachment(
                self._get_sdk_client(),
                download_url=str(getattr(message, "download_url", "") or ""),
                file_id=getattr(message, "file_id", None),
                file_name=getattr(message, "file_name", None),
                file_size=getattr(message, "file_size", None),
                aes_key=getattr(message, "aes_key", None),
                auth_key=getattr(message, "auth_key", None),
                attachment_kind=getattr(message, "attachment_kind", None),
                mime_type=getattr(message, "mime_type", None),
                is_hd=getattr(message, "is_hd", None),
                base_request=getattr(message, "base_request", None),
                max_bytes=self._inbound_attachment_max_bytes,
            )
        except QwSaasPrivateObjectAccessError as exc:
            return await asyncio.to_thread(
                self._download_private_object_via_inbound_s3_sync,
                exc.object_url,
                getattr(message, "file_name", None),
                getattr(message, "mime_type", None),
            )

    def _cache_inbound_attachment(self, data: bytes, file_name: str, message_type: MessageType) -> str:
        if message_type == MessageType.PHOTO:
            ext = Path(file_name).suffix.lower() or ".jpg"
            return cache_image_from_bytes(data, ext)
        return cache_document_from_bytes(data, file_name)

    def _attachment_message_type(self, message: Any, *, content_type: str | None = None) -> MessageType:
        kind = str(getattr(message, "attachment_kind", "") or "").strip().lower()
        mime_type = str(content_type or getattr(message, "mime_type", "") or "").strip().lower()

        if kind == "image" or mime_type.startswith("image/"):
            return MessageType.PHOTO
        if kind == "video" or mime_type.startswith("video/"):
            return MessageType.VIDEO
        if kind == "audio" or mime_type.startswith("audio/"):
            if self._looks_like_voice_attachment(mime_type, getattr(message, "file_name", None)):
                return MessageType.VOICE
            return MessageType.AUDIO
        return MessageType.DOCUMENT

    def _attachment_name(self, message: Any) -> str:
        file_name = str(getattr(message, "file_name", "") or "").strip()
        if file_name:
            return file_name
        download_url = str(getattr(message, "download_url", "") or "").strip()
        derived = _source_name(download_url, default="")
        if derived:
            return derived

        default_names = {
            "image": "image.jpg",
            "audio": "audio.amr",
            "video": "video.mp4",
            "document": "document",
        }
        return default_names.get(str(getattr(message, "attachment_kind", "") or "").strip().lower(), "attachment")

    def _looks_like_voice_attachment(self, mime_type: str, file_name: Any) -> bool:
        lowered_mime = str(mime_type or "").strip().lower()
        if lowered_mime in {
            "audio/amr",
            "audio/amr-wb",
            "audio/ogg",
            "audio/opus",
            "audio/x-opus+ogg",
        }:
            return True
        ext = Path(str(file_name or "")).suffix.lower()
        return ext in {".amr", ".ogg", ".opus", ".silk"}

    def _attachment_notice_text(self, message_type: MessageType, file_name: str) -> str:
        label = {
            MessageType.PHOTO: "image",
            MessageType.VIDEO: "video",
            MessageType.AUDIO: "audio",
            MessageType.VOICE: "audio",
            MessageType.DOCUMENT: "document",
        }.get(message_type, "attachment")
        return f"[Received {label}: {file_name}]"

    def _attachment_metadata_only_text(
        self,
        message_type: MessageType,
        file_name: str,
        error: Exception,
    ) -> str:
        label = {
            MessageType.PHOTO: "image",
            MessageType.VIDEO: "video",
            MessageType.AUDIO: "audio",
            MessageType.VOICE: "audio",
            MessageType.DOCUMENT: "document",
        }.get(message_type, "attachment")
        error_text = str(error or "").lower()
        reason = "exceeds download limit" if "exceeds" in error_text else "download unavailable"
        return f"[Received {label} metadata only: {file_name} ({reason})]"

    def _combine_attachment_text(self, original_text: str, fallback_text: str) -> str:
        if not original_text:
            return fallback_text
        return f"{original_text}\n\n{fallback_text}"

    def _raw_message_field(self, message: Any, key: str, default: Any = None) -> Any:
        raw_message = getattr(message, "raw_message", None)
        if isinstance(raw_message, dict) and raw_message.get(key) is not None:
            return raw_message.get(key)
        return default

    def _upsert_message_directory_entries(
        self,
        message: Any,
        *,
        chat_id: str,
        chat_name: str,
        chat_type: str,
    ) -> None:
        sender_id = str(message.sender_id or "").strip()
        sender_name = str(message.sender_name or sender_id).strip()
        if sender_id:
            self._juhe_cache.upsert_contact(
                {
                    "user_id": sender_id,
                    "name": sender_name or sender_id,
                }
            )

        if chat_type == "group":
            room_name = str(
                self._raw_message_field(message, "room_name")
                or self._raw_message_field(message, "roomname")
                or chat_name
                or _strip_target_prefix(chat_id)
            ).strip()
            self._juhe_cache.upsert_room(
                {
                    "room_id": _strip_target_prefix(chat_id),
                    "roomname": room_name or _strip_target_prefix(chat_id),
                }
            )

    def _build_quote_payload(self, message: Any) -> dict[str, Any]:
        msg_type = int(getattr(message, "message_type", TEXT_MESSAGE_TYPE) or TEXT_MESSAGE_TYPE)
        payload: dict[str, Any] = {"msg_type": msg_type}
        if msg_type == TEXT_MESSAGE_TYPE:
            payload["content"] = str(getattr(message, "text", "") or "")
            return payload

        if getattr(message, "file_id", None):
            payload["file_id"] = str(message.file_id)
        if getattr(message, "file_name", None):
            payload["file_name"] = str(message.file_name)
        if getattr(message, "file_size", None) is not None:
            payload["size"] = _safe_int(getattr(message, "file_size", None), default=0)
        if getattr(message, "file_md5", None):
            payload["md5"] = str(message.file_md5)
        if getattr(message, "aes_key", None):
            payload["aes_key"] = str(message.aes_key)
        if getattr(message, "auth_key", None):
            payload["auth_key"] = str(message.auth_key)
        if getattr(message, "download_url", None):
            payload["url"] = str(message.download_url)
        if not payload.get("content") and getattr(message, "text", None):
            payload["content"] = str(message.text)
        return payload

    def _record_message_metadata(self, message: Any, *, delivered: bool, source: str) -> None:
        record = self._build_message_metadata(message, delivered=delivered, source=source)
        if record is None:
            return

        existing = self._juhe_cache.get_message(
            message_id=record.get("message_id"),
            appinfo=record.get("appinfo"),
        )
        if existing and existing.get("delivered") and not delivered:
            record["delivered"] = True

        self._juhe_cache.record_message(record)
        if record.get("seq"):
            self._juhe_cache.update_sync_state({"message_sync_key": str(record["seq"])})

    def _build_message_metadata(self, message: Any, *, delivered: bool, source: str) -> dict[str, Any] | None:
        message_id = str(getattr(message, "message_id", "") or "").strip()
        appinfo = str(
            self._raw_message_field(message, "appinfo")
            or self._raw_message_field(message, "app_info")
            or ""
        ).strip()
        if not message_id and not appinfo:
            return None

        return {
            "message_id": message_id,
            "appinfo": appinfo,
            "refer_id": str(self._raw_message_field(message, "refer_id") or "").strip(),
            "conversation_id": str(getattr(message, "conversation_id", "") or "").strip(),
            "seq": str(self._raw_message_field(message, "seq") or "").strip(),
            "content_type": _safe_int(
                self._raw_message_field(message, "content_type", getattr(message, "message_type", 0)),
                default=_safe_int(getattr(message, "message_type", 0), default=0),
            ),
            "sender": str(getattr(message, "sender_id", "") or "").strip(),
            "sender_name": str(getattr(message, "sender_name", "") or "").strip(),
            "receiver": str(self._raw_message_field(message, "receiver") or "").strip(),
            "roomid": str(
                self._raw_message_field(message, "roomid")
                or self._raw_message_field(message, "room_id")
                or ""
            ).strip(),
            "message": self._build_quote_payload(message),
            "text": str(getattr(message, "text", "") or ""),
            "delivered": delivered,
            "source": source,
        }

    def _is_message_already_delivered(self, message: Any) -> bool:
        record = self._juhe_cache.get_message(
            message_id=str(getattr(message, "message_id", "") or "").strip(),
            appinfo=str(self._raw_message_field(message, "appinfo") or "").strip(),
        )
        return bool(record and record.get("delivered"))

    async def _run_startup_sync(self) -> None:
        if not QWSAAS_AVAILABLE:
            return
        client = self._get_sdk_client()
        await self._sync_contacts(client)
        await self._sync_rooms(client)
        await self._sync_tags(client)
        await self._sync_messages(client)

    async def refresh_contacts(self) -> None:
        await self._sync_contacts(self._get_sdk_client())

    async def refresh_rooms(self) -> None:
        await self._sync_rooms(self._get_sdk_client())

    async def refresh_tags(self) -> None:
        await self._sync_tags(self._get_sdk_client())

    async def refresh_messages(self) -> None:
        await self._sync_messages(self._get_sdk_client())

    async def _sync_contacts(self, client: "QwSaasClient") -> None:
        state = self._juhe_cache.load_sync_state()
        contacts_seq = str(state.get("contacts_last_seq") or "")
        body: dict[str, Any] | None = None

        if sync_contact is not None:
            body = await sync_contact(client, seq=contacts_seq, limit=100)
        elif sync_multi_data is not None:
            body = await sync_multi_data(client, business_id=1, seq=contacts_seq, limit=100)

        if not isinstance(body, dict):
            return

        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        contact_list = data.get("contact_list") if isinstance(data.get("contact_list"), list) else []
        last_seq = str(data.get("last_seq") or contacts_seq)
        self._juhe_cache.upsert_contacts(
            [item for item in contact_list if isinstance(item, dict)],
            last_seq=last_seq,
        )

    async def _sync_tags(self, client: "QwSaasClient") -> None:
        if sync_label_list is None:
            return

        state = self._juhe_cache.load_sync_state()
        seq = str(state.get("tags_seq") or "")
        for _ in range(10):
            body = await sync_label_list(client, seq=seq, sync_type=2)
            if not isinstance(body, dict):
                return
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            label_items = data.get("label_items") if isinstance(data.get("label_items"), list) else []
            next_seq = str(data.get("seq") or seq)
            self._juhe_cache.apply_label_items(
                [item for item in label_items if isinstance(item, dict)],
                seq=next_seq,
            )
            seq = next_seq
            if not bool(data.get("has_next")):
                break

    async def _sync_rooms(self, client: "QwSaasClient") -> None:
        if get_room_list is None:
            return

        start_index = 0
        room_ids: list[str] = []
        while True:
            body = await get_room_list(client, start_index=start_index, limit=100)
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            roomdata = data.get("roomdata") if isinstance(data.get("roomdata"), dict) else {}
            datas = roomdata.get("datas") if isinstance(roomdata.get("datas"), list) else []

            for item in datas:
                if not isinstance(item, dict):
                    continue
                room = self._juhe_cache.upsert_room(item)
                if room and room.get("room_id"):
                    room_ids.append(str(room["room_id"]))

            next_start = _safe_int(data.get("next_start"), default=-1)
            total = _safe_int(data.get("total"), default=0)
            if next_start < 0 or next_start == start_index or next_start >= total:
                break
            start_index = next_start

        unique_room_ids = list(dict.fromkeys(room_ids))
        if batch_get_room_detail is not None:
            for room_chunk in _chunked(unique_room_ids, 50):
                if not room_chunk:
                    continue
                detail_body = await batch_get_room_detail(client, room_chunk)
                self._cache_roominfos(detail_body)
        elif sync_room_info is not None:
            room_versions = self._juhe_cache.load_sync_state().get("room_versions", {})
            if not isinstance(room_versions, dict):
                room_versions = {}
            for room_id in unique_room_ids:
                room_body = await sync_room_info(client, room_id, version=_safe_int(room_versions.get(room_id), default=0))
                self._cache_roominfos(room_body)

    def _cache_roominfos(self, body: dict[str, Any] | None) -> None:
        if not isinstance(body, dict):
            return
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        roominfos = data.get("roominfos") if isinstance(data.get("roominfos"), list) else []
        room_versions = self._juhe_cache.load_sync_state().get("room_versions", {})
        if not isinstance(room_versions, dict):
            room_versions = {}

        for roominfo in roominfos:
            if not isinstance(roominfo, dict):
                continue
            info = roominfo.get("info") if isinstance(roominfo.get("info"), dict) else {}
            room = self._juhe_cache.upsert_room(info)
            room_id = str((room or {}).get("room_id") or info.get("roomid") or "").strip()
            members = roominfo.get("members") if isinstance(roominfo.get("members"), list) else []
            if room_id:
                self._juhe_cache.upsert_room_members(
                    room_id,
                    [member for member in members if isinstance(member, dict)],
                )
                if roominfo.get("version") is not None:
                    room_versions[room_id] = _safe_int(roominfo.get("version"), default=0)

        self._juhe_cache.update_sync_state({"room_versions": room_versions})

    async def _sync_messages(self, client: "QwSaasClient") -> None:
        if sync_msg is None:
            return

        sync_key = str(self._juhe_cache.load_sync_state().get("message_sync_key") or "").strip()
        if not sync_key:
            return

        for _ in range(5):
            body = await sync_msg(client, sync_key, limit=100)
            if not isinstance(body, dict):
                return
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            max_sync_key = str(data.get("max_sync_key") or sync_key).strip()
            msg_list = data.get("msg_list") if isinstance(data.get("msg_list"), list) else []
            self._juhe_cache.update_sync_state({"message_sync_key": max_sync_key})

            if msg_list and parse_callback_event is not None:
                parsed = parse_callback_event(
                    {"guid": self._guid, "notify_type": NOTIFY_BATCH_NEW_MESSAGE, "data": msg_list}
                )
                if parsed is not None:
                    for sync_message in parsed.messages:
                        await self._process_sdk_message(sync_message, source="sync")

            sync_key = max_sync_key or sync_key
            if _safe_int(data.get("continue_flag"), default=0) == 0:
                break

    # ------------------------------------------------------------------
    # Policy helpers
    # ------------------------------------------------------------------

    def _is_dm_allowed(self, sender_id: str) -> bool:
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return _entry_matches(self._allow_from, sender_id)
        return True

    def _is_group_allowed(self, chat_id: str, sender_id: str) -> bool:
        if self._group_policy == "disabled":
            return False
        if self._group_policy == "allowlist" and not _entry_matches(self._group_allow_from, chat_id, group=True):
            return False

        group_cfg = self._resolve_group_cfg(chat_id)
        sender_allow = _coerce_list(group_cfg.get("allow_from") or group_cfg.get("allowFrom"))
        if sender_allow:
            return _entry_matches(sender_allow, sender_id)
        return True

    def _passes_mention_gate(self, chat_id: str, at_list: List[str]) -> bool:
        group_cfg = self._resolve_group_cfg(chat_id)
        require_mention = _truthy(
            group_cfg.get("require_mention", group_cfg.get("requireMention")),
            default=False,
        )
        if not require_mention:
            return True
        return bool(at_list)

    def _resolve_group_cfg(self, chat_id: str) -> Dict[str, Any]:
        if not isinstance(self._groups, dict):
            return {}
        target = _normalize_group(chat_id)
        for key, value in self._groups.items():
            if isinstance(value, dict) and _normalize_group(str(key)) == target:
                return value
        wildcard = self._groups.get("*")
        return wildcard if isinstance(wildcard, dict) else {}

    async def _send_unsupported_notice(self, chat_id: str, label: str) -> None:
        notice = f"暂不支持{label}消息，当前仅支持文本消息。"
        result = await self.send(chat_id, notice)
        if not result.success:
            logger.warning(
                "[%s] Failed to send unsupported-type notice to %s: %s",
                self.name,
                chat_id,
                result.error or "unknown error",
            )

    # ------------------------------------------------------------------
    # Outbound messaging
    # ------------------------------------------------------------------

    async def send_room_at(self, conversation_id: str, content: str, at_list: List[Any]) -> Dict[str, Any]:
        if send_room_at is None:
            return {"success": False, "error": "qwsaas send_room_at wrapper is unavailable"}
        try:
            body = await send_room_at(self._get_sdk_client(), conversation_id, content, at_list)
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        return {
            "success": True,
            "message_id": str(body.get("message_id") or data.get("message_id") or ""),
            "raw": body,
        }

    async def send_quote(
        self,
        *,
        conversation_id: str,
        quote: str,
        content: str,
        appinfo: str,
        content_type: int,
        sender: str,
        sender_name: str,
        message: Dict[str, Any],
    ) -> Dict[str, Any]:
        if send_quote_msg is None:
            return {"success": False, "error": "qwsaas send_quote_msg wrapper is unavailable"}
        try:
            body = await send_quote_msg(
                self._get_sdk_client(),
                conversation_id=conversation_id,
                quote=quote,
                content=content,
                appinfo=appinfo,
                content_type=content_type,
                sender=sender,
                sender_name=sender_name,
                message=message,
            )
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        return {
            "success": True,
            "message_id": str(body.get("message_id") or data.get("message_id") or ""),
            "raw": body,
        }

    async def revoke_message(self, conversation_id: str, msgid: str) -> Dict[str, Any]:
        if revoke_msg is None:
            return {"success": False, "error": "qwsaas revoke_msg wrapper is unavailable"}
        try:
            body = await revoke_msg(self._get_sdk_client(), conversation_id, msgid)
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "raw": body}

    async def mark_read(self, conversation_id: str) -> Dict[str, Any]:
        if report_unread is None:
            return {"success": False, "error": "qwsaas report_unread wrapper is unavailable"}
        try:
            body = await report_unread(self._get_sdk_client(), conversation_id)
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "raw": body}

    async def confirm_internal_read(
        self,
        *,
        message_type: int,
        sender: str,
        receiver: str,
        msgid: str,
        roomid: str | None = None,
    ) -> Dict[str, Any]:
        if confirm_msg is None:
            return {"success": False, "error": "qwsaas confirm_msg wrapper is unavailable"}
        try:
            body = await confirm_msg(
                self._get_sdk_client(),
                message_type,
                sender=sender,
                receiver=receiver,
                roomid=roomid,
                msgid=msgid,
            )
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "raw": body}

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        target = str(chat_id or "").strip()
        if not target.upper().startswith(("S:", "R:")):
            return SendResult(success=False, error="Juhe targets must be prefixed with S: for DMs or R: for groups")
        try:
            result = await self._guid_request(
                path="/msg/send_text",
                data={
                    "guid": self._guid,
                    "conversation_id": target,
                    "content": content,
                },
            )
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

        err_code = result.get("err_code")
        if err_code != 0:
            return SendResult(success=False, error=str(result.get("err_msg") or err_code))
        return SendResult(success=True, message_id=str(result.get("message_id") or ""))

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self._send_file_from_source(
            chat_id=chat_id,
            source=image_url,
            file_type=2,
            caption=caption,
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_file_from_source(
            chat_id=chat_id,
            source=image_path,
            file_type=2,
            caption=caption,
            reply_to=reply_to,
            metadata=kwargs.get("metadata"),
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_file_from_source(
            chat_id=chat_id,
            source=video_path,
            file_type=4,
            caption=caption,
            reply_to=reply_to,
            metadata=kwargs.get("metadata"),
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_file_from_source(
            chat_id=chat_id,
            source=audio_path,
            file_type=5,
            caption=caption,
            reply_to=reply_to,
            metadata=kwargs.get("metadata"),
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_file_from_source(
            chat_id=chat_id,
            source=file_path,
            file_type=5,
            caption=caption,
            reply_to=reply_to,
            metadata=kwargs.get("metadata"),
            file_name=file_name,
        )

    async def _send_file_from_source(
        self,
        *,
        chat_id: str,
        source: str,
        file_type: int,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        file_name: Optional[str] = None,
    ) -> SendResult:
        target = str(chat_id or "").strip()
        if not target.upper().startswith(("S:", "R:")):
            return SendResult(success=False, error="Juhe targets must be prefixed with S: for DMs or R: for groups")

        source_text = str(source or "").strip()
        if not source_text:
            return SendResult(success=False, error="Juhe file source is required")
        if not self._private_base_url:
            return SendResult(success=False, error="JUHE_PRIVATE_BASE_URL is required for Juhe file delivery")

        if caption:
            caption_result = await self.send(target, caption, reply_to=reply_to, metadata=metadata)
            if not caption_result.success:
                return caption_result

        staged_cleanup = None
        try:
            upload_source, staged_cleanup = await self._resolve_upload_source(source_text)
        except FileNotFoundError as exc:
            return SendResult(success=False, error=str(exc))
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

        upload_name = file_name or _source_name(source_text)
        try:
            client = self._get_sdk_client()
            body = await self._upload_file_from_url(
                client=client,
                conversation_id=target,
                file_url=upload_source,
                file_name=upload_name,
                file_type=file_type,
            )
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        finally:
            if staged_cleanup is not None:
                try:
                    await staged_cleanup()
                except Exception:
                    logger.warning("[%s] Failed to clean up staged temp object for %s", self.name, source_text, exc_info=True)

        response_data = body.get("data") if isinstance(body.get("data"), dict) else {}
        return SendResult(
            success=True,
            message_id=str(body.get("message_id") or response_data.get("message_id") or ""),
        )

    async def _resolve_upload_source(
        self,
        source: str,
    ) -> Tuple[str, Optional[Callable[[], Awaitable[None]]]]:
        if _is_http_url(source):
            return source, None

        path = Path(source).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Media file not found: {source}")

        return await self._stage_local_file_for_upload(str(path))

    async def _stage_local_file_for_upload(
        self,
        file_path: str,
    ) -> Tuple[str, Callable[[], Awaitable[None]]]:
        missing = self._missing_temp_s3_fields()
        if missing:
            raise RuntimeError(
                "Juhe local file delivery requires temporary S3 staging. Missing: "
                + ", ".join(missing)
            )

        object_key = self._build_temp_s3_object_key(Path(file_path))
        file_url = await asyncio.to_thread(self._upload_local_file_to_temp_s3_sync, file_path, object_key)

        async def _cleanup() -> None:
            await asyncio.to_thread(self._delete_temp_s3_object_sync, object_key)

        return file_url, _cleanup

    def _missing_temp_s3_fields(self) -> List[str]:
        missing = []
        if not self._temp_s3_endpoint_url:
            missing.append("JUHE_S3_ENDPOINT_URL")
        if not self._temp_s3_bucket:
            missing.append("JUHE_S3_BUCKET")
        if not self._temp_s3_access_key:
            missing.append("JUHE_S3_ACCESS_KEY (or QINIU_ACCESS_KEY)")
        if not self._temp_s3_secret_key:
            missing.append("JUHE_S3_SECRET_KEY (or QINIU_SECRET_KEY)")
        return missing

    def _missing_inbound_s3_fields(self) -> List[str]:
        missing = []
        if not self._inbound_s3_endpoint_url:
            missing.append("JUHE_INBOUND_S3_ENDPOINT_URL (or MINIO_ENDPOINT)")
        if not self._inbound_s3_access_key:
            missing.append("JUHE_INBOUND_S3_ACCESS_KEY (or MINIO_ACCESS_KEY)")
        if not self._inbound_s3_secret_key:
            missing.append("JUHE_INBOUND_S3_SECRET_KEY (or MINIO_SECRET_KEY)")
        return missing

    def _build_temp_s3_object_key(self, file_path: Path) -> str:
        suffix = file_path.suffix.lower()
        date_prefix = datetime.now().strftime("%Y/%m/%d")
        token = uuid.uuid4().hex
        return "/".join(part for part in [self._temp_s3_prefix, date_prefix, f"{token}{suffix}"] if part)

    def _build_temp_s3_client(self) -> Any:
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError as exc:  # pragma: no cover - exercised by runtime only
            raise RuntimeError(
                "boto3 is required for Juhe local file staging. Install hermes-agent[juhe] or pip install boto3."
            ) from exc

        return boto3.client(
            "s3",
            endpoint_url=self._temp_s3_endpoint_url,
            aws_access_key_id=self._temp_s3_access_key,
            aws_secret_access_key=self._temp_s3_secret_key,
            region_name=self._temp_s3_region,
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": self._temp_s3_addressing_style},
            ),
        )

    def _build_inbound_s3_client(self) -> Any:
        missing = self._missing_inbound_s3_fields()
        if missing:
            raise RuntimeError(
                "Inbound Juhe private attachment download requires S3-compatible object storage access. Missing: "
                + ", ".join(missing)
            )

        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError as exc:  # pragma: no cover - exercised by runtime only
            raise RuntimeError(
                "boto3 is required for Juhe private inbound attachment downloads. "
                "Install hermes-agent[juhe] or pip install boto3."
            ) from exc

        return boto3.client(
            "s3",
            endpoint_url=self._inbound_s3_endpoint_url,
            aws_access_key_id=self._inbound_s3_access_key,
            aws_secret_access_key=self._inbound_s3_secret_key,
            region_name=self._inbound_s3_region,
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": self._inbound_s3_addressing_style},
            ),
        )

    def _resolve_inbound_s3_bucket_key(self, object_url: str) -> Tuple[str, str]:
        parsed = urlparse(str(object_url or "").strip())
        path = unquote(parsed.path).lstrip("/")
        configured_bucket = str(self._inbound_s3_bucket or "").strip()

        if configured_bucket and path.startswith(f"{configured_bucket}/"):
            return configured_bucket, path[len(configured_bucket) + 1 :]
        if configured_bucket and path and "/" not in path:
            return configured_bucket, path
        if "/" in path:
            bucket, key = path.split("/", 1)
            if bucket and key:
                return bucket, key
        if configured_bucket and path:
            return configured_bucket, path
        raise RuntimeError(f"Unable to derive S3 bucket/key from private object URL: {object_url}")

    def _download_private_object_via_inbound_s3_sync(
        self,
        object_url: str,
        file_name: Any,
        mime_type: Any,
    ) -> Any:
        if DownloadedAttachment is None:
            raise RuntimeError("qwsaas attachment model is unavailable")

        client = self._build_inbound_s3_client()
        bucket, key = self._resolve_inbound_s3_bucket_key(object_url)
        response = client.get_object(Bucket=bucket, Key=key)
        body = response.get("Body")
        if body is None or not hasattr(body, "read"):
            raise RuntimeError("S3 get_object response missing Body stream")

        try:
            data = body.read()
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

        normalized_name = str(file_name or "").strip() or _source_name(object_url)
        content_type = str(response.get("ContentType") or mime_type or "").strip()
        if not content_type:
            guessed, _encoding = mimetypes.guess_type(normalized_name)
            content_type = guessed or "application/octet-stream"

        return DownloadedAttachment(
            data=data,
            file_name=normalized_name,
            content_type=content_type,
        )

    def _upload_local_file_to_temp_s3_sync(self, file_path: str, object_key: str) -> str:
        client = self._build_temp_s3_client()
        content_type, _encoding = mimetypes.guess_type(file_path)
        extra_args = {"ContentType": content_type} if content_type else None
        if extra_args:
            client.upload_file(file_path, self._temp_s3_bucket, object_key, ExtraArgs=extra_args)
        else:
            client.upload_file(file_path, self._temp_s3_bucket, object_key)

        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._temp_s3_bucket, "Key": object_key},
            ExpiresIn=self._temp_s3_url_expires_seconds,
        )

    def _delete_temp_s3_object_sync(self, object_key: str) -> None:
        try:
            client = self._build_temp_s3_client()
            client.delete_object(Bucket=self._temp_s3_bucket, Key=object_key)
        except Exception:
            logger.warning("[%s] Failed to delete staged temp object %s", self.name, object_key, exc_info=True)

    async def _upload_file_from_url(
        self,
        *,
        client: "QwSaasClient",
        conversation_id: str,
        file_url: str,
        file_name: str,
        file_type: int,
    ) -> Dict[str, Any]:
        size_hint = await self._get_remote_file_size_hint(file_url)
        if size_hint is not None and size_hint > SMALL_FILE_LIMIT_BYTES:
            return await send_big_file_from_url(
                client=client,
                conversation_id=conversation_id,
                file_url=file_url,
                file_name=file_name,
                file_type=file_type,
            )

        try:
            return await send_small_file_from_url(
                client=client,
                conversation_id=conversation_id,
                file_url=file_url,
                file_name=file_name,
                file_type=file_type,
            )
        except QwSaasError:
            if size_hint is None:
                logger.info("[%s] small upload failed for %s, retrying big upload", self.name, file_url)
                return await send_big_file_from_url(
                    client=client,
                    conversation_id=conversation_id,
                    file_url=file_url,
                    file_name=file_name,
                    file_type=file_type,
                )
            raise

    async def _get_remote_file_size_hint(self, file_url: str) -> Optional[int]:
        if not _is_http_url(file_url):
            return None
        if not HTTPX_AVAILABLE:
            return None

        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                response = await client.head(file_url)
                content_length = response.headers.get("Content-Length")
                if response.status_code < 400 and content_length:
                    return int(content_length)

                async with client.stream("GET", file_url, headers={"Range": "bytes=0-0"}) as stream:
                    content_range = stream.headers.get("Content-Range", "")
                    if stream.status_code == 206 and "/" in content_range:
                        total_size = content_range.rsplit("/", 1)[-1].strip()
                        if total_size.isdigit():
                            return int(total_size)
                    content_length = stream.headers.get("Content-Length")
                    if stream.status_code < 400 and content_length:
                        return int(content_length)
        except Exception:
            logger.debug("[%s] Failed to determine remote file size for %s", self.name, file_url, exc_info=True)
        return None

    async def _guid_request(self, *, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        client = self._get_sdk_client()
        request_data = dict(data)
        request_data.pop("guid", None)
        body = await client._request_public(path, request_data)
        response_data = body.get("data") if isinstance(body.get("data"), dict) else {}
        return {
            "err_code": int(body.get("error_code") or 0),
            "err_msg": str(body.get("error_message") or body.get("errMsg") or ""),
            "message_id": str(body.get("message_id") or response_data.get("message_id") or ""),
            "data": body,
        }

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        chat_type = "group" if str(chat_id).upper().startswith("R:") else "dm"
        return {"name": str(chat_id), "type": chat_type, "chat_id": str(chat_id)}

    def _build_sdk_client(self) -> "QwSaasClient":
        return QwSaasClient(
            app_key=self._app_key,
            app_secret=self._app_secret,
            guid=self._guid,
            private_base_url=self._private_base_url,
            public_base_url=self._base_url,
        )

    def _get_sdk_client(self) -> "QwSaasClient":
        if self._sdk_client is None:
            self._sdk_client = self._build_sdk_client()
        return self._sdk_client
