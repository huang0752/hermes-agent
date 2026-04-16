"""Juhe platform adapter.

Connects Hermes Agent to enterprise WeChat conversations through the juhebot
aggregate-chat gateway.  Phase 1 intentionally supports only one account,
WeWork/enterprise WeChat, WebSocket callbacks, and text messages.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
from datetime import datetime
from typing import Any, Dict, List, Optional

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

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.platforms.helpers import MessageDeduplicator

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://chat-api.juhebot.com"
DEFAULT_WS_URL = "wss://chat-api.juhebot.com/ws/juwe"

NOTIFY_NEW_MESSAGE = 11010
NOTIFY_BATCH_NEW_MESSAGE = 11013
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
REQUEST_TIMEOUT_SECONDS = 30.0
RECONNECT_BACKOFF_SECONDS = [2, 5, 10, 30, 60]


def check_juhe_requirements() -> bool:
    """Return True when Juhe runtime dependencies are available."""
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE


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

        self._session: Optional["aiohttp.ClientSession"] = None
        self._ws: Optional["aiohttp.ClientWebSocketResponse"] = None
        self._http_client: Optional["httpx.AsyncClient"] = None
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
        if not self._app_key or not self._app_secret or not self._guid:
            message = "Juhe startup failed: JUHE_APP_KEY, JUHE_APP_SECRET, and JUHE_GUID are required"
            self._set_fatal_error("juhe_missing_credentials", message, retryable=True)
            logger.warning("[%s] %s", self.name, message)
            return False

        try:
            self._http_client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True)
            await self._open_connection()
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

    async def _open_connection(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self._ws_url, heartbeat=30)
        await self._ws.send_json({
            "type": "auth",
            "app_key": self._app_key,
            "app_secret": self._app_secret,
            "guid": self._guid,
        })

    async def _cleanup(self) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    async def _close_ws_only(self) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

    async def _listen_loop(self) -> None:
        backoff_index = 0
        while self._running:
            try:
                if self._ws is None or self._ws.closed:
                    await self._open_connection()
                msg = await self._ws.receive()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    backoff_index = 0
                    payload = json.loads(msg.data)
                    await self._dispatch_ws_payload(payload)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    raise RuntimeError("Juhe WebSocket closed")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self._running:
                    break
                logger.warning("[%s] WebSocket receive/reconnect error: %s", self.name, exc)
                await self._close_ws_only()
                delay = RECONNECT_BACKOFF_SECONDS[min(backoff_index, len(RECONNECT_BACKOFF_SECONDS) - 1)]
                backoff_index = min(backoff_index + 1, len(RECONNECT_BACKOFF_SECONDS) - 1)
                await asyncio.sleep(delay + random.uniform(0, 0.5))

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

        event_id = payload.get("event_id")
        event = self._coerce_callback_event(payload.get("event"))
        if event is None:
            event = self._coerce_callback_event(payload.get("data"))
        if event is None and isinstance(payload, dict):
            event = self._coerce_callback_event(payload)
        if self._trace_payloads:
            logger.info(
                "[%s] callback envelope event_id=%s parsed_event=%s",
                self.name,
                event_id,
                _preview_json(event),
            )
        success = await self._handle_callback_event(event)
        await self._send_ack(event_id, success)
        return success

    async def _send_ack(self, event_id: Any, success: bool) -> None:
        if not self._ws or self._ws.closed:
            return
        await self._ws.send_json({
            "type": "ack",
            "event_id": event_id,
            "success": success,
        })

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

        data = event.get("data")
        if notify_type == NOTIFY_BATCH_NEW_MESSAGE and isinstance(data, list):
            messages = data
        elif isinstance(data, list):
            messages = data
        elif isinstance(data, dict):
            messages = [data]
        else:
            logger.warning(
                "[%s] Callback notify_type=%s has unsupported data payload type=%s",
                self.name,
                notify_type,
                type(data).__name__,
            )
            return False

        ok = True
        for message in messages:
            ok = await self._process_message(message, event) and ok
        return ok

    @staticmethod
    def _coerce_callback_event(event: Any) -> Optional[Dict[str, Any]]:
        if isinstance(event, dict):
            return event
        if isinstance(event, str):
            text = event.strip()
            if not text:
                return None
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                return None
            return parsed if isinstance(parsed, dict) else None
        return None

    async def _process_message(self, message: Any, event: Dict[str, Any]) -> bool:
        if not isinstance(message, dict):
            logger.warning("[%s] Ignoring non-dict message payload type=%s", self.name, type(message).__name__)
            return False

        if _is_self_echo_message(message):
            if self._trace_payloads:
                logger.info("[%s] Ignoring self echo message id=%s", self.name, message.get("id") or message.get("seq"))
            return True

        sender_id = self._extract_sender_id(message)
        if not sender_id:
            logger.warning("[%s] Ignoring message without sender: %s", self.name, _preview_json(message))
            return False

        is_group = self._is_group_message(message)
        if is_group:
            raw_chat_id = self._extract_group_id(message)
            if not raw_chat_id:
                logger.warning("[%s] Group message missing group id: %s", self.name, _preview_json(message))
                return False
            chat_id = _prefixed_group(raw_chat_id)
            if not self._is_group_allowed(chat_id, sender_id):
                if self._trace_payloads:
                    logger.info(
                        "[%s] Ignoring group message due to allowlist sender=%s chat=%s",
                        self.name,
                        sender_id,
                        chat_id,
                    )
                return True
            if not self._passes_mention_gate(chat_id, self._extract_at_list(message)):
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
            chat_id = _prefixed_dm(sender_id)
            chat_type = "dm"
            chat_name = str(message.get("sender_name") or sender_id)

        msg_type = self._message_type(message)
        text = self._extract_text(message, msg_type)
        message_id = self._message_id(message, event, sender_id, text)
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
            user_name=str(message.get("sender_name") or sender_id),
        )
        inbound = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=message,
            message_id=message_id,
            timestamp=datetime.now(),
        )
        logger.info("[%s] inbound from=%s chat=%s type=%s", self.name, sender_id, chat_id, chat_type)
        await self.handle_message(inbound)
        return True

    @staticmethod
    def _message_type(message: Dict[str, Any]) -> int:
        try:
            return int(
                message.get("msg_type")
                or message.get("msgtype")
                or message.get("content_type")
                or message.get("type")
                or TEXT_MESSAGE_TYPE
            )
        except (TypeError, ValueError):
            return TEXT_MESSAGE_TYPE

    @staticmethod
    def _extract_sender_id(message: Dict[str, Any]) -> str:
        from_user = message.get("from_user") if isinstance(message.get("from_user"), dict) else {}
        sender = (
            message.get("chatroom_sender")
            or message.get("from_username")
            or message.get("sender")
            or message.get("sender_id")
            or message.get("wxid")
            or from_user.get("id")
            or ""
        )
        return _strip_target_prefix(str(sender).strip())

    @staticmethod
    def _extract_group_id(message: Dict[str, Any]) -> str:
        group_id = (
            message.get("chat_id")
            or message.get("room_id")
            or message.get("roomid")
            or message.get("chatroom")
            or ""
        )
        if _is_zero_like_id(group_id):
            return ""
        return _strip_target_prefix(str(group_id).strip())

    @staticmethod
    def _is_group_message(message: Dict[str, Any]) -> bool:
        if message.get("is_group") or message.get("is_chatroom_msg"):
            return True
        return bool(JuheAdapter._extract_group_id(message))

    @staticmethod
    def _extract_at_list(message: Dict[str, Any]) -> List[str]:
        at_list = message.get("at_list") or []
        if isinstance(at_list, str):
            return [item.strip() for item in at_list.split(",") if item.strip()]
        if isinstance(at_list, (list, tuple, set)):
            return [str(item).strip() for item in at_list if str(item).strip()]
        return []

    @staticmethod
    def _extract_text(message: Dict[str, Any], msg_type: int) -> str:
        if msg_type != TEXT_MESSAGE_TYPE:
            return ""
        raw = message.get("content")
        if raw is None:
            raw = message.get("msg", "")
        if isinstance(raw, dict):
            return str(raw.get("msg") or raw.get("text") or "").strip()
        text = str(raw or "")
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return text.strip()
        if isinstance(parsed, dict):
            nested = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
            return str(parsed.get("msg") or parsed.get("text") or nested.get("msg") or "").strip()
        return text.strip()

    @staticmethod
    def _message_id(message: Dict[str, Any], event: Dict[str, Any], sender_id: str, text: str) -> str:
        for key in ("msg_id", "msgid", "message_id", "id", "seq"):
            value = message.get(key)
            if value:
                return str(value)
        stable = "|".join([
            str(event.get("guid") or ""),
            str(message.get("timestamp") or message.get("sendtime") or ""),
            sender_id,
            str(message.get("roomid") or message.get("room_id") or message.get("chat_id") or ""),
            text,
        ])
        return hashlib.sha1(stable.encode("utf-8")).hexdigest()

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

    async def _guid_request(self, *, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        owns_client = False
        client = self._http_client
        if client is None:
            client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True)
            owns_client = True
        try:
            response = await client.post(
                f"{self._base_url}/open/GuidRequest",
                json={
                    "app_key": self._app_key,
                    "app_secret": self._app_secret,
                    "path": path,
                    "data": data,
                },
            )
            response.raise_for_status()
            body = response.json()
        finally:
            if owns_client:
                await client.aclose()

        base_response = body.get("baseResponse") if isinstance(body, dict) else {}
        err_code = None
        if isinstance(body, dict):
            if isinstance(body.get("list"), list) and body["list"]:
                err_code = body["list"][0].get("ret")
            if err_code is None and isinstance(base_response, dict):
                err_code = base_response.get("ret")
            if err_code is None:
                err_code = body.get("error_code", 0)
        return {
            "err_code": 0 if err_code is None else err_code,
            "err_msg": (
                base_response.get("errMsg") if isinstance(base_response, dict) else None
            ) or (body.get("errMsg") if isinstance(body, dict) else None),
            "data": body,
        }

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        chat_type = "group" if str(chat_id).upper().startswith("R:") else "dm"
        return {"name": str(chat_id), "type": chat_type, "chat_id": str(chat_id)}
