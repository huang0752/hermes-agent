"""Shared durable-memory routing helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from agent.auxiliary_client import call_llm

MemoryTarget = Literal["user", "global", "room", "none"]
MemoryConfidence = Literal["high", "medium", "low"]
MemoryAction = Literal["skip", "add", "replace", "remove"]

_EXPLICIT_MEMORY_PATTERNS = (
    r"记一下",
    r"记住",
    r"记下",
    r"帮我记",
    r"帮忙记",
    r"以后.*(?:就|都|默认)",
)
_MEMORY_QUESTION_PATTERNS = (
    r"还记得",
    r"记得.*吗",
    r"有没有说过",
    r"是否聊过",
)

MEMORY_ROUTING_GUIDANCE = """Memory routing targets:
- user: stable facts about the primary user, such as communication style, work habits, and long-lived preferences that remain true across chats.
- global: agent/workspace-wide operating rules, platform constraints, tool quirks, and reusable workflow knowledge. Never use this for one specific room.
- room: shared long-lived facts for the current Juhe room only, such as group shorthand, stable terminology mappings, or room-wide business rules.
- none: transient task state, approvals, dates, one-off parameters, progress, or anything not durable enough for long-term memory.

Routing rules:
- User-personality or collaboration-style facts -> user
- Workspace/platform/tooling rules -> global
- Room-shared shorthand or long-lived room conventions -> room
- Single-run execution state -> none
- For an explicit remember request inside a group chat, if scope is genuinely unclear, prefer room

Confidence:
- high: clear unambiguous fit
- medium: likely fit
- low: ambiguous or weak evidence
"""

BUILTIN_MEMORY_REVIEW_GUIDANCE = (
    MEMORY_ROUTING_GUIDANCE
    + "\nBuilt-in memory tool mapping:\n"
      "- target 'user' maps to USER.md\n"
      "- target 'global' maps to MEMORY.md\n"
      "- target 'room' is NOT available through the built-in memory tool here; skip room-only facts in this reviewer.\n"
      "- Never save transient execution state.\n"
)


@dataclass(frozen=True)
class MemoryRouteDecision:
    target: MemoryTarget
    reason: str
    explicit: bool
    confidence: MemoryConfidence
    action: MemoryAction = "skip"
    content: str = ""
    match: str = ""


def is_explicit_memory_request(text: Any) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    lowered = value.lower()
    if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in _MEMORY_QUESTION_PATTERNS):
        return False
    return any(re.search(pattern, value, re.IGNORECASE) for pattern in _EXPLICIT_MEMORY_PATTERNS)


def _extract_json_object(content: str) -> dict[str, Any]:
    text = str(content or "").strip()
    if not text:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\})", text, re.DOTALL)
        if not match:
            return {}
        try:
            value = json.loads(match.group(1))
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}


def _coerce_target(value: Any) -> MemoryTarget:
    text = str(value or "").strip().lower()
    if text == "memory":
        return "global"
    if text in {"user", "global", "room", "none"}:
        return text
    return "none"


def _coerce_confidence(value: Any) -> MemoryConfidence:
    text = str(value or "").strip().lower()
    if text in {"high", "medium", "low"}:
        return text
    return "low"


def _coerce_action(value: Any) -> MemoryAction:
    text = str(value or "").strip().lower()
    if text in {"skip", "add", "replace", "remove"}:
        return text
    return "skip"


def route_memory_decision(
    *,
    current_message: str,
    assistant_response: str,
    explicit: bool,
    chat_scope: str,
    existing_room_memory: str = "",
    pending_context: str = "",
    relevant_room_history: str = "",
    timeout: float = 30.0,
    task: str = "memory_route_review",
) -> MemoryRouteDecision:
    response = call_llm(
        task=task,
        messages=[
            {
                "role": "system",
                "content": (
                    "You classify whether a candidate memory belongs to the user profile, "
                    "global agent memory, room memory, or nowhere durable.\n\n"
                    f"{MEMORY_ROUTING_GUIDANCE}\n"
                    "Return JSON only with keys: target, reason, explicit, confidence, action, content, match.\n"
                    "Allowed actions: skip, add, replace, remove.\n"
                    "If target is none, action should be skip.\n"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Explicit remember request: {bool(explicit)}\n"
                    f"Chat scope: {chat_scope}\n\n"
                    f"Current message:\n{current_message or '(empty)'}\n\n"
                    f"Assistant reply:\n{assistant_response or '(empty)'}\n\n"
                    f"Existing room memory:\n{existing_room_memory or '(empty)'}\n\n"
                    f"Pending context:\n{pending_context or '(none)'}\n\n"
                    f"Relevant room history:\n{relevant_room_history or '(none)'}"
                ),
            },
        ],
        max_tokens=300,
        temperature=0.0,
        timeout=timeout,
    )
    payload = _extract_json_object(response.choices[0].message.content or "")

    decision = MemoryRouteDecision(
        target=_coerce_target(payload.get("target")),
        reason=str(payload.get("reason") or "").strip() or "unspecified",
        explicit=bool(explicit),
        confidence=_coerce_confidence(payload.get("confidence")),
        action=_coerce_action(payload.get("action")),
        content=str(payload.get("content") or "").strip(),
        match=str(payload.get("match") or payload.get("old_text") or "").strip(),
    )

    if decision.target == "none":
        return MemoryRouteDecision(
            target="none",
            reason=decision.reason,
            explicit=decision.explicit,
            confidence=decision.confidence,
            action="skip",
            content=decision.content,
            match=decision.match,
        )

    if explicit and chat_scope == "group" and decision.confidence == "low" and decision.content:
        return MemoryRouteDecision(
            target="room",
            reason="group_explicit_fallback",
            explicit=True,
            confidence="low",
            action="add" if decision.action == "skip" else decision.action,
            content=decision.content,
            match=decision.match,
        )

    return decision
