"""Tests for shared memory routing helpers."""

from types import SimpleNamespace

from memory_routing import (
    MemoryRouteDecision,
    is_explicit_memory_request,
    route_memory_decision,
)


def _llm_response(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def test_is_explicit_memory_request_detects_remember_phrases():
    assert is_explicit_memory_request("@魔法老头 记一下，以后我发 1 就是直接生成证书")
    assert is_explicit_memory_request("帮我记住：Juhe 回复不要带 Markdown")
    assert is_explicit_memory_request("以后记得这个群里食品五体系就是那五张")
    assert is_explicit_memory_request("记下这个约定")
    assert not is_explicit_memory_request("你还记得之前说过 logo 吗")
    assert not is_explicit_memory_request("这次洲际日期是 2.15")


def test_route_memory_decision_falls_back_to_room_for_low_confidence_group_explicit(monkeypatch):
    monkeypatch.setattr(
        "memory_routing.call_llm",
        lambda **_kwargs: _llm_response(
            '{"target":"user","reason":"uncertain","explicit":true,"confidence":"low","action":"add","content":"发“1”表示直接开始生成证书闭环。"}'
        ),
    )

    decision = route_memory_decision(
        current_message="@魔法老头 记一下，以后我发 1 就是直接生成证书",
        assistant_response="收到。",
        explicit=True,
        chat_scope="group",
    )

    assert isinstance(decision, MemoryRouteDecision)
    assert decision.target == "room"
    assert decision.reason == "group_explicit_fallback"
    assert decision.explicit is True
    assert decision.confidence == "low"
    assert "发“1”" in decision.content
