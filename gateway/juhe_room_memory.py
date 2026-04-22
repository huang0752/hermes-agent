"""Per-room durable memory for Juhe group conversations."""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, List

from agent.auxiliary_client import call_llm
from hermes_constants import get_hermes_home
from memory_routing import MemoryRouteDecision, route_memory_decision

logger = logging.getLogger(__name__)

DEFAULT_ROOM_MEMORY_CHAR_LIMIT = 2400
_ROOM_MEMORY_LOCK = threading.Lock()

_COMPACT_PROMPT = """Rewrite this room MEMORY.md into a shorter bullet list.

Rules:
- keep only durable room-level facts
- remove duplicates
- stay concise
- preserve important constraints and decisions
- output JSON only: {"entries":["...", "..."]}
"""


def _normalize_room_id(room_id: str) -> str:
    text = str(room_id or "").strip()
    if text.upper().startswith("R:"):
        return text[2:]
    return text


def get_room_memory_path(room_id: str) -> Path:
    normalized_room_id = _normalize_room_id(room_id)
    return get_hermes_home() / "memories" / "juhe" / "rooms" / normalized_room_id / "MEMORY.md"


def load_room_memory(room_id: str) -> str:
    path = get_room_memory_path(room_id)
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _extract_json_object(content: str) -> Dict[str, Any]:
    text = str(content or "").strip()
    if not text:
        return {"action": "skip"}
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {"action": "skip"}
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\})", text, re.DOTALL)
        if not match:
            return {"action": "skip"}
        try:
            value = json.loads(match.group(1))
            return value if isinstance(value, dict) else {"action": "skip"}
        except json.JSONDecodeError:
            return {"action": "skip"}


def _read_entries(path: Path) -> List[str]:
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return []

    entries: List[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("- "):
            entry = _clean_entry(line[2:])
            if entry:
                entries.append(entry)
    return entries


def _clean_entry(content: str) -> str:
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    return text.strip("- ").strip()


def _render_entries(entries: List[str]) -> str:
    if not entries:
        return ""
    unique_entries = list(dict.fromkeys(_clean_entry(entry) for entry in entries if _clean_entry(entry)))
    if not unique_entries:
        return ""
    return "# MEMORY\n\n" + "\n".join(f"- {entry}" for entry in unique_entries)


def _write_memory(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def _apply_decision(entries: List[str], decision: Dict[str, Any]) -> List[str]:
    action = str(decision.get("action") or "skip").strip().lower()
    content = _clean_entry(str(decision.get("content") or ""))
    match = _clean_entry(str(decision.get("match") or decision.get("target") or ""))

    updated = list(entries)
    if action == "skip":
        return updated

    if action == "add":
        if content and content not in updated:
            updated.append(content)
        return updated

    if action == "replace":
        if not content:
            return updated
        for index, existing in enumerate(updated):
            if match and match in existing:
                updated[index] = content
                return updated
        if content not in updated:
            updated.append(content)
        return updated

    if action == "remove":
        if not match:
            return updated
        return [entry for entry in updated if match not in entry]

    return updated


def _decision_to_payload(decision: MemoryRouteDecision) -> Dict[str, Any]:
    return {
        "action": decision.action,
        "content": decision.content,
        "match": decision.match,
    }


def _compact_entries(entries: List[str], *, char_limit: int) -> List[str]:
    rendered = _render_entries(entries)
    if len(rendered) <= char_limit:
        return entries

    try:
        response = call_llm(
            task="juhe_room_memory_compact",
            messages=[
                {"role": "system", "content": _COMPACT_PROMPT},
                {"role": "user", "content": f"Char limit: {char_limit}\n\nCurrent memory:\n{rendered}"},
            ],
            max_tokens=500,
            temperature=0.0,
            timeout=30.0,
        )
        payload = _extract_json_object(response.choices[0].message.content or "")
        compacted_entries = payload.get("entries")
        if isinstance(compacted_entries, list):
            normalized = [_clean_entry(item) for item in compacted_entries if _clean_entry(item)]
            if normalized and len(_render_entries(normalized)) <= char_limit:
                return normalized
    except Exception as exc:
        logger.debug("Juhe room memory compaction failed: %s", exc)

    fallback = list(entries)
    while fallback and len(_render_entries(fallback)) > char_limit:
        fallback.pop(0)
    return fallback


def _apply_room_memory_decision_locked(
    path: Path,
    decision: MemoryRouteDecision,
    *,
    char_limit: int,
) -> Dict[str, Any]:
    existing_entries = _read_entries(path)
    updated_entries = _apply_decision(existing_entries, _decision_to_payload(decision))
    updated_entries = _compact_entries(updated_entries, char_limit=char_limit)
    rendered = _render_entries(updated_entries)

    if rendered:
        _write_memory(path, rendered)
    elif path.exists():
        path.unlink()

    return {
        "success": True,
        "action": decision.action,
        "path": str(path),
        "entries": updated_entries,
    }


def apply_room_memory_decision(
    room_id: str,
    decision: MemoryRouteDecision,
    *,
    char_limit: int = DEFAULT_ROOM_MEMORY_CHAR_LIMIT,
) -> Dict[str, Any]:
    path = get_room_memory_path(room_id)
    with _ROOM_MEMORY_LOCK:
        return _apply_room_memory_decision_locked(path, decision, char_limit=char_limit)


def update_room_memory(
    room_id: str,
    *,
    current_message: str,
    pending_context: str,
    relevant_room_history: str = "",
    assistant_response: str,
    char_limit: int = DEFAULT_ROOM_MEMORY_CHAR_LIMIT,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    if not str(current_message or "").strip() or not str(assistant_response or "").strip():
        return {"action": "skip", "reason": "empty_turn"}

    path = get_room_memory_path(room_id)
    with _ROOM_MEMORY_LOCK:
        existing_memory = _render_entries(_read_entries(path))

        decision = route_memory_decision(
            current_message=current_message,
            assistant_response=assistant_response,
            explicit=False,
            chat_scope="group",
            existing_room_memory=existing_memory,
            pending_context=pending_context,
            relevant_room_history=relevant_room_history,
            timeout=timeout,
            task="juhe_room_memory_review",
        )
        if decision.target != "room" or decision.action == "skip" or decision.confidence == "low":
            return {
                "action": "skip",
                "path": str(path),
                "target": decision.target,
                "reason": decision.reason,
                "confidence": decision.confidence,
            }

        result = _apply_room_memory_decision_locked(path, decision, char_limit=char_limit)
        result.update(
            {
                "target": decision.target,
                "reason": decision.reason,
                "confidence": decision.confidence,
            }
        )
        return result


def schedule_room_memory_update(
    room_id: str,
    *,
    current_message: str,
    pending_context: str,
    relevant_room_history: str = "",
    assistant_response: str,
    char_limit: int = DEFAULT_ROOM_MEMORY_CHAR_LIMIT,
) -> None:
    def _run() -> None:
        try:
            update_room_memory(
                room_id,
                current_message=current_message,
                pending_context=pending_context,
                relevant_room_history=relevant_room_history,
                assistant_response=assistant_response,
                char_limit=char_limit,
            )
        except Exception as exc:
            logger.debug("Juhe room memory update failed for %s: %s", room_id, exc)

    thread = threading.Thread(
        target=_run,
        daemon=True,
        name=f"juhe-room-memory:{_normalize_room_id(room_id)}",
    )
    thread.start()
