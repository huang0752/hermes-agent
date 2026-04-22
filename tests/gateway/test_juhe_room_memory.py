"""Tests for Juhe per-room MEMORY.md helpers."""

from types import SimpleNamespace

from gateway.juhe_room_memory import (
    MemoryRouteDecision,
    apply_room_memory_decision,
    get_room_memory_path,
    update_room_memory,
)


def _llm_response(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def test_update_room_memory_adds_entry_when_review_returns_add(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "memory_routing.call_llm",
        lambda **_kwargs: _llm_response(
            '{"target":"room","reason":"room_rule","explicit":false,"confidence":"high","action":"add","content":"客户群统一约定：需求确认以群内最终确认版本为准。"}'
        ),
    )

    result = update_room_memory(
        "R:2001",
        current_message="这次还是按群里最终确认版本走。",
        pending_context="",
        assistant_response="收到，我会按群里的最终确认版本执行。",
        char_limit=2400,
    )

    assert result["action"] == "add"
    memory_path = get_room_memory_path("R:2001")
    assert memory_path.exists()
    assert "最终确认版本" in memory_path.read_text(encoding="utf-8")


def test_update_room_memory_skips_when_review_returns_skip(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "memory_routing.call_llm",
        lambda **_kwargs: _llm_response('{"target":"none","reason":"transient","explicit":false,"confidence":"high","action":"skip"}'),
    )

    result = update_room_memory(
        "R:2001",
        current_message="好的，明天再说。",
        pending_context="",
        assistant_response="收到，明天再继续。",
        char_limit=2400,
    )

    assert result["action"] == "skip"
    assert get_room_memory_path("R:2001").exists() is False


def test_update_room_memory_skips_when_router_targets_global(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "memory_routing.call_llm",
        lambda **_kwargs: _llm_response(
            '{"target":"global","reason":"workspace_rule","explicit":false,"confidence":"high","action":"add","content":"Juhe 回复不要带 Markdown 加粗符号。"}'
        ),
    )

    result = update_room_memory(
        "R:2001",
        current_message="以后 Juhe 回复不要带 Markdown 加粗符号。",
        pending_context="",
        assistant_response="收到，后续会按纯文本发送。",
        char_limit=2400,
    )

    assert result["action"] == "skip"
    assert result["target"] == "global"
    assert get_room_memory_path("R:2001").exists() is False


def test_apply_room_memory_decision_writes_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = apply_room_memory_decision(
        "R:2001",
        MemoryRouteDecision(
            target="room",
            reason="room_shorthand",
            explicit=True,
            confidence="high",
            action="add",
            content="本群约定：发“1”表示直接开始生成证书闭环。",
        ),
        char_limit=2400,
    )

    assert result["action"] == "add"
    memory_path = get_room_memory_path("R:2001")
    assert memory_path.exists()
    assert "发“1”表示直接开始生成证书闭环" in memory_path.read_text(encoding="utf-8")
