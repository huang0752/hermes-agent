"""Tests for the Juhe-specific action tool."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.config import Platform, PlatformConfig
from gateway.juhe_cache import JuheCacheStore
from tools.juhe_tool import juhe_tool


def _run_async_immediately(coro):
    return asyncio.run(coro)


def _make_config():
    return SimpleNamespace(
        platforms={
            Platform.JUHE: PlatformConfig(
                enabled=True,
                extra={"app_key": "app", "app_secret": "secret", "guid": "guid"},
            )
        }
    )


class TestJuheTool:
    def test_list_contacts_reads_from_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        store = JuheCacheStore()
        store.upsert_contact({"user_id": "1001", "name": "Alice"})

        with patch("gateway.config.load_gateway_config", return_value=_make_config()):
            result = json.loads(juhe_tool({"action": "list_contacts"}))

        assert result["contacts"][0]["user_id"] == "1001"

    def test_send_room_at_action_calls_adapter(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = SimpleNamespace(
            send_room_at=AsyncMock(return_value={"success": True, "message_id": "msg-1"})
        )

        with patch("gateway.config.load_gateway_config", return_value=_make_config()), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.juhe_tool.JuheAdapter", return_value=adapter):
            result = json.loads(
                juhe_tool(
                    {
                        "action": "send_room_at",
                        "conversation_id": "R:2001",
                        "content": "hello {$@}",
                        "at_list": ["1001"],
                    }
                )
            )

        assert result["success"] is True
        adapter.send_room_at.assert_awaited_once_with("R:2001", "hello {$@}", ["1001"])
