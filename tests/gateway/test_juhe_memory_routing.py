"""Tests for Juhe explicit memory routing helpers in the gateway runner."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource


def _build_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.JUHE: PlatformConfig(
                enabled=True,
                extra={},
            )
        }
    )
    runner._session_db = None
    return runner


def test_should_surface_background_review_is_disabled_for_juhe():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)

    juhe_source = SessionSource(
        platform=Platform.JUHE,
        chat_id="R:2001",
        chat_type="group",
        user_id="1001",
    )
    telegram_source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="1234",
        chat_type="dm",
        user_id="1001",
    )

    assert GatewayRunner._should_surface_background_review(runner, juhe_source) is False
    assert GatewayRunner._should_surface_background_review(runner, telegram_source) is True


def test_apply_juhe_explicit_memory_note_appends_room_confirmation(monkeypatch):
    from gateway.run import GatewayRunner

    runner = _build_runner()
    event = SimpleNamespace()
    source = SessionSource(
        platform=Platform.JUHE,
        chat_id="R:2001",
        chat_type="group",
        user_id="1001",
    )
    memory_store = MagicMock()

    monkeypatch.setattr("gateway.run.is_explicit_memory_request", lambda _text: True)
    monkeypatch.setattr(
        "gateway.run.route_memory_decision",
        lambda **_kwargs: SimpleNamespace(
            target="room",
            reason="room_shorthand",
            explicit=True,
            confidence="high",
            action="add",
            content="本群约定：发“1”表示直接开始生成证书闭环。",
            match="",
        ),
    )
    monkeypatch.setattr(
        "gateway.run.apply_room_memory_decision",
        lambda room_id, decision, char_limit=2400: {"success": True, "action": "add", "path": f"/tmp/{room_id}.md"},
    )

    response = GatewayRunner._maybe_apply_juhe_explicit_memory(
        runner,
        event=event,
        source=source,
        message_text="@魔法老头 记一下，以后我发 1 就是直接生成证书",
        response_text="收到，我后续直接按这个口令执行。",
        memory_store=memory_store,
        room_memory_char_limit=2400,
    )

    assert response.endswith("已记到本群记忆。")
    assert getattr(event, "_juhe_skip_auto_room_memory", False) is True


def test_attach_juhe_recall_context_skips_room_history_for_normal_certificate_request():
    from gateway.run import GatewayRunner

    runner = _build_runner()
    runner._session_db = MagicMock()
    runner._session_db.search_room_history.return_value = [
        {
            "message_id": "old-hit-1",
            "created_at": 1776852982.0,
            "sender_name": "Hermes",
            "text_preview": "安阳宝华冶金耐材有限公司 三体系证书已出证完成。",
            "triggered": True,
            "direction": "outbound",
        }
    ]

    event = SimpleNamespace(message_id="current-msg")
    source = SessionSource(
        platform=Platform.JUHE,
        chat_id="R:2001",
        chat_type="group",
        user_id="1001",
    )
    session_entry = SimpleNamespace(session_id="session-current")

    GatewayRunner._attach_juhe_recall_context(
        runner,
        event=event,
        source=source,
        session_entry=session_entry,
        message_text="深圳市万洁环境产业有限公司 中天三体系 26年4.12 范围全要",
        is_new_session=False,
    )

    assert getattr(event, "_juhe_relevant_room_history_text", "") == ""
    assert getattr(event, "_juhe_relevant_prior_agent_turns_text", "") == ""
    runner._session_db.search_prior_session_messages.assert_not_called()


def test_attach_juhe_recall_context_keeps_history_for_explicit_history_question():
    from gateway.run import GatewayRunner

    runner = _build_runner()
    runner._session_db = MagicMock()
    runner._session_db.search_room_history.return_value = [
        {
            "message_id": "old-hit-1",
            "created_at": 1776852982.0,
            "sender_name": "Hermes",
            "text_preview": "之前讨论过：深圳市万洁环境产业有限公司按中天三体系处理。",
            "triggered": True,
            "direction": "outbound",
        }
    ]
    runner._session_db.search_prior_session_messages.return_value = []

    event = SimpleNamespace(message_id="current-msg")
    source = SessionSource(
        platform=Platform.JUHE,
        chat_id="R:2001",
        chat_type="group",
        user_id="1001",
    )
    session_entry = SimpleNamespace(session_id="session-current")

    GatewayRunner._attach_juhe_recall_context(
        runner,
        event=event,
        source=source,
        session_entry=session_entry,
        message_text="之前那个公司怎么处理的",
        is_new_session=False,
    )

    history_text = getattr(event, "_juhe_relevant_room_history_text", "")
    assert "History answer rule" in history_text
    assert "之前讨论过" in history_text
