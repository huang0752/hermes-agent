from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


_RENDER_JOB_SUMMARY_TOOLS = {
    "mcp_local_create_render_job_and_wait",
    "mcp_local_get_render_job_download_url",
}

_MATERIALIZATION_MESSAGE = (
    "Render job ready. Call materialize_render_job_artifact with this job_id before sending the file."
)


@dataclass(frozen=True)
class DeliveryArtifact:
    tool_name: str
    local_path: str
    filename: str
    size: int | None
    media_tag: str


def _load_json(content: str) -> Any:
    try:
        return json.loads(content)
    except Exception:
        return None


def _unwrap_tool_payload(content: str) -> Any:
    payload = _load_json(content)
    current = payload

    for _ in range(4):
        if not isinstance(current, dict):
            return current

        structured = current.get("structuredContent")
        if isinstance(structured, dict):
            current = structured
            continue
        if isinstance(structured, str):
            parsed = _load_json(structured)
            if isinstance(parsed, dict):
                current = parsed
                continue

        result = current.get("result")
        if isinstance(result, dict):
            current = result
            continue
        if isinstance(result, str):
            parsed = _load_json(result)
            if isinstance(parsed, dict):
                current = parsed
                continue

        return current

    return current


def extract_delivery_artifacts(tool_name: str, content: str) -> list[DeliveryArtifact]:
    payload = _unwrap_tool_payload(content)
    if not isinstance(payload, dict):
        return []

    delivery = payload.get("delivery")
    if not isinstance(delivery, dict):
        return []

    local_path = str(delivery.get("local_path") or "").strip()
    filename = str(delivery.get("filename") or "").strip()
    media_tag = str(delivery.get("media_tag") or "").strip()
    if not local_path or not filename or not media_tag.startswith("MEDIA:"):
        return []

    size_value = delivery.get("size")
    size = int(size_value) if isinstance(size_value, int) else None
    return [
        DeliveryArtifact(
            tool_name=tool_name,
            local_path=local_path,
            filename=filename,
            size=size,
            media_tag=media_tag,
        )
    ]


def sanitize_tool_result_for_model(tool_name: str, content: str) -> str:
    payload = _unwrap_tool_payload(content)
    if not isinstance(payload, dict):
        return content

    delivery = payload.get("delivery")
    if isinstance(delivery, dict):
        delivery = dict(delivery)
        delivery.pop("local_path", None)
        delivery.pop("media_tag", None)
        payload["delivery"] = delivery
        payload.pop("download_url", None)
        payload.pop("output_url", None)

    if tool_name in _RENDER_JOB_SUMMARY_TOOLS:
        payload.pop("download_url", None)
        payload.pop("output_url", None)
        payload["delivery"] = {
            "status": "needs_materialization",
            "message": _MATERIALIZATION_MESSAGE,
        }

    return json.dumps(payload, ensure_ascii=False)


class DeliveryArtifactStore:
    def __init__(self) -> None:
        self._by_session: dict[str, list[DeliveryArtifact]] = {}

    def record(
        self,
        *,
        session_key: str,
        tool_use_id: str,
        tool_name: str,
        content: str,
    ) -> list[DeliveryArtifact]:
        artifacts = extract_delivery_artifacts(tool_name, content)
        if artifacts:
            self._by_session.setdefault(session_key, []).extend(artifacts)
        return artifacts

    def media_tags_for_session(self, session_key: str) -> list[str]:
        return [artifact.media_tag for artifact in self._by_session.get(session_key, [])]

    def clear_session(self, session_key: str) -> None:
        self._by_session.pop(session_key, None)
