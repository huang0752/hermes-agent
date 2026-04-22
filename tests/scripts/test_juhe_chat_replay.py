"""Tests for the Juhe offline chat replay harness."""

from __future__ import annotations

import importlib
import json
import io
import sys
from pathlib import Path
from contextlib import redirect_stdout

import pytest


def _load_module():
    sys.modules.pop("scripts.juhe_chat_replay", None)
    return importlib.import_module("scripts.juhe_chat_replay")


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_load_scenario_normalizes_minimal_text_steps(tmp_path):
    module = _load_module()
    scenario_path = _write_json(
        tmp_path / "scenario.json",
        {
            "name": "minimal",
            "adapter_extra": {
                "app_key": "app-key",
                "app_secret": "app-secret",
                "guid": "guid-123",
            },
            "steps": [
                {
                    "name": "customer message",
                    "message": {
                        "sender": "1002",
                        "sender_name": "Customer",
                        "roomid": "R:2001",
                        "content": "hello replay",
                    },
                }
            ],
        },
    )

    scenario = module.load_scenario(scenario_path)

    assert scenario["name"] == "minimal"
    assert scenario["adapter_extra"]["guid"] == "guid-123"
    step = scenario["steps"][0]
    assert step["event"]["guid"] == "guid-123"
    assert step["event"]["notify_type"] == 11010
    assert step["event"]["data"]["msg_type"] == 2
    assert step["event"]["data"]["content_type"] == 2
    assert step["event"]["data"]["roomid"] == "2001"
    assert step["event"]["data"]["id"].startswith("replay-step-1")


@pytest.mark.asyncio
async def test_replay_scenario_from_path_preserves_source_path(tmp_path):
    module = _load_module()
    scenario_path = _write_json(
        tmp_path / "scenario.json",
        {
            "name": "dm-from-path",
            "adapter_extra": {
                "app_key": "app-key",
                "app_secret": "app-secret",
                "guid": "guid-123",
                "allow_from": ["1001"],
            },
            "steps": [
                {
                    "name": "dm hello",
                    "message": {
                        "sender": "1001",
                        "id": "dm-msg-1",
                        "content": "hello",
                    },
                }
            ],
        },
    )

    result = await module.replay_scenario(scenario_path, hermes_home=tmp_path / "replay-home")

    assert result["scenario_path"] == str(scenario_path.resolve())


def test_main_writes_output_with_scenario_path(tmp_path):
    module = _load_module()
    scenario_path = _write_json(
        tmp_path / "scenario.json",
        {
            "name": "dm-from-main",
            "adapter_extra": {
                "app_key": "app-key",
                "app_secret": "app-secret",
                "guid": "guid-123",
                "allow_from": ["1001"],
            },
            "steps": [
                {
                    "name": "dm hello",
                    "message": {
                        "sender": "1001",
                        "id": "dm-msg-1",
                        "content": "hello",
                    },
                }
            ],
        },
    )
    output_path = tmp_path / "result.json"

    with redirect_stdout(io.StringIO()):
        exit_code = module.main(
            [
                str(scenario_path),
                "--hermes-home",
                str(tmp_path / "replay-home"),
                "--output",
                str(output_path),
            ]
        )

    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert result["scenario_path"] == str(scenario_path.resolve())


