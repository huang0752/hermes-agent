"""Shared rules for outbound delivery references."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_HTTP_URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.IGNORECASE)
_CERTIFICATE_RENDER_JOB_SEGMENTS = (
    "/certificate_render_jobs/",
    "/certificaterenderjobs/",
)

JUHE_CERTIFICATE_REMOTE_DELIVERY_ERROR = (
    "Juhe certificate delivery requires a local artifact. "
    "Do not send certificate render-job download URLs directly; "
    "use the wrapper-provided local artifact instead. "
    "Do not work around this with terminal, curl, wget, or Python download scripts."
)


def _clean_url_candidate(value: str) -> str:
    return str(value or "").strip().rstrip(".,;:)]}>\"'")


def is_certificate_render_job_url(value: str) -> bool:
    """Return True when value is a remote certificate render-job download URL."""
    text = _clean_url_candidate(value)
    if not text:
        return False

    try:
        parsed = urlsplit(text)
    except Exception:
        return False

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return False

    path = (parsed.path or "").lower()
    return any(segment in path for segment in _CERTIFICATE_RENDER_JOB_SEGMENTS)


def extract_http_urls(text: str) -> list[str]:
    """Return cleaned HTTP(S) URLs found in free-form text."""
    value = str(text or "")
    if not value:
        return []
    results: list[str] = []
    seen: set[str] = set()
    for match in _HTTP_URL_RE.finditer(value):
        candidate = _clean_url_candidate(match.group(0))
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        results.append(candidate)
    return results


def contains_certificate_render_job_url(text: str) -> bool:
    """Return True when free-form text includes any certificate render-job URL."""
    return any(is_certificate_render_job_url(url) for url in extract_http_urls(text))
