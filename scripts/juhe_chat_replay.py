#!/usr/bin/env python3
"""Offline Juhe chat replay harness for practical conversation testing."""

from __future__ import annotations

import argparse
import asyncio
import copy
import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable


SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


DEFAULT_GUID = "guid-123"
DEFAULT_NOTIFY_TYPE = 11010
DEFAULT_TEXT_MESSAGE_TYPE = 2
DEFAULT_ROOM_HISTORY_TAIL = 5


def _ensure_dict(value: Any, *, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _ensure_list(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Scenario file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Scenario JSON is invalid at {path}:{exc.lineno}:{exc.colno}") from exc
    return _ensure_dict(payload, label="scenario")


def _normalize_roomid(roomid: Any) -> str:
    text = str(roomid or "").strip()
    if text.upper().startswith("R:"):
        return text[2:]
    return text


def _coerce_text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def _normalize_message_step(message: Dict[str, Any], *, index: int, guid: str) -> Dict[str, Any]:
    payload = copy.deepcopy(message)
    payload.setdefault("msg_type", DEFAULT_TEXT_MESSAGE_TYPE)

    if "text" in payload and "content" not in payload:
        payload["content"] = payload.pop("text")

    if int(payload.get("msg_type") or 0) == DEFAULT_TEXT_MESSAGE_TYPE:
        payload.setdefault("content_type", DEFAULT_TEXT_MESSAGE_TYPE)
        payload["content"] = _coerce_text_content(payload.get("content"))

    if "roomid" in payload:
        payload["roomid"] = _normalize_roomid(payload.get("roomid"))

    if "id" not in payload and "msg_id" not in payload:
        payload["id"] = f"replay-step-{index}"

    payload.setdefault("sendtime", 1700000000 + index)

    return {
        "guid": guid,
        "notify_type": DEFAULT_NOTIFY_TYPE,
        "data": payload,
    }


def _normalize_event_step(event: Dict[str, Any], *, guid: str) -> Dict[str, Any]:
    payload = copy.deepcopy(event)
    payload.setdefault("guid", guid)
    payload.setdefault("notify_type", DEFAULT_NOTIFY_TYPE)
    return payload


def _normalize_expect(expect: Any) -> Dict[str, Any]:
    if expect is None:
        return {}
    return _ensure_dict(expect, label="step.expect")


def _normalize_mode(value: Any) -> str:
    text = str(value or "gateway").strip().lower().replace("-", "_")
    if text in {"gateway", "full", "full_flow"}:
        return "gateway"
    if text in {"adapter", "adapter_only", "ingress"}:
        return "adapter"
    raise ValueError("scenario.mode must be 'gateway' or 'adapter'")


def _normalize_env(env: Any) -> Dict[str, str]:
    if env is None:
        return {}
    payload = _ensure_dict(env, label="scenario.env")
    return {
        str(key): str(value)
        for key, value in payload.items()
        if value is not None
    }


def _normalize_agent(agent: Any) -> Dict[str, Any]:
    if agent is None:
        return {"mode": "mock"}
    payload = copy.deepcopy(_ensure_dict(agent, label="agent"))
    mode = str(payload.get("mode") or "mock").strip().lower().replace("-", "_")
    if mode in {"mock", "stub"}:
        payload["mode"] = "mock"
    elif mode in {"real", "live"}:
        payload["mode"] = "real"
    else:
        raise ValueError("agent.mode must be 'mock' or 'real'")
    return payload


def _normalize_step(step: Dict[str, Any], *, index: int, guid: str) -> Dict[str, Any]:
    normalized = copy.deepcopy(_ensure_dict(step, label=f"step[{index}]"))
    if "event" in normalized:
        event = _normalize_event_step(
            _ensure_dict(normalized["event"], label=f"step[{index}].event"),
            guid=guid,
        )
    elif "message" in normalized:
        event = _normalize_message_step(
            _ensure_dict(normalized["message"], label=f"step[{index}].message"),
            index=index,
            guid=guid,
        )
    else:
        raise ValueError(f"step[{index}] must include either 'message' or 'event'")

    return {
        "index": index,
        "name": str(normalized.get("name") or f"step-{index}"),
        "event": event,
        "expect": _normalize_expect(normalized.get("expect")),
        "agent": _normalize_agent(normalized.get("agent")),
    }


def normalize_scenario(payload: Dict[str, Any], *, source_path: Path | None = None) -> Dict[str, Any]:
    data = _ensure_dict(payload, label="scenario")
    adapter_extra = copy.deepcopy(_ensure_dict(data.get("adapter_extra") or {}, label="scenario.adapter_extra"))
    adapter_extra.setdefault("app_key", "replay-app-key")
    adapter_extra.setdefault("app_secret", "replay-app-secret")
    adapter_extra.setdefault("guid", str(data.get("guid") or DEFAULT_GUID))

    raw_steps = _ensure_list(data.get("steps"), label="scenario.steps")
    if not raw_steps:
        raise ValueError("scenario.steps must not be empty")

    steps = [
        _normalize_step(step, index=index, guid=str(adapter_extra["guid"]))
        for index, step in enumerate(raw_steps, start=1)
    ]

    return {
        "name": str(data.get("name") or (source_path.stem if source_path else "juhe-replay")),
        "description": str(data.get("description") or ""),
        "source_path": str(source_path) if source_path else None,
        "mode": _normalize_mode(data.get("mode")),
        "env": _normalize_env(data.get("env")),
        "agent": _normalize_agent(data.get("agent")),
        "adapter_extra": adapter_extra,
        "steps": steps,
    }


def load_scenario(path: str | os.PathLike[str]) -> Dict[str, Any]:
    scenario_path = Path(path).expanduser().resolve()
    return normalize_scenario(_read_json(scenario_path), source_path=scenario_path)


def _ensure_home_layout(hermes_home: Path) -> None:
    hermes_home.mkdir(parents=True, exist_ok=True)
    for name in ("sessions", "cron", "memories", "skills", "logs"):
        (hermes_home / name).mkdir(exist_ok=True)


def _reload_runtime_modules(hermes_home: Path) -> Dict[str, Any]:
    os.environ["HERMES_HOME"] = str(hermes_home)
    _ensure_home_layout(hermes_home)

    module_order = [
        "hermes_constants",
        "hermes_state",
        "gateway.config",
        "gateway.session",
        "gateway.juhe_cache",
        "gateway.juhe_room_memory",
        "gateway.platforms.base",
        "gateway.platforms.juhe",
        "gateway.run",
    ]
    for module_name in module_order:
        if module_name in sys.modules:
            importlib.reload(sys.modules[module_name])

    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.juhe_room_memory import get_room_memory_path
    from gateway.platforms.base import SendResult
    from gateway.platforms.juhe import JuheAdapter
    from gateway.run import GatewayRunner
    from gateway.session import SessionStore
    from hermes_state import SessionDB

    return {
        "GatewayConfig": GatewayConfig,
        "GatewayRunner": GatewayRunner,
        "Platform": Platform,
        "PlatformConfig": PlatformConfig,
        "SessionStore": SessionStore,
        "SessionDB": SessionDB,
        "JuheAdapter": JuheAdapter,
        "SendResult": SendResult,
        "get_room_memory_path": get_room_memory_path,
    }


def _extract_step_data(step: Dict[str, Any]) -> Dict[str, Any]:
    event = step.get("event") or {}
    data = event.get("data")
    if isinstance(data, dict):
        return data
    return {}


def _infer_conversation_id(step: Dict[str, Any], dispatched_event: Any | None) -> str | None:
    if dispatched_event is not None and getattr(dispatched_event, "source", None) is not None:
        return str(dispatched_event.source.chat_id or "") or None

    data = _extract_step_data(step)
    roomid = str(data.get("roomid") or "").strip()
    if roomid and roomid != "0":
        normalized = _normalize_roomid(roomid)
        return f"R:{normalized}" if normalized else None

    sender = str(data.get("sender") or "").strip()
    if sender:
        return f"S:{sender}"
    return None


def _infer_text_preview(step: Dict[str, Any], dispatched_event: Any | None) -> str:
    if dispatched_event is not None:
        return str(getattr(dispatched_event, "text", "") or "")

    data = _extract_step_data(step)
    content = data.get("content")
    if isinstance(content, str):
        return content
    return _coerce_text_content(content)


def _room_history_item_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "message_id": item.get("message_id"),
        "sender_id": item.get("sender_id"),
        "sender_name": item.get("sender_name"),
        "direction": item.get("direction"),
        "text_preview": item.get("text_preview"),
        "triggered": bool(item.get("triggered")),
        "consumed_for_context": bool(item.get("consumed_for_context")),
    }


def _transcript_item_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    summary = {
        "role": item.get("role"),
        "content": item.get("content"),
    }
    if "tool_call_id" in item:
        summary["tool_call_id"] = item.get("tool_call_id")
    if "name" in item:
        summary["name"] = item.get("name")
    return summary


def _build_completed_reply(outbound_messages: list[Dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in outbound_messages:
        content = str(item.get("content") or "").strip()
        if content:
            parts.append(content)
    return "\n\n".join(parts)


def _merge_agent_config(scenario_agent: Dict[str, Any], step_agent: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(scenario_agent or {})
    merged.update(copy.deepcopy(step_agent or {}))
    if "mode" not in merged:
        merged["mode"] = "mock"
    return merged


def _build_step_result(
    *,
    step: Dict[str, Any],
    handled: bool,
    dispatched_events: list[Any],
    outbound_messages: list[Dict[str, Any]],
    conversation_id: str | None,
    room_history: list[Dict[str, Any]],
    room_history_tail: int,
    room_memory_path: str | None,
    room_memory_text: str,
    session_key: str | None,
    chat_type: str | None,
    pending_context: str,
    agent_called: bool,
    agent_message: str,
    context_prompt: str,
    final_response: str,
    session_id: str | None,
    transcript: list[Dict[str, Any]],
    memory_updates: list[Dict[str, Any]],
) -> Dict[str, Any]:
    completed_reply = _build_completed_reply(outbound_messages)
    return {
        "index": step["index"],
        "name": step["name"],
        "handled": bool(handled),
        "dispatched": bool(dispatched_events),
        "dispatch_count": len(dispatched_events),
        "agent_called": bool(agent_called),
        "outbound_count": len(outbound_messages),
        "outbound": copy.deepcopy(outbound_messages),
        "conversation_id": conversation_id,
        "session_key": session_key,
        "session_id": session_id,
        "chat_type": chat_type,
        "text": _infer_text_preview(step, dispatched_events[0] if dispatched_events else None),
        "pending_context": pending_context,
        "agent_message": agent_message,
        "context_prompt": context_prompt,
        "final_response": final_response,
        "completed_reply": completed_reply,
        "room_history_size": len(room_history),
        "room_history_tail": [
            _room_history_item_summary(item)
            for item in room_history[-max(room_history_tail, 1):]
        ],
        "transcript_size": len(transcript),
        "transcript_tail": [
            _transcript_item_summary(item)
            for item in transcript[-max(room_history_tail, 1):]
        ],
        "room_memory_path": room_memory_path,
        "room_memory_exists": bool(room_memory_text),
        "room_memory_text": room_memory_text,
        "memory_update_count": len(memory_updates),
        "memory_updates": copy.deepcopy(memory_updates),
    }


def _load_room_state(
    *,
    db: Any,
    get_room_memory_path: Any,
    conversation_id: str | None,
    room_log_limit: int,
    room_history_tail: int,
) -> tuple[list[Dict[str, Any]], str | None, str]:
    room_history: list[Dict[str, Any]] = []
    room_memory_path: str | None = None
    room_memory_text = ""

    if conversation_id and conversation_id.startswith("R:"):
        room_history = db.get_room_history(
            "juhe",
            conversation_id,
            limit=max(room_log_limit, room_history_tail),
        )
        memory_path_obj = get_room_memory_path(conversation_id)
        room_memory_path = str(memory_path_obj)
        if memory_path_obj.exists():
            room_memory_text = memory_path_obj.read_text(encoding="utf-8")

    return room_history, room_memory_path, room_memory_text


def _build_mock_agent_result(
    *,
    agent_config: Dict[str, Any],
    step_name: str,
    message: str,
    history: list[Dict[str, Any]],
    session_id: str,
) -> Dict[str, Any]:
    result = copy.deepcopy(_ensure_dict(agent_config.get("result") or {}, label="agent.result"))
    final_response = str(
        result.get("final_response")
        or agent_config.get("final_response")
        or agent_config.get("response")
        or f"[mock agent] {step_name}"
    )
    history_copy = copy.deepcopy(history or [])
    if "messages" not in result:
        result["messages"] = history_copy + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": final_response},
        ]
    result.setdefault("final_response", final_response)
    result.setdefault("tools", copy.deepcopy(agent_config.get("tools") or []))
    result.setdefault("history_offset", len(history_copy))
    result.setdefault("last_prompt_tokens", int(agent_config.get("last_prompt_tokens") or 0))
    result.setdefault("api_calls", int(agent_config.get("api_calls") or 1))
    result.setdefault("session_id", session_id)
    return result


async def _wait_for_adapter_idle(adapter: Any, *, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + max(float(timeout_seconds), 0.1)
    while True:
        pending = [task for task in getattr(adapter, "_background_tasks", set()) if not task.done()]
        if not pending:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for adapter background tasks to finish")
        await asyncio.wait(pending, timeout=min(remaining, 0.5))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _compare_expected_value(
    failures: list[str],
    *,
    key: str,
    actual: Any,
    expected: Any,
) -> None:
    if actual != expected:
        failures.append(f"{key}: expected {expected!r}, got {actual!r}")


def _evaluate_expectations(step_result: Dict[str, Any], expect: Dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if not expect:
        return failures

    if "dispatched" in expect:
        _compare_expected_value(
            failures,
            key="dispatched",
            actual=step_result["dispatched"],
            expected=bool(expect["dispatched"]),
        )

    if "session_key" in expect:
        _compare_expected_value(
            failures,
            key="session_key",
            actual=step_result.get("session_key"),
            expected=str(expect["session_key"]),
        )

    if "room_history_size" in expect:
        _compare_expected_value(
            failures,
            key="room_history_size",
            actual=step_result.get("room_history_size"),
            expected=int(expect["room_history_size"]),
        )

    if "outbound_count" in expect:
        _compare_expected_value(
            failures,
            key="outbound_count",
            actual=step_result.get("outbound_count"),
            expected=int(expect["outbound_count"]),
        )

    if "agent_called" in expect:
        _compare_expected_value(
            failures,
            key="agent_called",
            actual=step_result.get("agent_called"),
            expected=bool(expect["agent_called"]),
        )

    if "pending_context_contains" in expect:
        pending_context = step_result.get("pending_context") or ""
        for snippet in _as_list(expect["pending_context_contains"]):
            if str(snippet) not in pending_context:
                failures.append(f"pending_context_contains: missing {snippet!r}")

    if "agent_message_contains" in expect:
        agent_message = step_result.get("agent_message") or ""
        for snippet in _as_list(expect["agent_message_contains"]):
            if str(snippet) not in agent_message:
                failures.append(f"agent_message_contains: missing {snippet!r}")

    if "final_response_contains" in expect:
        final_response = step_result.get("final_response") or ""
        for snippet in _as_list(expect["final_response_contains"]):
            if str(snippet) not in final_response:
                failures.append(f"final_response_contains: missing {snippet!r}")

    if "completed_reply_contains" in expect:
        completed_reply = step_result.get("completed_reply") or ""
        for snippet in _as_list(expect["completed_reply_contains"]):
            if str(snippet) not in completed_reply:
                failures.append(f"completed_reply_contains: missing {snippet!r}")

    return failures


async def _replay_adapter_scenario(
    *,
    scenario: Dict[str, Any],
    replay_home: Path,
    runtime: Dict[str, Any],
    room_history_tail: int,
) -> Dict[str, Any]:
    GatewayConfig = runtime["GatewayConfig"]
    Platform = runtime["Platform"]
    PlatformConfig = runtime["PlatformConfig"]
    SessionStore = runtime["SessionStore"]
    SessionDB = runtime["SessionDB"]
    JuheAdapter = runtime["JuheAdapter"]
    SendResult = runtime["SendResult"]
    get_room_memory_path = runtime["get_room_memory_path"]

    platform_config = PlatformConfig(enabled=True, extra=copy.deepcopy(scenario["adapter_extra"]))
    adapter = JuheAdapter(platform_config)
    gateway_config = GatewayConfig(platforms={Platform.JUHE: platform_config})
    session_store = SessionStore(replay_home / "sessions", gateway_config)

    dispatched_events: list[Any] = []
    outbound_messages: list[Dict[str, Any]] = []

    async def _record_inbound(event: Any) -> None:
        dispatched_events.append(event)

    async def _record_outbound(target: str, content: str) -> Any:
        outbound_messages.append({"target": target, "content": content})
        return SendResult(success=True, message_id=f"mock-send-{len(outbound_messages):03d}")

    adapter.handle_message = _record_inbound
    adapter.send = _record_outbound

    db = SessionDB(db_path=replay_home / "state.db")
    try:
        results: list[Dict[str, Any]] = []
        room_log_limit = int(scenario["adapter_extra"].get("room_log_limit") or 500)

        for step in scenario["steps"]:
            dispatched_events.clear()
            outbound_messages.clear()
            handled = await adapter._handle_callback_event(copy.deepcopy(step["event"]))

            dispatched_event = dispatched_events[0] if dispatched_events else None
            conversation_id = _infer_conversation_id(step, dispatched_event)
            room_history, room_memory_path, room_memory_text = _load_room_state(
                db=db,
                get_room_memory_path=get_room_memory_path,
                conversation_id=conversation_id,
                room_log_limit=room_log_limit,
                room_history_tail=room_history_tail,
            )

            session_key = None
            pending_context = ""
            chat_type = None
            if dispatched_event is not None:
                session_key = session_store._generate_session_key(dispatched_event.source)
                pending_context = str(getattr(dispatched_event, "_juhe_pending_context_text", "") or "")
                chat_type = str(dispatched_event.source.chat_type or "")

            step_result = _build_step_result(
                step=step,
                handled=bool(handled),
                dispatched_events=dispatched_events,
                outbound_messages=outbound_messages,
                conversation_id=conversation_id,
                room_history=room_history,
                room_history_tail=room_history_tail,
                room_memory_path=room_memory_path,
                room_memory_text=room_memory_text,
                session_key=session_key,
                chat_type=chat_type,
                pending_context=pending_context,
                agent_called=False,
                agent_message="",
                context_prompt="",
                final_response="",
                session_id=None,
                transcript=[],
                memory_updates=[],
            )
            step_result["failures"] = _evaluate_expectations(step_result, step["expect"])
            step_result["passed"] = not step_result["failures"]
            results.append(step_result)

        return results
    finally:
        db.close()


async def _replay_gateway_scenario(
    *,
    scenario: Dict[str, Any],
    replay_home: Path,
    runtime: Dict[str, Any],
    room_history_tail: int,
) -> Dict[str, Any]:
    GatewayConfig = runtime["GatewayConfig"]
    Platform = runtime["Platform"]
    PlatformConfig = runtime["PlatformConfig"]
    SessionDB = runtime["SessionDB"]
    JuheAdapter = runtime["JuheAdapter"]
    SendResult = runtime["SendResult"]
    get_room_memory_path = runtime["get_room_memory_path"]
    GatewayRunner = runtime["GatewayRunner"]
    juhe_module = sys.modules["gateway.platforms.juhe"]

    platform_config = PlatformConfig(enabled=True, extra=copy.deepcopy(scenario["adapter_extra"]))
    gateway_config = GatewayConfig(platforms={Platform.JUHE: platform_config})
    runner = GatewayRunner(gateway_config)
    adapter = JuheAdapter(platform_config)
    runner.adapters[Platform.JUHE] = adapter
    runner.delivery_router.adapters = runner.adapters

    dispatched_events: list[Any] = []
    outbound_messages: list[Dict[str, Any]] = []
    agent_invocations: list[Dict[str, Any]] = []
    memory_updates: list[Dict[str, Any]] = []
    current_step: Dict[str, Any] | None = None

    async def _record_outbound(chat_id: str, content: str, reply_to: str | None = None, metadata=None) -> Any:
        outbound_messages.append(
            {
                "target": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": copy.deepcopy(metadata),
            }
        )
        return SendResult(success=True, message_id=f"mock-send-{len(outbound_messages):03d}")

    async def _record_runner_message(event: Any) -> Any:
        dispatched_events.append(event)
        return await runner._handle_message(event)

    original_run_agent = runner._run_agent

    async def _capturing_run_agent(**kwargs):
        step = current_step or {}
        effective_agent = _merge_agent_config(scenario["agent"], step.get("agent") or {})
        call_record = {
            "message": kwargs.get("message", ""),
            "context_prompt": kwargs.get("context_prompt", ""),
            "history": copy.deepcopy(kwargs.get("history") or []),
            "session_id": kwargs.get("session_id"),
            "session_key": kwargs.get("session_key"),
        }
        if effective_agent.get("mode") == "real":
            result = await original_run_agent(**kwargs)
        else:
            result = _build_mock_agent_result(
                agent_config=effective_agent,
                step_name=str(step.get("name") or "step"),
                message=str(kwargs.get("message") or ""),
                history=kwargs.get("history") or [],
                session_id=str(kwargs.get("session_id") or ""),
            )
        call_record["result"] = copy.deepcopy(result)
        agent_invocations.append(call_record)
        return result

    def _record_memory_update(room_id: str, **kwargs) -> None:
        payload = {"room_id": room_id, **copy.deepcopy(kwargs)}
        memory_updates.append(payload)

    adapter.send = _record_outbound
    adapter.set_message_handler(_record_runner_message)
    runner._run_agent = _capturing_run_agent

    original_memory_scheduler = juhe_module.schedule_room_memory_update
    juhe_module.schedule_room_memory_update = _record_memory_update

    db = SessionDB(db_path=replay_home / "state.db")
    try:
        results: list[Dict[str, Any]] = []
        room_log_limit = int(scenario["adapter_extra"].get("room_log_limit") or 500)

        for step in scenario["steps"]:
            current_step = step
            dispatched_events.clear()
            outbound_messages.clear()
            agent_invocations.clear()
            memory_updates.clear()

            handled = await adapter._handle_callback_event(copy.deepcopy(step["event"]))
            await _wait_for_adapter_idle(adapter)

            dispatched_event = dispatched_events[0] if dispatched_events else None
            conversation_id = _infer_conversation_id(step, dispatched_event)
            room_history, room_memory_path, room_memory_text = _load_room_state(
                db=db,
                get_room_memory_path=get_room_memory_path,
                conversation_id=conversation_id,
                room_log_limit=room_log_limit,
                room_history_tail=room_history_tail,
            )

            session_key = None
            pending_context = ""
            chat_type = None
            session_id = None
            transcript: list[Dict[str, Any]] = []
            if dispatched_event is not None:
                session_key = runner.session_store._generate_session_key(dispatched_event.source)
                pending_context = str(getattr(dispatched_event, "_juhe_pending_context_text", "") or "")
                chat_type = str(dispatched_event.source.chat_type or "")
            if agent_invocations:
                session_id = str(agent_invocations[0].get("session_id") or "") or None
                if session_id:
                    transcript = runner.session_store.load_transcript(session_id)

            agent_message = str(agent_invocations[0].get("message") or "") if agent_invocations else ""
            context_prompt = str(agent_invocations[0].get("context_prompt") or "") if agent_invocations else ""
            final_response = str(
                ((agent_invocations[0].get("result") or {}).get("final_response") if agent_invocations else "")
                or ""
            )

            step_result = _build_step_result(
                step=step,
                handled=bool(handled),
                dispatched_events=dispatched_events,
                outbound_messages=outbound_messages,
                conversation_id=conversation_id,
                room_history=room_history,
                room_history_tail=room_history_tail,
                room_memory_path=room_memory_path,
                room_memory_text=room_memory_text,
                session_key=session_key,
                chat_type=chat_type,
                pending_context=pending_context,
                agent_called=bool(agent_invocations),
                agent_message=agent_message,
                context_prompt=context_prompt,
                final_response=final_response,
                session_id=session_id,
                transcript=transcript,
                memory_updates=memory_updates,
            )
            step_result["failures"] = _evaluate_expectations(step_result, step["expect"])
            step_result["passed"] = not step_result["failures"]
            results.append(step_result)

        return results
    finally:
        juhe_module.schedule_room_memory_update = original_memory_scheduler
        db.close()


def _push_env_overrides(env_updates: Dict[str, str]) -> Any:
    previous: Dict[str, str | None] = {}
    for key, value in env_updates.items():
        previous[key] = os.environ.get(key)
        os.environ[key] = str(value)

    def _restore() -> None:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value

    return _restore


async def replay_scenario(
    scenario_or_payload: Dict[str, Any] | str | os.PathLike[str],
    *,
    hermes_home: str | os.PathLike[str] | None = None,
    room_history_tail: int = DEFAULT_ROOM_HISTORY_TAIL,
    mode_override: str | None = None,
    agent_mode_override: str | None = None,
) -> Dict[str, Any]:
    scenario = (
        load_scenario(scenario_or_payload)
        if isinstance(scenario_or_payload, (str, os.PathLike))
        else normalize_scenario(_ensure_dict(scenario_or_payload, label="scenario"))
    )
    if mode_override:
        scenario["mode"] = _normalize_mode(mode_override)
    if agent_mode_override:
        scenario["agent"] = _merge_agent_config(scenario["agent"], {"mode": agent_mode_override})

    replay_home = Path(hermes_home or tempfile.mkdtemp(prefix="hermes-juhe-replay-")).expanduser().resolve()
    restore_env = _push_env_overrides(scenario.get("env") or {})
    try:
        runtime = _reload_runtime_modules(replay_home)
        if scenario["mode"] == "adapter":
            steps = await _replay_adapter_scenario(
                scenario=scenario,
                replay_home=replay_home,
                runtime=runtime,
                room_history_tail=room_history_tail,
            )
        else:
            steps = await _replay_gateway_scenario(
                scenario=scenario,
                replay_home=replay_home,
                runtime=runtime,
                room_history_tail=room_history_tail,
            )

        return {
            "scenario": scenario["name"],
            "description": scenario["description"],
            "scenario_path": scenario["source_path"],
            "mode": scenario["mode"],
            "agent_mode": scenario["agent"].get("mode"),
            "hermes_home": str(replay_home),
            "all_expectations_passed": all(step["passed"] for step in steps),
            "steps": steps,
        }
    finally:
        restore_env()


def _write_output(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay editable Juhe chat scenarios from JSON files.")
    parser.add_argument("scenario", help="Path to a scenario JSON file.")
    parser.add_argument(
        "--hermes-home",
        help="Replay workspace directory. Defaults to a fresh temp directory.",
    )
    parser.add_argument(
        "--output",
        help="Optional path to write the replay result JSON.",
    )
    parser.add_argument(
        "--room-history-tail",
        type=int,
        default=DEFAULT_ROOM_HISTORY_TAIL,
        help="How many room-log entries to include in each step summary.",
    )
    parser.add_argument(
        "--mode",
        choices=["gateway", "adapter"],
        help="Override the scenario mode.",
    )
    parser.add_argument(
        "--agent-mode",
        choices=["mock", "real"],
        help="Override whether gateway mode uses a mocked or real agent run.",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    result = asyncio.run(
        replay_scenario(
            args.scenario,
            hermes_home=args.hermes_home,
            room_history_tail=max(int(args.room_history_tail or 0), 1),
            mode_override=args.mode,
            agent_mode_override=args.agent_mode,
        )
    )

    if args.output:
        _write_output(Path(args.output).expanduser().resolve(), result)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["all_expectations_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
