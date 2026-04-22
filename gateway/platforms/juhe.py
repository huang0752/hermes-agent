"""Juhe platform adapter.

Connects Hermes Agent to enterprise WeChat conversations through the juhebot
aggregate-chat gateway. Supports one account, WebSocket callbacks, text
messages, and file delivery from externally reachable HTTP(S) URLs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlparse

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
        get_cdn_info,
        get_wwfile_download_info,
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
        QwSaasResponseError,
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
    get_cdn_info = None  # type: ignore[assignment]
    get_wwfile_download_info = None  # type: ignore[assignment]
    parse_callback_envelope = None  # type: ignore[assignment]
    parse_callback_event = None  # type: ignore[assignment]
    QwSaasError = Exception  # type: ignore[assignment]
    QwSaasPrivateObjectAccessError = Exception  # type: ignore[assignment]
    QwSaasRequestError = Exception  # type: ignore[assignment]
    QwSaasResponseError = Exception  # type: ignore[assignment]
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
from gateway.delivery_rules import (
    JUHE_CERTIFICATE_REMOTE_DELIVERY_ERROR,
    extract_http_urls,
    is_certificate_render_job_url,
)
from gateway.juhe_access_control import JuheAccessControlStore
from gateway.juhe_cache import JuheCacheStore
from gateway.juhe_room_memory import (
    DEFAULT_ROOM_MEMORY_CHAR_LIMIT,
    load_room_memory,
    schedule_room_memory_update,
)
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
    cache_document_from_bytes,
    cache_image_from_bytes,
    media_reference_suffix,
    safe_url_for_log,
)
from gateway.platforms.helpers import MessageDeduplicator, strip_markdown
from hermes_constants import get_hermes_home
from hermes_state import SessionDB

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
DEFAULT_INBOUND_S3_URL_EXPIRES_SECONDS = 3600
DEFAULT_TEMP_S3_URL_EXPIRES_SECONDS = 3600
DEFAULT_TEMP_S3_PREFIX = "juhe-temp"
DEFAULT_ROOM_LOG_LIMIT = 500
DEFAULT_PENDING_CONTEXT_LIMIT = 20
DEFAULT_PENDING_CONTEXT_CHAR_LIMIT = 4000
DEFAULT_ATTACHMENT_PREHEAT_MAX_BYTES = 3 * 1024 * 1024
DEFAULT_ATTACHMENT_PREHEAT_WORKERS = 2
DEFAULT_OUTBOUND_DOCUMENT_DOWNLOAD_TIMEOUT_SECONDS = 120.0
DEFAULT_OUTBOUND_DOCUMENT_DOWNLOAD_CONNECT_TIMEOUT_SECONDS = 15.0
DEFAULT_OUTBOUND_DOCUMENT_DOWNLOAD_RETRIES = 3
SUPPORTED_ATTACHMENT_MESSAGE_TYPES = {MSG_TYPE_IMAGE, MSG_TYPE_VOICE, MSG_TYPE_VIDEO, MSG_TYPE_FILE}
TEXTUAL_MENTION_RE = re.compile(r"(^|[\s\u2000-\u200b\u202f\u205f\u3000])@([^\s@]{1,64})")
MENTION_TOKEN_TRIM_CHARS = ",，:：;；!！?？"
JUHE_MEDIA_DIRECTIVE_RE = re.compile(r"(?im)^[ \t]*MEDIA:\s*.+$")
JUHE_INSTRUCTION_LEAK_MARKERS = (
    "artifact, or equivalent non-link deliverable.",
    "If any business meaning is uncertain, stop and ask directly.",
    "Do not guess templates, certificate names, professional terms, logo choices, or multi-date meanings.",
    "Prefer `certificate_workflow_tool` as the standard execution surface.",
    "Do not use execute_code with Python `urllib.request` to fetch delivery artifacts.",
    "Do not expose raw download links to end users.",
)
SIGNED_URL_QUERY_KEYS = {
    "accesskeyid",
    "awsaccesskeyid",
    "expires",
    "signature",
    "x-goog-algorithm",
    "x-goog-credential",
    "x-goog-signature",
    "x-oss-accesskeyid",
    "x-oss-signature",
}


class _ResolvedAttachmentAccessTarget(SimpleNamespace):
    url: str
    object_url: str


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


def _strip_visible_markdown_for_juhe(content: Any) -> str:
    """Render outbound Juhe text as plain text while preserving MEDIA directives."""
    text = str(content or "")
    if not text:
        return ""

    preserved_media_lines: list[str] = []

    def _preserve_media(match: re.Match[str]) -> str:
        token = f"HERMESJUHEMEDIATOKEN{len(preserved_media_lines)}"
        preserved_media_lines.append(match.group(0).strip())
        return token

    cleaned = JUHE_MEDIA_DIRECTIVE_RE.sub(_preserve_media, text)
    cleaned = strip_markdown(cleaned)

    for index, media_line in enumerate(preserved_media_lines):
        cleaned = cleaned.replace(f"HERMESJUHEMEDIATOKEN{index}", media_line)

    return _strip_instruction_leakage_for_juhe(cleaned)


def _strip_instruction_leakage_for_juhe(content: Any) -> str:
    """Trim leaked internal skill/prompt instructions from Juhe-facing text."""
    text = str(content or "")
    if not text:
        return ""

    leak_points = [text.find(marker) for marker in JUHE_INSTRUCTION_LEAK_MARKERS if marker in text]
    for pattern in (
        r"(?im)^[ \t-]*If any business meaning is uncertain, stop and ask directly\..*$",
        r"(?im)^[ \t-]*Do not guess templates, certificate names, professional terms, logo choices, or multi-date meanings\..*$",
        r"(?im)^[ \t-]*Prefer `certificate_workflow_tool` as the standard execution surface\..*$",
        r"(?im)^[ \t-]*Do not use execute_code with Python `urllib\.request` to fetch delivery artifacts\..*$",
        r"(?im)^[ \t-]*Do not expose raw download links to end users\..*$",
    ):
        match = re.search(pattern, text)
        if match:
            leak_points.append(match.start())
    if not leak_points:
        return text.strip()

    cleaned = text[: min(leak_points)].rstrip()
    cleaned = re.sub(r"(?:\\n|\n){3,}", "\n\n", cleaned)
    return cleaned.strip()


def _strip_certificate_delivery_links_for_juhe(content: Any) -> str:
    """Remove raw certificate render-job URLs from outbound Juhe text."""
    text = str(content or "")
    if not text:
        return ""

    cleaned = text
    for url in extract_http_urls(text):
        if not is_certificate_render_job_url(url):
            continue
        cleaned = cleaned.replace(url, "")

    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"(?m)^[ \t]+$", "", cleaned)
    return cleaned.strip()


def _strip_local_delivery_paths_for_juhe(content: Any) -> str:
    """Remove visible local artifact paths from outbound Juhe text."""
    text = str(content or "")
    if not text:
        return ""

    cleaned = text
    patterns = (
        r"(?im)^[ \t]*本地路径[：:]\s*.+$",
        r"(?im)^[ \t]*local (?:filesystem )?path[：:]\s*.+$",
    )
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned)

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"(?m)^[ \t]+$", "", cleaned)
    return cleaned.strip()


def _has_textual_mention_fallback(text: Any) -> bool:
    value = str(text or "")
    if not value:
        return False
    return bool(TEXTUAL_MENTION_RE.search(value))


def _normalize_mention_token(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("@"):
        text = text[1:].strip()
    text = text.strip(MENTION_TOKEN_TRIM_CHARS)
    return text.casefold()


def _extract_textual_mentions(text: Any) -> List[str]:
    value = str(text or "")
    if not value:
        return []

    mentions: List[str] = []
    seen: set[str] = set()
    for match in TEXTUAL_MENTION_RE.finditer(value):
        token = _normalize_mention_token(match.group(2))
        if not token or token in seen:
            continue
        seen.add(token)
        mentions.append(token)
    return mentions


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


def _looks_like_private_media_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return host.endswith("weixin.qq.com") or "tpdownloadmedia" in path


def _looks_like_inbound_proxy_download_path(value: str) -> bool:
    path = str(value or "").strip().lower()
    return path.startswith("/cgi-bin/") or "tpdownloadmedia" in path


def _looks_like_public_qpic_url(value: str) -> bool:
    host = urlparse(str(value or "").strip()).netloc.lower()
    return host.startswith("wework.qpic.cn") or host.endswith(".qpic.cn")


def _looks_like_c2c_file_id(value: str) -> bool:
    return str(value or "").strip().startswith("30")


def _looks_like_big_file_id(value: str) -> bool:
    return str(value or "").strip().startswith("*")


def _source_name(value: str, default: str = "attachment") -> str:
    text = str(value or "").strip()
    if not text:
        return default
    if _is_http_url(text):
        path = unquote(urlparse(text).path or "")
        name = Path(path).name
        return name or default
    return Path(text).name or default


def _format_exception_detail(exc: Exception) -> str:
    detail = str(exc).strip()
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        cause_detail = str(cause).strip() or cause.__class__.__name__
        if cause_detail and cause_detail not in detail:
            detail = f"{detail} ({cause_detail})" if detail else cause_detail
    if not detail:
        detail = exc.__class__.__name__
    return detail


def _should_retry_big_upload_after_small_failure(
    exc: Exception,
    *,
    size_hint: Optional[int],
    file_type: int,
) -> bool:
    detail = _format_exception_detail(exc).lower()
    transient_markers = (
        "timeout",
        "timed out",
        "readtimeout",
        "remoteprotocolerror",
        "connection reset",
        "connection aborted",
        "temporarily unavailable",
        "503",
        "504",
        "502",
        "http error",
    )
    if size_hint is None:
        return True
    if file_type == 5:
        return True
    return any(marker in detail for marker in transient_markers)


def _looks_like_signed_http_url(value: str) -> bool:
    text = str(value or "").strip()
    if not _is_http_url(text):
        return False
    parsed = urlparse(text)
    if parsed.username or parsed.password:
        return True
    query_keys = {key.lower() for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
    if any(key.startswith("x-amz-") for key in query_keys):
        return True
    return bool(query_keys & SIGNED_URL_QUERY_KEYS)


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


def _coerce_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            loaded = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if isinstance(loaded, dict):
            return loaded
    return {}


def _normalize_attachment_mime(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if "/" in text else ""


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
        inbound_s3_expires_raw = extra.get(
            "inbound_s3_url_expires_seconds",
            os.getenv(
                "JUHE_INBOUND_S3_URL_EXPIRES_SECONDS",
                str(DEFAULT_INBOUND_S3_URL_EXPIRES_SECONDS),
            ),
        )
        try:
            self._inbound_s3_url_expires_seconds = max(60, int(inbound_s3_expires_raw))
        except (TypeError, ValueError):
            self._inbound_s3_url_expires_seconds = DEFAULT_INBOUND_S3_URL_EXPIRES_SECONDS
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
        mention_targets = extra.get("mention_targets")
        if mention_targets is None:
            mention_targets = os.getenv("JUHE_MENTION_TARGETS", "")
        self._mention_targets = _coerce_list(mention_targets)
        self._normalized_mention_targets = {
            token for item in self._mention_targets if (token := _normalize_mention_token(item))
        }
        trigger_user_ids = extra.get("trigger_user_ids")
        if trigger_user_ids is None:
            trigger_user_ids = os.getenv("JUHE_TRIGGER_USER_IDS", "")
        self._trigger_user_ids = _coerce_list(trigger_user_ids)
        if "group_sessions_per_user" not in self.config.extra:
            self.config.extra["group_sessions_per_user"] = False
        room_log_limit_raw = extra.get("room_log_limit", os.getenv("JUHE_ROOM_LOG_LIMIT", str(DEFAULT_ROOM_LOG_LIMIT)))
        try:
            self._room_log_limit = max(1, int(room_log_limit_raw))
        except (TypeError, ValueError):
            self._room_log_limit = DEFAULT_ROOM_LOG_LIMIT
        pending_context_limit_raw = extra.get(
            "pending_context_limit",
            os.getenv("JUHE_PENDING_CONTEXT_LIMIT", str(DEFAULT_PENDING_CONTEXT_LIMIT)),
        )
        try:
            self._pending_context_limit = max(1, int(pending_context_limit_raw))
        except (TypeError, ValueError):
            self._pending_context_limit = DEFAULT_PENDING_CONTEXT_LIMIT
        room_memory_char_limit_raw = extra.get(
            "room_memory_char_limit",
            os.getenv("JUHE_ROOM_MEMORY_CHAR_LIMIT", str(DEFAULT_ROOM_MEMORY_CHAR_LIMIT)),
        )
        try:
            self._room_memory_char_limit = max(200, int(room_memory_char_limit_raw))
        except (TypeError, ValueError):
            self._room_memory_char_limit = DEFAULT_ROOM_MEMORY_CHAR_LIMIT
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
        self._access_control_store = JuheAccessControlStore(
            fallback_dm_allow_from=self._allow_from,
            fallback_group_trigger_user_ids=self._trigger_user_ids,
        )
        self._session_db = SessionDB(db_path=get_hermes_home() / "state.db")
        self._attachment_preheat_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._attachment_preheat_workers: list[asyncio.Task] = []
        self._attachment_preheat_pending_keys: set[str] = set()
        attachment_preheat_limit_raw = extra.get(
            "attachment_preheat_max_bytes",
            os.getenv("JUHE_ATTACHMENT_PREHEAT_MAX_BYTES", str(DEFAULT_ATTACHMENT_PREHEAT_MAX_BYTES)),
        )
        try:
            self._attachment_preheat_max_bytes = max(0, int(attachment_preheat_limit_raw))
        except (TypeError, ValueError):
            self._attachment_preheat_max_bytes = DEFAULT_ATTACHMENT_PREHEAT_MAX_BYTES
        attachment_preheat_workers_raw = extra.get(
            "attachment_preheat_workers",
            os.getenv("JUHE_ATTACHMENT_PREHEAT_WORKERS", str(DEFAULT_ATTACHMENT_PREHEAT_WORKERS)),
        )
        try:
            self._attachment_preheat_worker_count = max(1, int(attachment_preheat_workers_raw))
        except (TypeError, ValueError):
            self._attachment_preheat_worker_count = DEFAULT_ATTACHMENT_PREHEAT_WORKERS

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
        await self._shutdown_attachment_preheat_workers()
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

    def _ensure_attachment_preheat_workers(self) -> None:
        running_workers = [task for task in self._attachment_preheat_workers if not task.done()]
        self._attachment_preheat_workers = running_workers
        while len(self._attachment_preheat_workers) < self._attachment_preheat_worker_count:
            self._attachment_preheat_workers.append(asyncio.create_task(self._attachment_preheat_worker()))

    async def _shutdown_attachment_preheat_workers(self) -> None:
        workers = [task for task in self._attachment_preheat_workers if not task.done()]
        self._attachment_preheat_workers = []
        if not workers:
            return
        for _ in workers:
            await self._attachment_preheat_queue.put(None)
        for task in workers:
            task.cancel()
        for task in workers:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _attachment_preheat_worker(self) -> None:
        while True:
            job = await self._attachment_preheat_queue.get()
            if job is None:
                self._attachment_preheat_queue.task_done()
                return
            try:
                await self._run_attachment_preheat_job(job)
            except Exception as exc:
                logger.warning("[%s] Juhe attachment preheat failed: %s", self.name, exc, exc_info=True)
            finally:
                dedupe_key = str(job.get("dedupe_key") or "").strip()
                if dedupe_key:
                    self._attachment_preheat_pending_keys.discard(dedupe_key)
                self._attachment_preheat_queue.task_done()

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

        message_id = str(message.message_id or "")
        refer_id = str(self._raw_message_field(message, "refer_id") or "").strip()
        appinfo = str(
            self._raw_message_field(message, "appinfo")
            or self._raw_message_field(message, "app_info")
            or ""
        ).strip()
        seq = str(self._raw_message_field(message, "seq") or "").strip()
        created_at = self._message_created_at(message)
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
        if self._dedup.is_duplicate(message_id):
            if self._trace_payloads:
                logger.info("[%s] Dropping duplicate message id=%s", self.name, message_id)
            return True
        if self._is_message_already_delivered(message):
            if self._trace_payloads:
                logger.info("[%s] Dropping previously delivered message id=%s", self.name, message_id)
            return True
        if not _is_zero_like_id(refer_id):
            if self._trace_payloads:
                logger.info(
                    "[%s] Ignoring secondary callback id=%s refer_id=%s",
                    self.name,
                    message_id,
                    refer_id,
                )
            return True

        message_source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=sender_id,
            user_name=str(message.sender_name or sender_id),
        )
        if is_group and not _truthy(self.config.extra.get("group_sessions_per_user"), default=False):
            setattr(message_source, "shared_session", True)

        should_trigger_group_reply = (
            self._should_trigger_group_reply(chat_id, sender_id, list(message.at_list), text)
            if is_group
            else False
        )
        if is_group:
            self._append_room_message(
                conversation_id=chat_id,
                message_id=message_id,
                appinfo=appinfo,
                refer_id=refer_id,
                seq=seq,
                sender_id=sender_id,
                sender_name=str(message.sender_name or sender_id),
                direction="inbound",
                message_type=msg_type,
                text_preview=self._room_message_preview(message),
                raw_payload=message.raw_message,
                created_at=created_at,
                triggered=should_trigger_group_reply,
                consumed_for_context=should_trigger_group_reply,
            )
            if not should_trigger_group_reply:
                if msg_type in SUPPORTED_ATTACHMENT_MESSAGE_TYPES:
                    await self._schedule_buffered_group_attachment_preheat(message)
                if self._trace_payloads:
                    logger.info(
                        "[%s] Buffered non-trigger group message sender=%s chat=%s",
                        self.name,
                        sender_id,
                        chat_id,
                    )
                return True
            pending_messages: List[Dict[str, Any]] = []
        else:
            pending_messages = []

        if msg_type in SUPPORTED_ATTACHMENT_MESSAGE_TYPES:
            inbound = await self._build_attachment_event(message=message, source=message_source)
            if is_group:
                pending_messages = self._session_db.get_unconsumed_room_messages(
                    self.platform.value,
                    chat_id,
                    limit=self._pending_context_limit,
                    char_limit=DEFAULT_PENDING_CONTEXT_CHAR_LIMIT,
                )
                if pending_messages:
                    self._session_db.mark_room_messages_consumed(
                        self.platform.value,
                        chat_id,
                        [item["id"] for item in pending_messages],
                    )
                pending_context_text = self._format_pending_room_context(pending_messages)
                if pending_context_text:
                    setattr(inbound, "_juhe_pending_context_text", pending_context_text)
                room_memory = load_room_memory(chat_id)
                if room_memory:
                    setattr(inbound, "_juhe_room_memory_text", room_memory)
                await self._rehydrate_pending_room_attachments(
                    inbound=inbound,
                    pending_messages=pending_messages,
                )
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
        if is_group:
            pending_messages = self._session_db.get_unconsumed_room_messages(
                self.platform.value,
                chat_id,
                limit=self._pending_context_limit,
                char_limit=DEFAULT_PENDING_CONTEXT_CHAR_LIMIT,
            )
            if pending_messages:
                self._session_db.mark_room_messages_consumed(
                    self.platform.value,
                    chat_id,
                    [item["id"] for item in pending_messages],
                )
            pending_context_text = self._format_pending_room_context(pending_messages)
            if pending_context_text:
                setattr(inbound, "_juhe_pending_context_text", pending_context_text)
            room_memory = load_room_memory(chat_id)
            if room_memory:
                setattr(inbound, "_juhe_room_memory_text", room_memory)
            await self._rehydrate_pending_room_attachments(
                inbound=inbound,
                pending_messages=pending_messages,
            )
        logger.info("[%s] inbound from=%s chat=%s type=%s", self.name, sender_id, chat_id, chat_type)
        await self.handle_message(inbound)
        self._record_message_metadata(message, delivered=True, source=source)
        return True

    async def _schedule_buffered_group_attachment_preheat(self, message: Any) -> None:
        attachment_name = self._attachment_name(message)
        resolved_media_type = self._attachment_mime_type(message)
        attachment_type = self._attachment_message_type(message, content_type=resolved_media_type)
        if not self._attachment_supports_url_only_pipeline(
            attachment_type=attachment_type,
            media_type=resolved_media_type,
            file_name=attachment_name,
        ):
            return

        try:
            access_target = await self._resolve_inbound_attachment_access_target(
                message,
                attachment_type=attachment_type,
                attachment_name=attachment_name,
                media_type=resolved_media_type,
            )
            identity = self._build_inbound_attachment_identity(
                message,
                resolved_url=access_target.url,
                object_url=access_target.object_url,
            )
            access_url = self._maybe_generate_inbound_access_url(
                access_target.url,
                object_url=access_target.object_url,
            )
            if not access_url:
                return
            self._schedule_attachment_preheat(
                access_url=access_url,
                identity=identity,
                media_type=resolved_media_type,
                display_name=attachment_name,
                attachment_type=attachment_type,
                file_size=self._attachment_size_hint(message),
            )
        except Exception:
            logger.debug(
                "[%s] Skipping buffered group attachment preheat message_id=%s",
                self.name,
                getattr(message, "message_id", ""),
                exc_info=True,
            )

    async def _build_attachment_event(self, *, message: Any, source: Any) -> MessageEvent:
        attachment_name = self._attachment_name(message)
        resolved_media_type = self._attachment_mime_type(message)
        attachment_type = self._attachment_message_type(message, content_type=resolved_media_type)
        original_text = str(message.text or "").strip()
        attachment_size = self._attachment_size_hint(message)
        media_urls: List[str] = []
        media_types: List[str] = []
        preferred_media_access_urls: List[str] = []
        attachment_identities: List[Dict[str, Any]] = []
        media_descriptions: List[str] = []

        if self._attachment_supports_url_only_pipeline(
            attachment_type=attachment_type,
            media_type=resolved_media_type,
            file_name=attachment_name,
        ):
            try:
                access_target = await self._resolve_inbound_attachment_access_target(
                    message,
                    attachment_type=attachment_type,
                    attachment_name=attachment_name,
                    media_type=resolved_media_type,
                )
                identity = self._build_inbound_attachment_identity(
                    message,
                    resolved_url=access_target.url,
                    object_url=access_target.object_url,
                )
                access_url = self._maybe_generate_inbound_access_url(
                    access_target.url,
                    object_url=access_target.object_url,
                )
                if not access_url:
                    raise RuntimeError("remote access URL unavailable")
                media_types = [resolved_media_type]
                media_urls = [self._build_inbound_attachment_media_ref(attachment_name, identity)]
                preferred_media_access_urls = [access_url]
                attachment_identities = [identity]
                if attachment_type == MessageType.PHOTO:
                    media_descriptions = [f"图片 {attachment_name}：已收到，但暂未生成描述"]
                elif attachment_type == MessageType.DOCUMENT:
                    media_descriptions = [f"文件 {attachment_name}：已收到，待解析"]
                self._schedule_attachment_preheat(
                    access_url=access_url,
                    identity=identity,
                    media_type=resolved_media_type,
                    display_name=attachment_name,
                    attachment_type=attachment_type,
                    file_size=attachment_size,
                )
                text = original_text or self._attachment_notice_text(attachment_type, attachment_name)
            except Exception as exc:
                logger.warning(
                    "[%s] Failed to resolve URL-only inbound attachment message_id=%s kind=%s: %s",
                    self.name,
                    getattr(message, "message_id", ""),
                    self._attachment_kind(message) or getattr(message, "attachment_kind", None),
                    exc,
                )
                download = None
                try:
                    download = await self._download_inbound_attachment(message)
                    attachment_name = str(download.file_name or attachment_name).strip() or attachment_name
                    resolved_media_type = self._attachment_mime_type(message, content_type=download.content_type)
                    attachment_type = self._attachment_message_type(message, content_type=resolved_media_type)
                    media_types = [resolved_media_type]
                    media_urls = [self._cache_inbound_attachment(download.data, attachment_name, attachment_type)]
                    preferred_access_url = self._maybe_generate_inbound_access_url(
                        media_urls[0],
                        object_url=self._attachment_private_object_url(message, download=download),
                    )
                    if preferred_access_url and preferred_access_url != media_urls[0]:
                        preferred_media_access_urls = [preferred_access_url]
                    if attachment_type == MessageType.PHOTO:
                        media_descriptions = [f"图片 {attachment_name}：已收到，但暂未生成描述"]
                    elif attachment_type == MessageType.DOCUMENT:
                        media_descriptions = [f"文件 {attachment_name}：已收到，待解析"]
                    text = original_text or self._attachment_notice_text(attachment_type, attachment_name)
                except Exception as download_exc:
                    fallback = self._attachment_metadata_only_text(attachment_type, attachment_name, download_exc)
                    text = self._combine_attachment_text(original_text, fallback)
        else:
            download = None
            try:
                download = await self._download_inbound_attachment(message)
                attachment_name = str(download.file_name or attachment_name).strip() or attachment_name
                resolved_media_type = self._attachment_mime_type(message, content_type=download.content_type)
                attachment_type = self._attachment_message_type(message, content_type=resolved_media_type)
                media_types = [resolved_media_type]
                media_urls = [self._cache_inbound_attachment(download.data, attachment_name, attachment_type)]
                preferred_access_url = self._maybe_generate_inbound_access_url(
                    media_urls[0],
                    object_url=self._attachment_private_object_url(message, download=download),
                )
                if preferred_access_url and preferred_access_url != media_urls[0]:
                    preferred_media_access_urls = [preferred_access_url]
                if attachment_type == MessageType.PHOTO:
                    media_descriptions = [f"图片 {attachment_name}：已收到，但暂未生成描述"]
                elif attachment_type == MessageType.DOCUMENT:
                    media_descriptions = [f"文件 {attachment_name}：已收到，待解析"]
                text = original_text or self._attachment_notice_text(attachment_type, attachment_name)
            except Exception as exc:
                logger.warning(
                    "[%s] Failed to download inbound attachment message_id=%s kind=%s: %s",
                    self.name,
                    getattr(message, "message_id", ""),
                    self._attachment_kind(message) or getattr(message, "attachment_kind", None),
                    exc,
                )
                fallback = self._attachment_metadata_only_text(attachment_type, attachment_name, exc)
                text = self._combine_attachment_text(original_text, fallback)

        event = MessageEvent(
            text=text,
            message_type=attachment_type,
            source=source,
            raw_message=message.raw_message,
            message_id=str(message.message_id or ""),
            media_urls=media_urls,
            media_types=media_types,
            timestamp=datetime.now(),
        )
        if preferred_media_access_urls:
            setattr(event, "_juhe_media_access_urls", preferred_media_access_urls)
        if attachment_identities:
            setattr(event, "_juhe_attachment_identities", attachment_identities)
        if media_descriptions:
            setattr(event, "_juhe_media_descriptions", media_descriptions)
        return event

    async def _download_inbound_attachment(self, message: Any) -> Any:
        if download_callback_attachment is None:
            raise RuntimeError("qwsaas download helper is unavailable")
        download_url = self._attachment_download_url(message)
        file_id = self._attachment_field(message, "file_id", "fileId", "media_id", "mediaId")
        file_name = self._attachment_name(message)
        file_size_raw = self._attachment_field(message, "file_size", "size", "content_length")
        file_size = None if file_size_raw in (None, "") else _safe_int(file_size_raw, default=0)
        aes_key = self._attachment_field(message, "aes_key", "aesKey")
        auth_key = self._attachment_field(message, "auth_key", "authKey")
        attachment_kind = self._attachment_kind(message) or None
        mime_type = self._attachment_mime_type(message) or None
        is_hd = self._attachment_field(message, "is_hd", "isHd")
        base_request = self._attachment_field(message, "base_request", "baseRequest")
        try:
            return await download_callback_attachment(
                self._get_sdk_client(),
                download_url=download_url,
                file_id=file_id,
                file_name=file_name,
                file_size=file_size,
                aes_key=aes_key,
                auth_key=auth_key,
                attachment_kind=attachment_kind,
                mime_type=mime_type,
                is_hd=is_hd,
                base_request=base_request,
                max_bytes=self._inbound_attachment_max_bytes,
            )
        except QwSaasPrivateObjectAccessError as exc:
            logger.info(
                "[%s] Juhe SDK private-object download failed; retrying through inbound S3 message_id=%s object=%s error=%s",
                self.name,
                getattr(message, "message_id", ""),
                safe_url_for_log(exc.object_url),
                exc.__class__.__name__,
            )
            downloaded = await asyncio.to_thread(
                self._download_private_object_via_inbound_s3_sync,
                exc.object_url,
                file_name,
                mime_type,
            )
            return SimpleNamespace(
                data=downloaded.data,
                file_name=downloaded.file_name,
                content_type=downloaded.content_type,
                _hermes_private_object_url=exc.object_url,
            )

    def _cache_inbound_attachment(self, data: bytes, file_name: str, message_type: MessageType) -> str:
        if message_type == MessageType.PHOTO:
            ext = Path(file_name).suffix.lower() or ".jpg"
            return cache_image_from_bytes(data, ext)
        return cache_document_from_bytes(data, file_name)

    def _attachment_private_object_url(self, message: Any, *, download: Any = None) -> str:
        candidates: List[str] = []
        if download is not None:
            for value in (
                getattr(download, "_hermes_private_object_url", None),
                getattr(download, "object_url", None),
                getattr(download, "objectUrl", None),
            ):
                text = str(value or "").strip()
                if text:
                    candidates.append(text)

        direct_value = self._attachment_field(
            message,
            "object_url",
            "objectUrl",
            "private_object_url",
            "privateObjectUrl",
            "storage_url",
            "storageUrl",
            "s3_url",
            "s3Url",
        )
        direct_text = str(direct_value or "").strip()
        if direct_text:
            candidates.append(direct_text)

        download_url = self._attachment_download_url(message)
        if download_url:
            candidates.append(download_url)

        for candidate in candidates:
            try:
                self._resolve_inbound_s3_bucket_key(candidate)
            except Exception:
                continue
            return candidate
        return ""

    def _attachment_supports_url_only_pipeline(
        self,
        *,
        attachment_type: MessageType,
        media_type: str,
        file_name: str,
    ) -> bool:
        if attachment_type == MessageType.PHOTO:
            return True
        if attachment_type != MessageType.DOCUMENT:
            return False
        normalized_media = str(media_type or "").strip().lower()
        if normalized_media in {
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
        }:
            return True
        return Path(file_name).suffix.lower() in {".pdf", ".doc", ".docx", ".xlsx", ".xls"}

    def _attachment_identity_dedupe_key(self, identity: Dict[str, Any]) -> str:
        bucket = str(identity.get("bucket") or "").strip().strip("/")
        object_key = str(identity.get("object_key") or "").strip().lstrip("/")
        if bucket and object_key:
            return f"bucket_key:{bucket}/{object_key}"
        object_url = self._session_db._normalize_juhe_attachment_object_url(identity.get("object_url"))  # noqa: SLF001
        if object_url:
            return f"object_url:{object_url}"
        file_id = str(identity.get("file_id") or "").strip()
        file_md5 = str(identity.get("file_md5") or "").strip().lower()
        if file_id and file_md5:
            return f"file_id_md5:{file_id}:{file_md5}"
        return ""

    def _attachment_size_hint(self, message: Any) -> Optional[int]:
        file_size_raw = self._attachment_field(message, "file_size", "size", "content_length")
        if file_size_raw in (None, ""):
            return None
        size_value = _safe_int(file_size_raw, default=-1)
        return size_value if size_value >= 0 else None

    def _schedule_attachment_preheat(
        self,
        *,
        access_url: str,
        identity: Dict[str, Any],
        media_type: str,
        display_name: str,
        attachment_type: MessageType,
        file_size: Optional[int],
    ) -> None:
        if not self._running:
            return
        normalized_url = str(access_url or "").strip()
        if not normalized_url:
            return
        if attachment_type not in {MessageType.PHOTO, MessageType.DOCUMENT}:
            return
        if file_size is None or file_size <= 0 or file_size > self._attachment_preheat_max_bytes:
            return

        dedupe_key = self._attachment_identity_dedupe_key(identity)
        if not dedupe_key or dedupe_key in self._attachment_preheat_pending_keys:
            return

        cached = self._session_db.get_juhe_attachment_parse_cache(platform="juhe", **identity)
        if cached is not None:
            cached_status = str(cached.get("parse_status") or "").strip().lower()
            cached_text = str(cached.get("extracted_text") or "").strip()
            if cached_status == "success" and cached_text:
                return
            if cached_status == "pending":
                return

        self._attachment_preheat_pending_keys.add(dedupe_key)
        self._ensure_attachment_preheat_workers()
        self._session_db.upsert_juhe_attachment_parse_cache(
            platform="juhe",
            media_type=media_type,
            file_name=display_name,
            parser="preheat_queue",
            extracted_text="",
            content_kind="image" if attachment_type == MessageType.PHOTO else "document",
            content_format="vision" if attachment_type == MessageType.PHOTO else "text",
            display_description=(
                f"图片 {display_name}：已收到，后台正在解析"
                if attachment_type == MessageType.PHOTO
                else f"文件 {display_name}：已收到，后台正在解析"
            ),
            parse_status="pending",
            error_message="",
            **identity,
        )
        self._attachment_preheat_queue.put_nowait(
            {
                "dedupe_key": dedupe_key,
                "access_url": normalized_url,
                "identity": identity,
                "media_type": media_type,
                "display_name": display_name,
                "attachment_type": attachment_type.value,
            }
        )

    async def _run_attachment_preheat_job(self, job: dict[str, Any]) -> None:
        from gateway.juhe_media_runtime import parse_juhe_media_bundle

        attachment_type = MessageType(job["attachment_type"])
        display_name = str(job.get("display_name") or "attachment").strip() or "attachment"
        media_type = str(job.get("media_type") or "").strip()
        identity = job.get("identity") or {}
        access_url = str(job.get("access_url") or "").strip()

        bundle = await parse_juhe_media_bundle(
            access_url=access_url,
            display_name=display_name,
            media_type=media_type,
            attachment_type=attachment_type,
        )

        self._session_db.upsert_juhe_attachment_parse_cache(
            platform="juhe",
            media_type=media_type,
            file_name=bundle.display_name,
            parser=bundle.parser,
            extracted_text=bundle.extracted_text,
            content_kind="image" if attachment_type == MessageType.PHOTO else "document",
            content_format=bundle.content_format,
            display_description=bundle.display_description,
            parse_status=bundle.parse_status,
            error_message=bundle.error_message,
            **identity,
        )

    def _build_inbound_attachment_identity(
        self,
        message: Any,
        *,
        resolved_url: str = "",
        object_url: str = "",
    ) -> Dict[str, Any]:
        normalized_file_id = str(
            self._attachment_field(message, "file_id", "fileId", "media_id", "mediaId") or ""
        ).strip()
        normalized_file_md5 = str(
            self._attachment_field(message, "file_md5", "md5", "fileMd5") or ""
        ).strip()
        direct_object_url = self._attachment_private_object_url(message)
        prefer_file_identity = bool(normalized_file_id and normalized_file_md5) and not direct_object_url

        candidate_object_url = ""
        if direct_object_url:
            candidate_object_url = direct_object_url
        elif not prefer_file_identity:
            candidate_object_url = str(object_url or "").strip()
            if not candidate_object_url and _is_http_url(resolved_url):
                candidate_object_url = str(resolved_url).strip()

        bucket = ""
        object_key = ""
        if candidate_object_url:
            try:
                bucket, object_key = self._resolve_inbound_s3_bucket_key(candidate_object_url)
            except Exception:
                bucket = ""
                object_key = ""

        return {
            "bucket": bucket,
            "object_key": object_key,
            "object_url": candidate_object_url,
            "file_id": normalized_file_id,
            "file_md5": normalized_file_md5,
        }

    def _build_inbound_attachment_media_ref(
        self,
        file_name: str,
        identity: Dict[str, Any],
    ) -> str:
        safe_name = Path(str(file_name or "").strip() or "attachment").name or "attachment"
        stable_identity = (
            str(identity.get("bucket") or "").strip()
            + "/"
            + str(identity.get("object_key") or "").strip()
        ).strip("/")
        if not stable_identity:
            stable_identity = str(identity.get("object_url") or "").strip()
        if not stable_identity:
            stable_identity = (
                str(identity.get("file_id") or "").strip()
                + ":"
                + str(identity.get("file_md5") or "").strip()
            ).strip(":")
        if not stable_identity:
            stable_identity = safe_name
        digest = hashlib.sha256(stable_identity.encode("utf-8", errors="ignore")).hexdigest()[:16]
        return f"juhe://attachment/{digest}/{safe_name}"

    def _maybe_generate_inbound_access_url(self, resolved_url: str, *, object_url: str = "") -> str:
        candidate_object_url = str(object_url or "").strip()
        if candidate_object_url:
            try:
                presigned_url = self._generate_inbound_s3_presigned_get_url_sync(candidate_object_url)
                logger.info(
                    "[%s] Generated Juhe inbound presigned URL object=%s",
                    self.name,
                    safe_url_for_log(candidate_object_url),
                )
                return presigned_url
            except Exception:
                logger.info(
                    "[%s] Failed to generate Juhe inbound presigned URL object=%s endpoint=%s",
                    self.name,
                    safe_url_for_log(candidate_object_url),
                    safe_url_for_log(str(self._inbound_s3_endpoint_url or "")),
                    exc_info=True,
                )
                return ""
        direct_url = str(resolved_url or "").strip()
        return direct_url if _is_http_url(direct_url) else ""

    async def _resolve_inbound_base_request(self, base_request: Any) -> Dict[str, str]:
        if isinstance(base_request, dict):
            required = ("cdn_dns", "client_version", "corp_id", "vid")
            if all(str(base_request.get(key) or "").strip() for key in required):
                return {key: str(base_request[key]) for key in required}
        if get_cdn_info is None:
            raise RuntimeError("qwsaas get_cdn_info helper is unavailable")
        client = self._get_sdk_client()
        body = await get_cdn_info(client)
        data = body.get("data") if isinstance(body.get("data"), dict) else None
        if not isinstance(data, dict):
            raise QwSaasResponseError("Invalid CDN info response data")
        required = ("cdn_dns", "client_version", "corp_id", "vid")
        missing = [key for key in required if not str(data.get(key) or "").strip()]
        if missing:
            raise QwSaasResponseError(f"Missing CDN info fields: {', '.join(missing)}")
        return {key: str(data[key]) for key in required}

    def _extract_private_download_url(self, body: Dict[str, Any]) -> str:
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        url = str(data.get("url") or body.get("url") or "").strip()
        if not url:
            raise QwSaasResponseError("Private download response missing data.url")
        return url

    def _extract_big_download_url(self, body: Dict[str, Any]) -> str:
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        url = str(data.get("url") or body.get("url") or "").strip()
        if not url:
            raise QwSaasResponseError("Big download response missing data.url")
        return url

    def _c2c_attachment_file_type(
        self,
        *,
        attachment_type: MessageType,
        media_type: str,
        is_hd: Any,
    ) -> int:
        normalized_media = str(media_type or "").strip().lower()
        if attachment_type == MessageType.PHOTO or normalized_media.startswith("image/"):
            return 1 if _truthy(is_hd, default=False) else 2
        if attachment_type == MessageType.VIDEO or normalized_media.startswith("video/"):
            return 4
        return 5

    async def _resolve_private_attachment_object_url(
        self,
        message: Any,
        *,
        attachment_type: MessageType,
        attachment_name: str,
        media_type: str,
    ) -> str:
        client = self._get_sdk_client()
        file_id = str(
            self._attachment_field(message, "file_id", "fileId", "media_id", "mediaId") or ""
        ).strip()
        download_url = self._attachment_download_url(message)
        file_size_raw = self._attachment_field(message, "file_size", "size", "content_length")
        file_size = None if file_size_raw in (None, "") else _safe_int(file_size_raw, default=0)
        aes_key = str(self._attachment_field(message, "aes_key", "aesKey") or "").strip()
        auth_key = str(self._attachment_field(message, "auth_key", "authKey") or "").strip()
        base_request = self._attachment_field(message, "base_request", "baseRequest")
        is_hd = self._attachment_field(message, "is_hd", "isHd")

        if file_id and _looks_like_big_file_id(file_id):
            if get_wwfile_download_info is None:
                raise RuntimeError("qwsaas get_wwfile_download_info helper is unavailable")
            body = await get_wwfile_download_info(client, file_id)
            return self._extract_big_download_url(body)

        if file_id and _looks_like_c2c_file_id(file_id):
            if not aes_key:
                raise QwSaasRequestError("c2c attachment URL resolution requires aes_key")
            if file_size is None:
                raise QwSaasRequestError("c2c attachment URL resolution requires file_size")
            resolved_base_request = await self._resolve_inbound_base_request(base_request)
            body = await client._request_private(
                "/cloud/c2c_download",
                data={
                    "file_id": file_id,
                    "file_name": attachment_name,
                    "file_size": int(file_size),
                    "file_type": self._c2c_attachment_file_type(
                        attachment_type=attachment_type,
                        media_type=media_type,
                        is_hd=is_hd,
                    ),
                    "aes_key": aes_key,
                    "to_mp3": False,
                    "base_request": resolved_base_request,
                },
            )
            return self._extract_private_download_url(body)

        private_reference = ""
        if file_id and _is_http_url(file_id) and not _looks_like_public_qpic_url(file_id):
            private_reference = file_id
        elif download_url and _looks_like_private_media_url(download_url):
            private_reference = download_url

        if private_reference:
            if not aes_key or not auth_key:
                raise QwSaasRequestError("private attachment URL resolution requires aes_key and auth_key")
            resolved_base_request = await self._resolve_inbound_base_request(base_request)
            body = await client._request_private(
                "/cloud/wx_download",
                data={
                    "url": private_reference,
                    "file_name": attachment_name,
                    "aes_key": aes_key,
                    "auth_key": auth_key,
                    "base_request": resolved_base_request,
                },
            )
            return self._extract_private_download_url(body)

        direct_url = download_url or file_id
        if _is_http_url(direct_url):
            return direct_url
        raise RuntimeError("No remote attachment access URL available")

    async def _resolve_inbound_attachment_access_target(
        self,
        message: Any,
        *,
        attachment_type: MessageType,
        attachment_name: str,
        media_type: str,
    ) -> _ResolvedAttachmentAccessTarget:
        direct_object_url = self._attachment_private_object_url(message)
        if direct_object_url:
            logger.info(
                "[%s] Juhe attachment access uses direct object URL message_id=%s object=%s",
                self.name,
                getattr(message, "message_id", ""),
                safe_url_for_log(direct_object_url),
            )
            return _ResolvedAttachmentAccessTarget(url=direct_object_url, object_url=direct_object_url)

        resolved_url = await self._resolve_private_attachment_object_url(
            message,
            attachment_type=attachment_type,
            attachment_name=attachment_name,
            media_type=media_type,
        )
        normalized_object_url = ""
        try:
            self._resolve_inbound_s3_bucket_key(resolved_url)
            normalized_object_url = resolved_url
        except Exception:
            normalized_object_url = ""
        logger.info(
            "[%s] Juhe attachment access resolved message_id=%s attachment=%s resolved=%s object_candidate=%s",
            self.name,
            getattr(message, "message_id", ""),
            attachment_name,
            safe_url_for_log(resolved_url),
            safe_url_for_log(normalized_object_url),
        )
        return _ResolvedAttachmentAccessTarget(url=resolved_url, object_url=normalized_object_url)

    def _attachment_message_type(self, message: Any, *, content_type: str | None = None) -> MessageType:
        kind = self._attachment_kind(message)
        mime_type = self._attachment_mime_type(message, content_type=content_type)

        if kind == "image" or mime_type.startswith("image/"):
            return MessageType.PHOTO
        if kind == "video" or mime_type.startswith("video/"):
            return MessageType.VIDEO
        if kind == "audio" or mime_type.startswith("audio/"):
            if self._looks_like_voice_attachment(mime_type, self._attachment_name(message)):
                return MessageType.VOICE
            return MessageType.AUDIO
        return MessageType.DOCUMENT

    def _attachment_name(self, message: Any) -> str:
        file_name = str(
            self._attachment_field(message, "file_name", "filename", "name", "title") or ""
        ).strip()
        if file_name:
            return file_name
        download_url = self._attachment_download_url(message)
        derived = _source_name(download_url, default="")
        if derived:
            return derived

        default_names = {
            "image": "image.jpg",
            "audio": "audio.amr",
            "video": "video.mp4",
            "document": "document",
        }
        return default_names.get(self._attachment_kind(message), "attachment")

    def _attachment_kind(self, message: Any) -> str:
        kind = str(self._attachment_field(message, "attachment_kind") or "").strip().lower()
        if kind:
            return kind
        msg_type = _safe_int(
            getattr(message, "message_type", self._raw_message_field(message, "msg_type", 0)),
            default=0,
        )
        return {
            MSG_TYPE_IMAGE: "image",
            MSG_TYPE_VOICE: "audio",
            MSG_TYPE_VIDEO: "video",
            MSG_TYPE_FILE: "document",
        }.get(msg_type, "")

    def _attachment_mime_type(self, message: Any, *, content_type: str | None = None) -> str:
        mime_type = _normalize_attachment_mime(content_type)
        if mime_type and mime_type != "application/octet-stream":
            return mime_type
        mime_type = _normalize_attachment_mime(
            self._attachment_field(message, "mime_type", "content_type", "contentType")
        )
        if mime_type and mime_type != "application/octet-stream":
            return mime_type
        guessed, _encoding = mimetypes.guess_type(self._attachment_name(message))
        if guessed:
            return str(guessed).strip().lower()
        return mime_type or "application/octet-stream"

    def _attachment_download_url(self, message: Any) -> str:
        download_url = str(
            self._attachment_field(
                message,
                "download_url",
                "url",
                "file_url",
                "fileUrl",
                "src",
                "cdn_url",
            )
            or ""
        ).strip()
        if download_url:
            return download_url
        file_id = str(
            self._attachment_field(message, "file_id", "fileId", "media_id", "mediaId") or ""
        ).strip()
        if _is_http_url(file_id):
            return file_id
        return ""

    def _attachment_field(self, message: Any, attr_name: str, *raw_keys: str) -> Any:
        value = getattr(message, attr_name, None)
        if value not in (None, ""):
            return value

        candidate_keys = (attr_name, *raw_keys)
        for key in candidate_keys:
            direct_value = self._raw_message_field(message, key)
            if direct_value not in (None, ""):
                return direct_value

        for payload in self._attachment_payloads(message):
            for key in candidate_keys:
                payload_value = payload.get(key)
                if payload_value not in (None, ""):
                    return payload_value
        return None

    def _attachment_payloads(self, message: Any) -> List[dict[str, Any]]:
        raw_message = getattr(message, "raw_message", None)
        if not isinstance(raw_message, dict):
            return []

        content = _coerce_dict(raw_message.get("content"))
        nested_data = content.get("data") if isinstance(content.get("data"), dict) else None
        payloads: List[dict[str, Any]] = []
        seen: set[int] = set()

        def add_payload(candidate: Any) -> None:
            if not isinstance(candidate, dict):
                return
            marker = id(candidate)
            if marker in seen:
                return
            seen.add(marker)
            payloads.append(candidate)

        for container in (raw_message, nested_data, content):
            if not isinstance(container, dict):
                continue
            add_payload(container)
            add_payload(container.get("cdn"))
            for key in ("image", "img", "pic", "voice", "audio", "video", "file", "document"):
                add_payload(container.get(key))

        return payloads

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

    def _parse_raw_room_message(self, raw_payload: Any) -> Any | None:
        if parse_callback_event is None or not isinstance(raw_payload, dict):
            return None
        try:
            parsed = parse_callback_event(
                {
                    "guid": self._guid,
                    "notify_type": NOTIFY_NEW_MESSAGE,
                    "data": raw_payload,
                }
            )
        except Exception:
            logger.debug("[%s] Failed to re-parse room message payload", self.name, exc_info=True)
            return None
        if parsed is None or not parsed.messages:
            return None
        return parsed.messages[0]

    async def _rehydrate_pending_room_attachments(
        self,
        *,
        inbound: MessageEvent,
        pending_messages: List[Dict[str, Any]],
    ) -> None:
        inbound_access_urls = list(getattr(inbound, "_juhe_media_access_urls", []) or [])
        inbound_identities = list(getattr(inbound, "_juhe_attachment_identities", []) or [])
        inbound_descriptions = list(getattr(inbound, "_juhe_media_descriptions", []) or [])
        while len(inbound_access_urls) < len(inbound.media_urls):
            inbound_access_urls.append("")
        while len(inbound_identities) < len(inbound.media_urls):
            inbound_identities.append({})
        while len(inbound_descriptions) < len(inbound.media_urls):
            inbound_descriptions.append("")
        updated_access_urls = False
        updated_identities = False
        updated_descriptions = False
        appended_attachment = False
        for item in pending_messages:
            if _safe_int(item.get("message_type"), default=0) not in SUPPORTED_ATTACHMENT_MESSAGE_TYPES:
                continue
            parsed_message = self._parse_raw_room_message(item.get("raw_payload"))
            if parsed_message is None:
                continue
            attachment_event = await self._build_attachment_event(
                message=parsed_message,
                source=inbound.source,
            )
            attachment_access_urls = list(getattr(attachment_event, "_juhe_media_access_urls", []) or [])
            attachment_identities = list(getattr(attachment_event, "_juhe_attachment_identities", []) or [])
            attachment_descriptions = list(getattr(attachment_event, "_juhe_media_descriptions", []) or [])
            for idx, media_url in enumerate(attachment_event.media_urls):
                if media_url in inbound.media_urls:
                    continue
                appended_attachment = True
                inbound.media_urls.append(media_url)
                media_type = (
                    attachment_event.media_types[idx]
                    if idx < len(attachment_event.media_types)
                    else ""
                )
                inbound.media_types.append(media_type)
                access_url = attachment_access_urls[idx] if idx < len(attachment_access_urls) else ""
                inbound_access_urls.append(access_url)
                updated_access_urls = updated_access_urls or bool(access_url)
                identity = attachment_identities[idx] if idx < len(attachment_identities) else {}
                inbound_identities.append(identity)
                updated_identities = updated_identities or bool(identity)
                description = attachment_descriptions[idx] if idx < len(attachment_descriptions) else ""
                inbound_descriptions.append(description)
                updated_descriptions = updated_descriptions or bool(str(description or "").strip())
        if appended_attachment:
            setattr(inbound, "_juhe_media_access_urls", inbound_access_urls)
            setattr(inbound, "_juhe_attachment_identities", inbound_identities)
        if updated_access_urls:
            setattr(inbound, "_juhe_media_access_urls", inbound_access_urls)
        if updated_identities:
            setattr(inbound, "_juhe_attachment_identities", inbound_identities)
        if updated_descriptions:
            setattr(inbound, "_juhe_media_descriptions", inbound_descriptions)

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

    def _should_trigger_group_reply(
        self,
        chat_id: str,
        sender_id: str,
        at_list: List[str],
        text: str = "",
    ) -> bool:
        if not self._is_group_allowed(chat_id, sender_id):
            return False
        trigger_user_ids = self._access_control_store.get_group_trigger_user_ids()
        if not trigger_user_ids or not _entry_matches(trigger_user_ids, sender_id):
            return False
        return self._group_message_mentions_target(at_list, text)

    def _group_message_mentions_target(self, at_list: List[str], text: str = "") -> bool:
        if not self._normalized_mention_targets:
            return bool(at_list) or _has_textual_mention_fallback(text)

        structured_mentions = {
            token for item in at_list if (token := _normalize_mention_token(item))
        }
        if structured_mentions & self._normalized_mention_targets:
            return True

        textual_mentions = set(_extract_textual_mentions(text))
        return bool(textual_mentions & self._normalized_mention_targets)

    @staticmethod
    def _message_created_at(message: Any) -> float:
        for field_name in ("create_time", "create_timestamp", "timestamp", "msg_time", "send_time"):
            value = getattr(message, field_name, None)
            try:
                if value is not None:
                    numeric = float(value)
                    if numeric > 0:
                        return numeric
            except (TypeError, ValueError):
                continue
        return datetime.now().timestamp()

    def _room_message_preview(self, message: Any) -> str:
        text = str(getattr(message, "text", "") or "").strip()
        if text:
            return text

        msg_type = _safe_int(getattr(message, "message_type", 0), default=0)
        attachment_name = self._attachment_name(message)
        if msg_type == MSG_TYPE_IMAGE:
            return f"[图片] {attachment_name}".strip()
        if msg_type == MSG_TYPE_VOICE:
            return f"[语音] {attachment_name}".strip()
        if msg_type == MSG_TYPE_VIDEO:
            return f"[视频] {attachment_name}".strip()
        if msg_type == MSG_TYPE_FILE:
            return f"[文件] {attachment_name}".strip()
        return f"[{_msg_type_label(msg_type)}]"

    def _append_room_message(
        self,
        *,
        conversation_id: str,
        message_id: str,
        appinfo: str,
        refer_id: str,
        seq: str,
        sender_id: str,
        sender_name: str,
        direction: str,
        message_type: int,
        text_preview: str,
        raw_payload: Any,
        created_at: float,
        triggered: bool,
        consumed_for_context: bool,
    ) -> int:
        return self._session_db.append_room_message(
            platform=self.platform.value,
            conversation_id=conversation_id,
            message_id=message_id or None,
            appinfo=appinfo or None,
            refer_id=refer_id or None,
            seq=seq or None,
            sender_id=sender_id or None,
            sender_name=sender_name or None,
            direction=direction,
            message_type=message_type,
            text_preview=text_preview,
            raw_payload=raw_payload,
            created_at=created_at,
            triggered=triggered,
            consumed_for_context=consumed_for_context,
            room_log_limit=self._room_log_limit,
        )

    @staticmethod
    def _format_pending_room_context(messages: List[Dict[str, Any]]) -> str:
        lines: List[str] = []
        for item in messages:
            preview = str(item.get("text_preview") or "").strip()
            if not preview:
                continue
            sender = str(item.get("sender_name") or item.get("sender_id") or "unknown").strip()
            lines.append(f"[{sender}] {preview}")
        return "\n".join(lines)

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
            return _entry_matches(self._access_control_store.get_dm_allow_from(), sender_id)
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

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        if outcome != ProcessingOutcome.SUCCESS:
            return
        if event.source.chat_type != "group":
            return
        if getattr(event, "_juhe_skip_auto_room_memory", False):
            return

        assistant_response = _strip_instruction_leakage_for_juhe(
            getattr(event, "_final_response_text", "")
        )
        current_message = str(event.text or "").strip()
        if not current_message or not assistant_response:
            return

        schedule_room_memory_update(
            event.source.chat_id,
            current_message=current_message,
            pending_context=str(getattr(event, "_juhe_pending_context_text", "") or ""),
            relevant_room_history=str(getattr(event, "_juhe_relevant_room_history_text", "") or ""),
            assistant_response=assistant_response,
            char_limit=self._room_memory_char_limit,
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
        formatted = self.format_message(content)
        if content and not formatted:
            return SendResult(success=False, error=JUHE_CERTIFICATE_REMOTE_DELIVERY_ERROR)
        try:
            result = await self._guid_request(
                path="/msg/send_text",
                data={
                    "guid": self._guid,
                    "conversation_id": target,
                    "content": formatted,
                },
            )
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

        err_code = result.get("err_code")
        if err_code != 0:
            return SendResult(success=False, error=str(result.get("err_msg") or err_code))
        message_id = str(result.get("message_id") or "")
        if target.upper().startswith("R:"):
            self._append_room_message(
                conversation_id=target,
                message_id=message_id,
                appinfo="",
                refer_id="",
                seq="",
                sender_id=self._guid,
                sender_name="Hermes",
                direction="outbound",
                message_type=MSG_TYPE_TEXT,
                text_preview=formatted,
                raw_payload=result,
                created_at=datetime.now().timestamp(),
                triggered=True,
                consumed_for_context=True,
            )
        return SendResult(success=True, message_id=message_id)

    def format_message(self, content: str) -> str:
        cleaned = _strip_visible_markdown_for_juhe(content)
        cleaned = _strip_local_delivery_paths_for_juhe(cleaned)
        return _strip_certificate_delivery_links_for_juhe(cleaned)

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
            caption_after_upload=True,
            materialize_remote_document=True,
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
        caption_after_upload: bool = False,
        materialize_remote_document: bool = False,
    ) -> SendResult:
        target = str(chat_id or "").strip()
        if not target.upper().startswith(("S:", "R:")):
            return SendResult(success=False, error="Juhe targets must be prefixed with S: for DMs or R: for groups")

        source_text = str(source or "").strip()
        if not source_text:
            return SendResult(success=False, error="Juhe file source is required")
        if not self._private_base_url:
            return SendResult(success=False, error="JUHE_PRIVATE_BASE_URL is required for Juhe file delivery")
        upload_name = file_name or _source_name(source_text)
        materialized_cleanup = None
        resolved_source_text = source_text

        if caption and not caption_after_upload:
            caption_result = await self.send(target, caption, reply_to=reply_to, metadata=metadata)
            if not caption_result.success:
                return caption_result

        if materialize_remote_document and _is_http_url(source_text):
            try:
                resolved_source_text, materialized_cleanup = await self._materialize_remote_document_for_upload(
                    source_text,
                    upload_name,
                )
            except Exception as exc:
                detail = _format_exception_detail(exc)
                return SendResult(success=False, error=detail, retryable=True)

        staged_cleanup = None
        try:
            if materialized_cleanup is not None:
                upload_source, staged_cleanup = await self._stage_local_file_for_upload(resolved_source_text)
            else:
                upload_source, staged_cleanup = await self._resolve_upload_source(
                    resolved_source_text,
                    upload_name=upload_name,
                )
        except FileNotFoundError as exc:
            return SendResult(success=False, error=str(exc))
        except Exception as exc:
            detail = _format_exception_detail(exc)
            return SendResult(success=False, error=detail, retryable=True)

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
            detail = _format_exception_detail(exc)
            logger.warning(
                "[%s] Juhe file upload failed source=%s upload_name=%s: %s",
                self.name,
                safe_url_for_log(source_text) if _is_http_url(source_text) else Path(source_text).name,
                upload_name,
                detail,
            )
            return SendResult(success=False, error=detail, retryable=True)
        finally:
            if staged_cleanup is not None:
                try:
                    await staged_cleanup()
                except Exception:
                    logger.warning("[%s] Failed to clean up staged temp object for %s", self.name, source_text, exc_info=True)
            if materialized_cleanup is not None:
                try:
                    await materialized_cleanup()
                except Exception:
                    logger.warning("[%s] Failed to clean up materialized remote document for %s", self.name, source_text, exc_info=True)

        if caption and caption_after_upload:
            caption_result = await self.send(target, caption, reply_to=reply_to, metadata=metadata)
            if not caption_result.success:
                return caption_result

        response_data = body.get("data") if isinstance(body.get("data"), dict) else {}
        return SendResult(
            success=True,
            message_id=str(body.get("message_id") or response_data.get("message_id") or ""),
        )

    async def _materialize_remote_document_for_upload(
        self,
        source: str,
        upload_name: str,
    ) -> Tuple[str, Callable[[], Awaitable[None]]]:
        suffix = media_reference_suffix(upload_name or source) or ".bin"
        fd, temp_path = tempfile.mkstemp(prefix="juhe-outbound-doc-", suffix=suffix)
        os.close(fd)
        path_obj = Path(temp_path)

        async def _cleanup() -> None:
            try:
                path_obj.unlink(missing_ok=True)
            except TypeError:
                if path_obj.exists():
                    path_obj.unlink()

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)",
            "Accept": "*/*",
        }
        timeout = httpx.Timeout(
            DEFAULT_OUTBOUND_DOCUMENT_DOWNLOAD_TIMEOUT_SECONDS,
            connect=DEFAULT_OUTBOUND_DOCUMENT_DOWNLOAD_CONNECT_TIMEOUT_SECONDS,
        )
        last_exc: Exception | None = None

        try:
            for attempt in range(DEFAULT_OUTBOUND_DOCUMENT_DOWNLOAD_RETRIES):
                try:
                    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                        async with client.stream("GET", source, headers=headers) as response:
                            response.raise_for_status()
                            with path_obj.open("wb") as handle:
                                async for chunk in response.aiter_bytes():
                                    if chunk:
                                        handle.write(chunk)
                    return str(path_obj), _cleanup
                except (httpx.TimeoutException, httpx.RequestError, httpx.HTTPStatusError) as exc:
                    last_exc = exc
                    try:
                        path_obj.unlink(missing_ok=True)
                    except TypeError:
                        if path_obj.exists():
                            path_obj.unlink()
                    if isinstance(exc, httpx.HTTPStatusError):
                        status_code = exc.response.status_code
                        if status_code < 500 and status_code not in {408, 409, 425, 429}:
                            raise
                    if attempt + 1 >= DEFAULT_OUTBOUND_DOCUMENT_DOWNLOAD_RETRIES:
                        raise
                    wait_seconds = 1.5 * (attempt + 1)
                    logger.warning(
                        "[%s] Retrying Juhe outbound document download %d/%d source=%s wait=%.1fs error=%s",
                        self.name,
                        attempt + 1,
                        DEFAULT_OUTBOUND_DOCUMENT_DOWNLOAD_RETRIES,
                        safe_url_for_log(source),
                        wait_seconds,
                        _format_exception_detail(exc),
                    )
                    await asyncio.sleep(wait_seconds)
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("Remote document download failed")
        except Exception:
            await _cleanup()
            raise

    async def _resolve_upload_source(
        self,
        source: str,
        *,
        upload_name: Optional[str] = None,
    ) -> Tuple[str, Optional[Callable[[], Awaitable[None]]]]:
        if _is_http_url(source):
            if _looks_like_signed_http_url(source):
                missing = self._missing_temp_s3_fields()
                if missing:
                    logger.warning(
                        "[%s] Signed remote upload source cannot be restaged without temp S3 config; using source directly: %s",
                        self.name,
                        safe_url_for_log(source),
                    )
                else:
                    return await self._stage_remote_http_source_for_upload(
                        source,
                        upload_name or _source_name(source),
                    )
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

    async def _stage_remote_http_source_for_upload(
        self,
        source_url: str,
        upload_name: str,
    ) -> Tuple[str, Callable[[], Awaitable[None]]]:
        from tools.url_safety import is_safe_url

        if not is_safe_url(source_url):
            raise ValueError(f"Blocked unsafe URL (SSRF protection): {safe_url_for_log(source_url)}")

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)",
            "Accept": "*/*",
        }
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.get(source_url, headers=headers)
            response.raise_for_status()
        cached_path = cache_document_from_bytes(response.content, upload_name)
        staged_url, staged_cleanup = await self._stage_local_file_for_upload(cached_path)

        async def _cleanup() -> None:
            try:
                await staged_cleanup()
            finally:
                try:
                    Path(cached_path).unlink(missing_ok=True)
                except Exception:
                    logger.debug(
                        "[%s] Failed to remove temporary staged cache file %s",
                        self.name,
                        cached_path,
                        exc_info=True,
                    )

        return staged_url, _cleanup

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
        text = str(object_url or "").strip()
        parsed = urlparse(text)
        path = unquote(parsed.path).lstrip("/")
        configured_bucket = str(self._inbound_s3_bucket or "").strip()

        if _looks_like_inbound_proxy_download_path(parsed.path):
            logger.info(
                "[%s] Rejecting Juhe proxy download URL for S3 resolution url=%s endpoint=%s",
                self.name,
                safe_url_for_log(text),
                safe_url_for_log(str(self._inbound_s3_endpoint_url or "")),
            )
            raise RuntimeError(
                f"Proxy download URL is not a private object URL: {safe_url_for_log(text)}"
            )

        if parsed.scheme or parsed.netloc:
            endpoint = urlparse(str(self._inbound_s3_endpoint_url or "").strip())
            endpoint_netloc = endpoint.netloc.lower()
            parsed_netloc = parsed.netloc.lower()
            virtual_bucket = ""
            if endpoint_netloc and parsed_netloc != endpoint_netloc:
                suffix = f".{endpoint_netloc}"
                if parsed_netloc.endswith(suffix):
                    virtual_bucket = parsed.netloc[: -len(suffix)].strip(".")
                else:
                    logger.warning(
                        "[%s] Inbound S3 host mismatch object=%s endpoint=%s",
                        self.name,
                        safe_url_for_log(text),
                        safe_url_for_log(str(self._inbound_s3_endpoint_url or "")),
                    )
                    raise RuntimeError(
                        f"Private object URL host does not match inbound S3 endpoint: {object_url}"
                    )
            if virtual_bucket and path:
                return virtual_bucket, path

        if configured_bucket and path.startswith(f"{configured_bucket}/"):
            return configured_bucket, path[len(configured_bucket) + 1 :]
        if configured_bucket and path:
            return configured_bucket, path
        if "/" in path:
            bucket, key = path.split("/", 1)
            if bucket and key:
                return bucket, key
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
        logger.info(
            "[%s] Falling back to inbound S3 object download bucket=%s key=%s object=%s",
            self.name,
            bucket,
            key,
            safe_url_for_log(object_url),
        )
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

    def _generate_inbound_s3_presigned_get_url_sync(
        self,
        object_url: str,
        *,
        expires_seconds: Optional[int] = None,
    ) -> str:
        client = self._build_inbound_s3_client()
        bucket, key = self._resolve_inbound_s3_bucket_key(object_url)
        expires_in = self._inbound_s3_url_expires_seconds
        if expires_seconds is not None:
            expires_in = max(60, int(expires_seconds))
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_in,
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
        except Exception as exc:
            if not _should_retry_big_upload_after_small_failure(
                exc,
                size_hint=size_hint,
                file_type=file_type,
            ):
                raise
            logger.warning(
                "[%s] small upload failed for %s, retrying big upload: %s",
                self.name,
                safe_url_for_log(file_url),
                _format_exception_detail(exc),
            )
            return await send_big_file_from_url(
                client=client,
                conversation_id=conversation_id,
                file_url=file_url,
                file_name=file_name,
                file_type=file_type,
            )

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
