import asyncio
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, merge_pending_message_event
from gateway.session import SessionSource, build_session_key
from gateway.run import GatewayRunner


class _PendingAdapter:
    def __init__(self):
        self._pending_messages = {}


def _make_runner():
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
    runner.adapters = {Platform.TELEGRAM: _PendingAdapter()}
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._voice_mode = {}
    runner._is_user_authorized = lambda _source: True
    return runner


@pytest.mark.asyncio
async def test_handle_message_does_not_priority_interrupt_photo_followup():
    runner = _make_runner()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm", user_id="u1")
    session_key = build_session_key(source)
    running_agent = MagicMock()
    runner._running_agents[session_key] = running_agent

    event = MessageEvent(
        text="caption",
        message_type=MessageType.PHOTO,
        source=source,
        media_urls=["/tmp/photo-a.jpg"],
        media_types=["image/jpeg"],
    )

    result = await runner._handle_message(event)

    assert result is None
    running_agent.interrupt.assert_not_called()
    assert runner.adapters[Platform.TELEGRAM]._pending_messages[session_key] is event


def test_merge_pending_photo_burst_keeps_juhe_parallel_attachment_fields_aligned():
    source = SessionSource(platform=Platform.JUHE, chat_id="R:2001", chat_type="group", user_id="u1")
    session_key = build_session_key(source)
    pending_messages = {}

    first = MessageEvent(
        text="第一张",
        message_type=MessageType.PHOTO,
        source=source,
        media_urls=["juhe://attachment/1/photo-a.jpg"],
        media_types=["image/jpeg"],
    )
    setattr(first, "_juhe_media_access_urls", ["https://minio.example/photo-a.jpg?X-Amz-Signature=1"])
    setattr(first, "_juhe_attachment_identities", [{"object_key": "photo-a.jpg"}])
    setattr(first, "_juhe_media_descriptions", ["图片 photo-a.jpg：营业执照第一页"])

    second = MessageEvent(
        text="第二张",
        message_type=MessageType.PHOTO,
        source=source,
        media_urls=["juhe://attachment/2/photo-b.jpg"],
        media_types=["image/jpeg"],
    )
    setattr(second, "_juhe_media_access_urls", ["https://minio.example/photo-b.jpg?X-Amz-Signature=2"])
    setattr(second, "_juhe_attachment_identities", [{"object_key": "photo-b.jpg"}])
    setattr(second, "_juhe_media_descriptions", ["图片 photo-b.jpg：营业执照第二页"])

    pending_messages[session_key] = first
    merge_pending_message_event(pending_messages, session_key, second)

    merged = pending_messages[session_key]
    assert merged.media_urls == [
        "juhe://attachment/1/photo-a.jpg",
        "juhe://attachment/2/photo-b.jpg",
    ]
    assert getattr(merged, "_juhe_media_access_urls") == [
        "https://minio.example/photo-a.jpg?X-Amz-Signature=1",
        "https://minio.example/photo-b.jpg?X-Amz-Signature=2",
    ]
    assert getattr(merged, "_juhe_attachment_identities") == [
        {"object_key": "photo-a.jpg"},
        {"object_key": "photo-b.jpg"},
    ]
    assert getattr(merged, "_juhe_media_descriptions") == [
        "图片 photo-a.jpg：营业执照第一页",
        "图片 photo-b.jpg：营业执照第二页",
    ]
