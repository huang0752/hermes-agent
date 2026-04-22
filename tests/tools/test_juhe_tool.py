"""Tests for the Juhe-specific action tool."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.session_context import clear_session_vars, set_session_vars
from hermes_state import SessionDB
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

    def test_get_room_history_reads_from_room_log(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            db.append_room_message(
                platform="juhe",
                conversation_id="R:2001",
                message_id="msg-1",
                sender_id="1002",
                sender_name="Bob",
                direction="inbound",
                message_type=2,
                text_preview="Earlier context",
                raw_payload={"content": "Earlier context"},
                triggered=False,
                consumed_for_context=False,
                room_log_limit=500,
            )
            db.append_room_message(
                platform="juhe",
                conversation_id="R:2001",
                message_id="msg-2",
                sender_id="1001",
                sender_name="Alice",
                direction="inbound",
                message_type=2,
                text_preview="Latest context",
                raw_payload={"content": "Latest context"},
                triggered=True,
                consumed_for_context=True,
                room_log_limit=500,
            )
        finally:
            db.close()

        with patch("gateway.config.load_gateway_config", return_value=_make_config()), \
             patch("tools.juhe_tool.JuheAdapter", return_value=SimpleNamespace()):
            result = json.loads(
                juhe_tool(
                    {
                        "action": "get_room_history",
                        "conversation_id": "R:2001",
                        "limit": 1,
                        "before_message_id": "msg-2",
                    }
                )
            )

        assert [item["message_id"] for item in result["messages"]] == ["msg-1"]

    def test_search_room_history_reads_from_room_log_search(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            db.append_room_message(
                platform="juhe",
                conversation_id="R:2001",
                message_id="msg-1",
                sender_id="1002",
                sender_name="Bob",
                direction="inbound",
                message_type=2,
                text_preview="品牌 logo 需要替换成客户确认版本",
                raw_payload={"content": "品牌 logo 需要替换成客户确认版本"},
                room_log_limit=500,
            )
        finally:
            db.close()

        with patch("gateway.config.load_gateway_config", return_value=_make_config()), \
             patch("tools.juhe_tool.JuheAdapter", return_value=SimpleNamespace()):
            result = json.loads(
                juhe_tool(
                    {
                        "action": "search_room_history",
                        "conversation_id": "R:2001",
                        "query": "品牌 logo",
                        "limit": 5,
                    }
                )
            )

        assert [item["message_id"] for item in result["messages"]] == ["msg-1"]

    def test_list_current_attachments_reads_session_media_manifest_without_leaking_access_urls(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        tokens = set_session_vars(
            platform="juhe",
            chat_id="R:2001",
            session_key="session-1",
            media_manifest=json.dumps(
                [
                    {
                        "index": 0,
                        "display_name": "brand-logo.jpg",
                        "media_type": "image/jpeg",
                        "description": "图片 brand-logo.jpg：蓝底品牌 logo",
                        "media_ref": "juhe://attachment/a/brand-logo.jpg",
                        "access_url": "https://minio.example.com/brand-logo.jpg?X-Amz-Signature=secret",
                    },
                    {
                        "index": 1,
                        "display_name": "certificate-logo.jpg",
                        "media_type": "image/jpeg",
                        "description": "图片 certificate-logo.jpg：白底证书 logo",
                        "media_ref": "juhe://attachment/b/certificate-logo.jpg",
                        "access_url": "https://minio.example.com/certificate-logo.jpg?X-Amz-Signature=secret2",
                    },
                ],
                ensure_ascii=False,
            ),
        )
        try:
            with patch("gateway.config.load_gateway_config", return_value=_make_config()), \
                 patch("tools.juhe_tool.JuheAdapter", return_value=SimpleNamespace()):
                result = json.loads(juhe_tool({"action": "list_current_attachments"}))
        finally:
            clear_session_vars(tokens)

        assert [item["position"] for item in result["attachments"]] == [1, 2]
        assert result["attachments"][0]["description"] == "图片 brand-logo.jpg：蓝底品牌 logo"
        assert "access_url" not in result["attachments"][0]
        assert "X-Amz-Signature" not in json.dumps(result, ensure_ascii=False)

    def test_materialize_current_attachment_downloads_remote_attachment_to_temp_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        tokens = set_session_vars(
            platform="juhe",
            chat_id="R:2001",
            session_key="session-1",
            media_manifest=json.dumps(
                [
                    {
                        "index": 0,
                        "display_name": "brand-logo.jpg",
                        "media_type": "image/jpeg",
                        "description": "图片 brand-logo.jpg：蓝底品牌 logo",
                        "media_ref": "juhe://attachment/a/brand-logo.jpg",
                        "access_url": "https://minio.example.com/brand-logo.jpg?X-Amz-Signature=secret",
                    }
                ],
                ensure_ascii=False,
            ),
        )
        try:
            with patch("gateway.config.load_gateway_config", return_value=_make_config()), \
                 patch("model_tools._run_async", side_effect=_run_async_immediately), \
                 patch("tools.juhe_tool.JuheAdapter", return_value=SimpleNamespace()), \
                 patch("tools.juhe_tool._download_attachment_bytes", new=AsyncMock(return_value=b"fake-image-bytes")):
                result = json.loads(juhe_tool({"action": "materialize_current_attachment", "position": 1}))
        finally:
            clear_session_vars(tokens)

        assert result["success"] is True
        materialized_path = Path(result["path"])
        assert materialized_path.exists()
        assert materialized_path.read_bytes() == b"fake-image-bytes"
        assert materialized_path.name.endswith("brand-logo.jpg")

    def test_upload_current_attachment_to_certificate_uses_hidden_access_url(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        tokens = set_session_vars(
            platform="juhe",
            chat_id="R:2001",
            session_key="session-1",
            media_manifest=json.dumps(
                [
                    {
                        "index": 0,
                        "position": 1,
                        "display_name": "brand-logo.jpg",
                        "media_type": "image/jpeg",
                        "description": "图片 brand-logo.jpg：蓝底品牌 logo",
                        "media_ref": "juhe://attachment/a/brand-logo.jpg",
                        "access_url": "https://minio.example.com/brand-logo.jpg?X-Amz-Signature=secret",
                    }
                ],
                ensure_ascii=False,
            ),
        )
        try:
            with patch("gateway.config.load_gateway_config", return_value=_make_config()), \
                 patch("tools.juhe_tool.JuheAdapter", return_value=SimpleNamespace()), \
                 patch(
                     "tools.juhe_tool._run_certificate_workflow_cli",
                     return_value={"id": 321, "title": "brand-logo.jpg"},
                 ) as run_cli:
                result = json.loads(
                    juhe_tool(
                        {
                            "action": "upload_current_attachment_to_certificate",
                            "position": 1,
                            "title": "中合信品牌logo",
                        }
                    )
                )
        finally:
            clear_session_vars(tokens)

        assert result["success"] is True
        assert result["asset_id"] == 321
        assert result["display_name"] == "brand-logo.jpg"
        assert "X-Amz-Signature" not in json.dumps(result, ensure_ascii=False)
        run_cli.assert_called_once_with(
            "upload-file-from-url",
            [
                "--url",
                "https://minio.example.com/brand-logo.jpg?X-Amz-Signature=secret",
                "--filename",
                "brand-logo.jpg",
                "--title",
                "中合信品牌logo",
            ],
        )