@pytest.mark.asyncio
async def test_replay_scenario_buffers_then_dispatches_group_context(tmp_path):
    module = _load_module()
    scenario = {
        "name": "shared-group-session",
        "env": {
            "JUHE_HOME_CHANNEL": "R:2001",
        },
        "adapter_extra": {
            "app_key": "app-key",
            "app_secret": "app-secret",
            "guid": "guid-123",
            "group_policy": "allowlist",
            "group_allow_from": ["R:2001"],
            "trigger_user_ids": ["1001"],
            "group_sessions_per_user": False,
        },
        "agent": {
            "mode": "mock",
        },
        "steps": [
            {
                "name": "buffered customer message",
                "message": {
                    "sender": "1002",
                    "sender_name": "Customer",
                    "roomid": "2001",
                    "id": "room-msg-1",
                    "content": "Earlier customer context",
                },
                "expect": {
                    "dispatched": False,
                    "room_history_size": 1,
                },
            },
            {
                "name": "authorized trigger user",
                "message": {
                    "sender": "1001",
                    "sender_name": "Employee A",
                    "roomid": "2001",
                    "id": "room-msg-2",
                    "at_list": ["bot"],
                    "content": "请整理刚才需求",
                },
                "agent": {
                    "final_response": "收到，我来整理刚才的需求。",
                },
                "expect": {
                    "dispatched": True,
                    "agent_called": True,
                    "session_key": "agent:main:juhe:group:R:2001",
                    "outbound_count": 1,
                    "pending_context_contains": "Earlier customer context",
                    "agent_message_contains": [
                        "[Recent room context]",
                        "[Employee A] 请整理刚才需求",
                    ],
                    "completed_reply_contains": "我来整理",
                    "final_response_contains": "我来整理",
                    "room_history_size": 2,
                },
            },
        ],
    }

    result = await module.replay_scenario(scenario, hermes_home=tmp_path / "replay-home")

    assert result["all_expectations_passed"] is True
    assert result["steps"][0]["dispatched"] is False
    assert result["steps"][1]["dispatched"] is True
    assert result["steps"][1]["agent_called"] is True
    assert result["steps"][1]["session_key"] == "agent:main:juhe:group:R:2001"
    assert "Earlier customer context" in result["steps"][1]["pending_context"]
    assert result["steps"][1]["completed_reply"] == "收到，我来整理刚才的需求。"
    assert result["steps"][1]["final_response"] == "收到，我来整理刚才的需求。"
    assert result["steps"][1]["transcript_tail"][-1]["content"] == "收到，我来整理刚才的需求。"


@pytest.mark.asyncio
async def test_replay_scenario_surfaces_expectation_failures(tmp_path):
    module = _load_module()
    scenario = {
        "name": "expectation-failure",
        "adapter_extra": {
            "app_key": "app-key",
            "app_secret": "app-secret",
            "guid": "guid-123",
            "trigger_user_ids": ["1001"],
        },
        "steps": [
            {
                "name": "direct message",
                "message": {
                    "sender": "1001",
                    "id": "dm-msg-1",
                    "content": "hello",
                },
                "expect": {
                    "session_key": "agent:main:juhe:dm:wrong",
                },
            }
        ],
    }

    result = await module.replay_scenario(scenario, hermes_home=tmp_path / "replay-home")

    assert result["all_expectations_passed"] is False
    assert result["steps"][0]["passed"] is False
    assert "session_key" in result["steps"][0]["failures"][0]


@pytest.mark.asyncio
async def test_replay_scenario_supports_adapter_only_mode(tmp_path):
    module = _load_module()
    scenario = {
        "name": "adapter-only",
        "mode": "adapter",
        "adapter_extra": {
            "app_key": "app-key",
            "app_secret": "app-secret",
            "guid": "guid-123",
            "group_policy": "allowlist",
            "group_allow_from": ["R:2001"],
            "trigger_user_ids": ["1001"],
            "group_sessions_per_user": False,
        },
        "steps": [
            {
                "name": "customer backlog",
                "message": {
                    "sender": "1002",
                    "sender_name": "Customer",
                    "roomid": "2001",
                    "id": "room-msg-1",
                    "content": "Earlier customer context",
                },
            },
            {
                "name": "authorized trigger user",
                "message": {
                    "sender": "1001",
                    "sender_name": "Employee A",
                    "roomid": "2001",
                    "id": "room-msg-2",
                    "at_list": ["bot"],
                    "content": "请整理刚才需求",
                },
            },
        ],
    }

    result = await module.replay_scenario(scenario, hermes_home=tmp_path / "replay-home")

    assert result["steps"][1]["dispatched"] is True
    assert result["steps"][1]["agent_called"] is False
    assert result["steps"][1]["outbound_count"] == 0
    assert result["steps"][1]["completed_reply"] == ""
