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
    from qwsaas import JuheWsClient, QwSaasClient, send_big_file_from_url, send_small_file_from_url
    from qwsaas.callbacks import (
        NOTIFY_BATCH_NEW_MESSAGE,
        NOTIFY_NEW_MESSAGE,
        TEXT_MESSAGE_TYPE,
        parse_callback_envelope,
        parse_callback_event,
    )
    from qwsaas.exceptions import QwSaasError

    QWSAAS_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency gate
    JuheWsClient = None  # type: ignore[assignment]
    QwSaasClient = None  # type: ignore[assignment]
    send_big_file_from_url = None  # type: ignore[assignment]
    send_small_file_from_url = None  # type: ignore[assignment]
    parse_callback_envelope = None  # type: ignore[assignment]
    parse_callback_event = None  # type: ignore[assignment]
    QwSaasError = Exception  # type: ignore[assignment]
    QWSAAS_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
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
DEFAULT_TEMP_S3_URL_EXPIRES_SECONDS = 3600
DEFAULT_TEMP_S3_PREFIX = "juhe-temp"


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
            ok = await self._process_sdk_message(message) and ok
        return ok

    async def _process_sdk_message(self, message: Any) -> bool:
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
            chat_type = "group"
            chat_name = chat_id
        else:
            if not self._is_dm_allowed(sender_id):
                if self._trace_payloads:
                    logger.info("[%s] Ignoring DM due to allowlist sender=%s", self.name, sender_id)
                return True
            chat_id = str(message.conversation_id or _prefixed_dm(sender_id))
            chat_type = "dm"
            chat_name = str(message.sender_name or sender_id)

        msg_type = int(message.message_type)
        text = str(message.text or "")
        message_id = str(message.message_id or "")
        if self._dedup.is_duplicate(message_id):
            if self._trace_payloads:
                logger.info("[%s] Dropping duplicate message id=%s", self.name, message_id)
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
            await self._send_unsupported_notice(chat_id, label)
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

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=sender_id,
            user_name=str(message.sender_name or sender_id),
        )
        inbound = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=message.raw_message,
            message_id=message_id,
            timestamp=datetime.now(),
        )
        logger.info("[%s] inbound from=%s chat=%s type=%s", self.name, sender_id, chat_id, chat_type)
        await self.handle_message(inbound)
        return True

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
