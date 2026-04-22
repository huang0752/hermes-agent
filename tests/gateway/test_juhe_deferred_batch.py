"""Tests for Juhe busy-session deferred follow-up batching."""

from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.JUHE: PlatformConfig(enabled=True, token="***")}
    )
    runner.config.group_sessions_per_user = False
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._juhe_deferred_batch = {}
    runner._pending_approvals = {}
    runner._busy_ack_ts = {}
    runner._voice_mode = {}
    runner._background_tasks = set()
    runner._draining = False
    runner._restart_requested = False
    runner._restart_task_started = False
    runner._restart_detached = False
    runner._restart_via_service = False
    runner._restart_drain_timeout = 0.0
    runner._stop_task = None
    runner._exit_code = None
    runner._update_runtime_status = MagicMock()
    runner._is_user_authorized = lambda _source: True
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.session_store = MagicMock()
    runner.delivery_router = MagicMock()
    return runner


def _make_event(
    *,
    text: str,
    message_id: str,
    chat_id: str = "R:2001",
    user_id: str = "alice",
    user_name: str = "Alice",
) -> MessageEvent:
    source = SessionSource(
        platform=Platform.JUHE,
        chat_id=chat_id,
        chat_type="group",
        user_id=user_id,
        user_name=user_name,
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id=message_id,
    )


def test_juhe_deferred_batches_are_isolated_by_session_key():
    runner = _make_runner()
    event_a = _make_event(text="logo用刚发的", message_id="juhe-a")
    event_b = _make_event(text="另外做一套", message_id="juhe-b", chat_id="R:2002", user_id="bob", user_name="Bob")

    runner._enqueue_juhe_deferred_event("agent:main:juhe:group:R:2001", event_a)
    runner._enqueue_juhe_deferred_event("agent:main:juhe:group:R:2002", event_b)

    assert list(runner._juhe_deferred_batch["agent:main:juhe:group:R:2001"]) == [event_a]
    assert list(runner._juhe_deferred_batch["agent:main:juhe:group:R:2002"]) == [event_b]


@pytest.mark.asyncio
async def test_render_juhe_deferred_batch_merges_processed_blocks_with_frozen_history():
    runner = _make_runner()
    history = [{"role": "assistant", "content": "earlier turn"}]
    event1 = _make_event(text="logo用刚发的", message_id="juhe-1", user_name="Alice")
    event2 = _make_event(text="另外做一套", message_id="juhe-2", user_id="bob", user_name="Bob")
    setattr(event1, "_juhe_pending_context_text", "Bob: 4.12")
    setattr(event2, "_juhe_pending_context_text", "Charlie: 安阳宝华")

    seen_histories = []

    async def fake_prepare(*, event, source, history):
        seen_histories.append(history)
        if event is event1:
            return "[Alice] logo用刚发的\n附件摘要A"
        return "[Bob] 另外做一套\n附件摘要B"

    runner._prepare_inbound_message_text = fake_prepare

    rendered = await runner._render_juhe_deferred_batch(
        deferred_events=[event1, event2],
        updated_history=history,
        fallback_source=event1.source,
    )

    assert seen_histories == [history, history]
    assert rendered["source"] == event2.source
    assert rendered["message_id"] == "juhe-2"
    assert rendered["message"].startswith("[Messages received while you were working]")
    assert "\n\n1.\n" in rendered["message"]
    assert "\n\n2.\n" in rendered["message"]
    assert rendered["message"].count("[Alice]") == 1
    assert "[Recent room context]\nBob: 4.12" in rendered["message"]
    assert "附件摘要B" in rendered["message"]


@pytest.mark.asyncio
async def test_render_juhe_deferred_batch_caps_final_text_size():
    runner = _make_runner()
    history = [{"role": "assistant", "content": "earlier turn"}]
    event1 = _make_event(text="first", message_id="juhe-1")
    event2 = _make_event(text="second", message_id="juhe-2", user_id="bob", user_name="Bob")

    async def fake_prepare(*, event, source, history):
        if event is event1:
            return "[Alice] " + ("A" * 1600)
        return "[Bob] " + ("B" * 1600)

    runner._prepare_inbound_message_text = fake_prepare

    rendered = await runner._render_juhe_deferred_batch(
        deferred_events=[event1, event2],
        updated_history=history,
        fallback_source=event1.source,
    )

    assert len(rendered["message"]) <= 2000
    assert rendered["message"].startswith("[Messages received while you were working]")


@pytest.mark.asyncio
async def test_take_juhe_deferred_followup_pops_batch_for_next_turn_even_after_failed_run():
    runner = _make_runner()
    event = _make_event(text="请继续", message_id="juhe-1")
    session_key = "agent:main:juhe:group:R:2001"
    runner._juhe_deferred_batch[session_key] = deque([event])
    runner._render_juhe_deferred_batch = AsyncMock(
        return_value={
            "message": "[Messages received while you were working]\n\n1.\n[Alice] 请继续",
            "source": event.source,
            "message_id": event.message_id,
        }
    )

    followup = await runner._take_juhe_deferred_followup(
        session_key=session_key,
        updated_history=[{"role": "assistant", "content": "The request failed: boom"}],
        fallback_source=event.source,
    )

    assert followup["message_id"] == event.message_id
    assert session_key not in runner._juhe_deferred_batch
