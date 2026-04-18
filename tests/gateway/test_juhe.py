"""Tests for the Juhe platform adapter."""

import inspect
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig, _apply_env_overrides
from gateway.session import SessionSource


def _make_adapter():
    from gateway.platforms.juhe import JuheAdapter

    return JuheAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "app_key": "app-key",
                "app_secret": "app-secret",
                "guid": "guid-123",
                "private_base_url": "https://private.example/upload",
                "allow_from": ["1001", "1002"],
            },
        )
    )


class TestJuheRequirements:
    def test_returns_false_without_aiohttp(self, monkeypatch):
        monkeypatch.setattr("gateway.platforms.juhe.AIOHTTP_AVAILABLE", False)
        monkeypatch.setattr("gateway.platforms.juhe.HTTPX_AVAILABLE", True)
        from gateway.platforms.juhe import check_juhe_requirements

        assert check_juhe_requirements() is False

    def test_returns_false_without_httpx(self, monkeypatch):
        monkeypatch.setattr("gateway.platforms.juhe.AIOHTTP_AVAILABLE", True)
        monkeypatch.setattr("gateway.platforms.juhe.HTTPX_AVAILABLE", False)
        from gateway.platforms.juhe import check_juhe_requirements

        assert check_juhe_requirements() is False

    def test_returns_true_when_available(self, monkeypatch):
        monkeypatch.setattr("gateway.platforms.juhe.AIOHTTP_AVAILABLE", True)
        monkeypatch.setattr("gateway.platforms.juhe.HTTPX_AVAILABLE", True)
        from gateway.platforms.juhe import check_juhe_requirements

        assert check_juhe_requirements() is True


class TestJuheConfig:
    def test_apply_env_overrides_configures_juhe(self):
        config = GatewayConfig()

        with patch.dict(
            os.environ,
            {
                "JUHE_APP_KEY": "env-app-key",
                "JUHE_APP_SECRET": "env-app-secret",
                "JUHE_GUID": "env-guid",
                "JUHE_BASE_URL": "https://juhe.example.com/",
                "JUHE_PRIVATE_BASE_URL": "https://juhe-private.example.com/",
                "JUHE_S3_ENDPOINT_URL": "https://s3.cn-north-1.qiniucs.com",
                "JUHE_S3_REGION": "cn-north-1",
                "JUHE_S3_BUCKET": "temp-filechuan",
                "JUHE_S3_ACCESS_KEY": "s3-ak",
                "JUHE_S3_SECRET_KEY": "s3-sk",
                "JUHE_S3_PREFIX": "juhe-temp",
                "JUHE_S3_URL_EXPIRES_SECONDS": "1800",
                "JUHE_WEBSOCKET_URL": "wss://juhe.example.com/ws/juhe",
                "JUHE_ALLOWED_USERS": "S:1001,S:1002",
                "JUHE_GROUP_ALLOWED_CHATS": "R:2001,R:2002",
                "JUHE_HOME_CHANNEL": "S:1001",
                "JUHE_HOME_CHANNEL_NAME": "Primary DM",
            },
            clear=True,
        ):
            _apply_env_overrides(config)

        platform_config = config.platforms[Platform.JUHE]
        assert platform_config.enabled is True
        assert platform_config.extra["app_key"] == "env-app-key"
        assert platform_config.extra["app_secret"] == "env-app-secret"
        assert platform_config.extra["guid"] == "env-guid"
        assert platform_config.extra["base_url"] == "https://juhe.example.com"
        assert platform_config.extra["private_base_url"] == "https://juhe-private.example.com"
        assert platform_config.extra["temp_s3_endpoint_url"] == "https://s3.cn-north-1.qiniucs.com"
        assert platform_config.extra["temp_s3_region"] == "cn-north-1"
        assert platform_config.extra["temp_s3_bucket"] == "temp-filechuan"
        assert platform_config.extra["temp_s3_access_key"] == "s3-ak"
        assert platform_config.extra["temp_s3_secret_key"] == "s3-sk"
        assert platform_config.extra["temp_s3_prefix"] == "juhe-temp"
        assert platform_config.extra["temp_s3_url_expires_seconds"] == 1800
        assert platform_config.extra["websocket_url"] == "wss://juhe.example.com/ws/juhe"
        assert platform_config.extra["dm_policy"] == "allowlist"
        assert platform_config.extra["allow_from"] == "S:1001,S:1002"
        assert platform_config.extra["group_policy"] == "allowlist"
        assert platform_config.extra["group_allow_from"] == "R:2001,R:2002"
        assert platform_config.home_channel == HomeChannel(Platform.JUHE, "S:1001", "Primary DM")

    def test_get_connected_platforms_includes_juhe_with_credentials(self):
        config = GatewayConfig(
            platforms={
                Platform.JUHE: PlatformConfig(
                    enabled=True,
                    extra={
                        "app_key": "app-key",
                        "app_secret": "app-secret",
                        "guid": "guid-123",
                    },
                )
            }
        )

        assert config.get_connected_platforms() == [Platform.JUHE]


class TestJuheAdapterInit:
    def test_reads_config_from_extra(self):
        from gateway.platforms.juhe import JuheAdapter

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "cfg-app",
                    "app_secret": "cfg-secret",
                    "guid": "cfg-guid",
                    "private_base_url": "https://private.example/api",
                    "temp_s3_endpoint_url": "https://s3.example.com",
                    "temp_s3_region": "cn-north-1",
                    "temp_s3_bucket": "temp-filechuan",
                    "temp_s3_access_key": "s3-ak",
                    "temp_s3_secret_key": "s3-sk",
                    "temp_s3_prefix": "juhe-temp",
                    "temp_s3_url_expires_seconds": 1200,
                    "websocket_url": "wss://custom.example/ws",
                    "group_policy": "allowlist",
                    "group_allow_from": ["R:2001"],
                },
            )
        )

        assert adapter._app_key == "cfg-app"
        assert adapter._app_secret == "cfg-secret"
        assert adapter._guid == "cfg-guid"
        assert adapter._private_base_url == "https://private.example/api"
        assert adapter._temp_s3_endpoint_url == "https://s3.example.com"
        assert adapter._temp_s3_region == "cn-north-1"
        assert adapter._temp_s3_bucket == "temp-filechuan"
        assert adapter._temp_s3_access_key == "s3-ak"
        assert adapter._temp_s3_secret_key == "s3-sk"
        assert adapter._temp_s3_prefix == "juhe-temp"
        assert adapter._temp_s3_url_expires_seconds == 1200
        assert adapter._ws_url == "wss://custom.example/ws"
        assert adapter._group_policy == "allowlist"
        assert adapter._group_allow_from == ["R:2001"]

    def test_falls_back_to_env_vars(self, monkeypatch):
        monkeypatch.setenv("JUHE_APP_KEY", "env-app")
        monkeypatch.setenv("JUHE_APP_SECRET", "env-secret")
        monkeypatch.setenv("JUHE_GUID", "env-guid")
        monkeypatch.setenv("JUHE_PRIVATE_BASE_URL", "https://env.example/private")
        monkeypatch.setenv("JUHE_S3_ENDPOINT_URL", "https://s3.env.example.com")
        monkeypatch.setenv("JUHE_S3_REGION", "cn-north-1")
        monkeypatch.setenv("JUHE_S3_BUCKET", "temp-filechuan")
        monkeypatch.setenv("QINIU_ACCESS_KEY", "fallback-ak")
        monkeypatch.setenv("QINIU_SECRET_KEY", "fallback-sk")
        monkeypatch.setenv("JUHE_WEBSOCKET_URL", "wss://env.example/ws")
        from gateway.platforms.juhe import JuheAdapter

        adapter = JuheAdapter(PlatformConfig(enabled=True))
        assert adapter._app_key == "env-app"
        assert adapter._app_secret == "env-secret"
        assert adapter._guid == "env-guid"
        assert adapter._private_base_url == "https://env.example/private"
        assert adapter._temp_s3_endpoint_url == "https://s3.env.example.com"
        assert adapter._temp_s3_region == "cn-north-1"
        assert adapter._temp_s3_bucket == "temp-filechuan"
        assert adapter._temp_s3_access_key == "fallback-ak"
        assert adapter._temp_s3_secret_key == "fallback-sk"
        assert adapter._ws_url == "wss://env.example/ws"


class TestJuheConnect:
    @pytest.mark.asyncio
    async def test_connect_records_missing_credentials(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module
        from gateway.platforms.juhe import JuheAdapter

        monkeypatch.delenv("JUHE_APP_KEY", raising=False)
        monkeypatch.delenv("JUHE_APP_SECRET", raising=False)
        monkeypatch.delenv("JUHE_GUID", raising=False)
        monkeypatch.setattr(juhe_module, "AIOHTTP_AVAILABLE", True)
        monkeypatch.setattr(juhe_module, "HTTPX_AVAILABLE", True)

        adapter = JuheAdapter(PlatformConfig(enabled=True))

        success = await adapter.connect()

        assert success is False
        assert adapter.has_fatal_error is True
        assert adapter.fatal_error_code == "juhe_missing_credentials"
        assert "JUHE_APP_KEY" in (adapter.fatal_error_message or "")


class TestJuhePolicyHelpers:
    def test_dm_allowlist_accepts_bare_uin_and_prefixed_entry(self):
        from gateway.platforms.juhe import JuheAdapter

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "app",
                    "app_secret": "secret",
                    "guid": "guid",
                    "dm_policy": "allowlist",
                    "allow_from": ["1001", "S:1002"],
                },
            )
        )

        assert adapter._is_dm_allowed("1001") is True
        assert adapter._is_dm_allowed("S:1002") is True
        assert adapter._is_dm_allowed("1003") is False

    def test_group_allowlist_and_per_group_sender_allowlist(self):
        from gateway.platforms.juhe import JuheAdapter

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "app",
                    "app_secret": "secret",
                    "guid": "guid",
                    "group_policy": "allowlist",
                    "group_allow_from": ["R:2001"],
                    "groups": {"R:2001": {"allow_from": ["1001"]}},
                },
            )
        )

        assert adapter._is_group_allowed("R:2001", "1001") is True
        assert adapter._is_group_allowed("R:2001", "1002") is False
        assert adapter._is_group_allowed("R:2999", "1001") is False

    def test_require_mention_accepts_any_at_list_entry(self):
        from gateway.platforms.juhe import JuheAdapter

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "app",
                    "app_secret": "secret",
                    "guid": "guid",
                    "groups": {"R:2001": {"require_mention": True}},
                },
            )
        )

        assert adapter._passes_mention_gate("R:2001", []) is False
        assert adapter._passes_mention_gate("R:2001", ["someone"]) is True
        assert adapter._passes_mention_gate("R:2002", []) is True


class TestJuheInbound:
    @pytest.mark.asyncio
    async def test_ws_callback_parses_stringified_event(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        adapter._ws = SimpleNamespace(closed=False, send_json=AsyncMock())

        success = await adapter._dispatch_ws_payload(
            {
                "type": "callback",
                "event_id": "evt-1",
                "event": json.dumps(
                    {
                        "guid": "guid-123",
                        "notify_type": 11010,
                        "data": {
                            "msg_type": 2,
                            "content": '{"msg":"hello string event"}',
                            "sender": "1001",
                            "msg_id": "msg-string-1",
                        },
                    }
                ),
            }
        )

        assert success is True
        adapter.handle_message.assert_awaited_once()
        adapter._ws.send_json.assert_awaited_once_with(
            {"type": "ack", "event_id": "evt-1", "success": True}
        )

    @pytest.mark.asyncio
    async def test_ws_callback_falls_back_to_data_when_event_missing(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        adapter._ws = SimpleNamespace(closed=False, send_json=AsyncMock())

        success = await adapter._dispatch_ws_payload(
            {
                "type": "callback",
                "event_id": "evt-2",
                "data": {
                    "guid": "guid-123",
                    "notify_type": 11010,
                    "data": {
                        "msg_type": 2,
                        "content": '{"msg":"hello payload data"}',
                        "sender": "1001",
                        "msg_id": "msg-data-1",
                    },
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_awaited_once()
        adapter._ws.send_json.assert_awaited_once_with(
            {"type": "ack", "event_id": "evt-2", "success": True}
        )

    @pytest.mark.asyncio
    async def test_single_text_callback_dispatches_dm_message(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 2,
                    "content": '{"msg":"hello juhe"}',
                    "sender": "1001",
                    "sender_name": "Alice",
                    "msg_id": "msg-1",
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "hello juhe"
        assert event.source.chat_id == "S:1001"
        assert event.source.user_id == "1001"
        assert event.source.chat_type == "dm"

    @pytest.mark.asyncio
    async def test_batch_callback_dispatches_each_text_message(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11013,
                "data": [
                    {
                        "msg_type": 2,
                        "content": '{"msg":"hello"}',
                        "sender": "1001",
                        "msg_id": "msg-1",
                    },
                    {
                        "msg_type": 2,
                        "content": '{"msg":"world"}',
                        "sender": "1002",
                        "msg_id": "msg-2",
                    },
                ],
            }
        )

        assert success is True
        assert adapter.handle_message.await_count == 2

    @pytest.mark.asyncio
    async def test_unsupported_message_is_acknowledged_and_replied_to(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, error=None))

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 5,
                    "content": '{"data":{"image":{"url":"https://example.com/a.jpg"}}}',
                    "sender": "1001",
                    "msg_id": "msg-1",
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_not_awaited()
        adapter.send.assert_awaited_once_with("S:1001", "暂不支持图片消息，当前仅支持文本消息。")

    @pytest.mark.asyncio
    async def test_msg_type_one_is_not_treated_as_text(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, error=None))

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 1,
                    "content": '{"msg":"revoke-like payload"}',
                    "sender": "1001",
                    "msg_id": "msg-revoke-1",
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_not_awaited()
        adapter.send.assert_awaited_once_with("S:1001", "暂不支持撤回消息，当前仅支持文本消息。")

    @pytest.mark.asyncio
    async def test_duplicate_message_is_ignored(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()

        payload = {
            "guid": "guid-123",
            "notify_type": 11010,
            "data": {
                "msg_type": 2,
                "content": '{"msg":"hello"}',
                "sender": "1001",
                "msg_id": "msg-1",
            },
        }

        assert await adapter._handle_callback_event(payload) is True
        assert await adapter._handle_callback_event(payload) is True
        assert adapter.handle_message.await_count == 1

    @pytest.mark.asyncio
    async def test_group_require_mention_blocks_when_no_at_list(self):
        from gateway.platforms.juhe import JuheAdapter

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "app",
                    "app_secret": "secret",
                    "guid": "guid-123",
                    "group_policy": "allowlist",
                    "group_allow_from": ["R:2001"],
                    "groups": {"R:2001": {"require_mention": True}},
                },
            )
        )
        adapter.handle_message = AsyncMock()

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 2,
                    "content": '{"msg":"hello group"}',
                    "sender": "1001",
                    "chat_id": "2001",
                    "is_group": True,
                    "msg_id": "msg-1",
                    "at_list": [],
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_msg_type_two_plain_text_group_message_dispatches(self):
        from gateway.platforms.juhe import JuheAdapter

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "app",
                    "app_secret": "secret",
                    "guid": "guid-123",
                    "group_policy": "allowlist",
                    "group_allow_from": ["R:2001"],
                    "groups": {"R:2001": {"require_mention": True}},
                },
            )
        )
        adapter.handle_message = AsyncMock()

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 2,
                    "content_type": 2,
                    "content": "@bot ping juhe",
                    "sender": "1001",
                    "roomid": "2001",
                    "id": "juhe-id-1",
                    "at_list": ["bot"],
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "@bot ping juhe"
        assert event.source.chat_id == "R:2001"
        assert event.source.chat_type == "group"

    @pytest.mark.asyncio
    async def test_roomid_zero_is_treated_as_dm(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 2,
                    "content_type": 2,
                    "content": "ping juhe",
                    "sender": "1001",
                    "roomid": "0",
                    "id": "juhe-id-2",
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "ping juhe"
        assert event.source.chat_id == "S:1001"
        assert event.source.chat_type == "dm"

    @pytest.mark.asyncio
    async def test_different_fallback_ids_do_not_dedup_into_one_message(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()

        first = {
            "guid": "guid-123",
            "notify_type": 11010,
            "data": {
                "msg_type": 2,
                "content_type": 2,
                "content": "hello first",
                "sender": "1001",
                "roomid": "0",
                "id": "juhe-id-3",
                "sendtime": 1776247621,
            },
        }
        second = {
            "guid": "guid-123",
            "notify_type": 11010,
            "data": {
                "msg_type": 2,
                "content_type": 2,
                "content": "hello second",
                "sender": "1001",
                "roomid": "0",
                "id": "juhe-id-4",
                "sendtime": 1776247629,
            },
        }

        assert await adapter._handle_callback_event(first) is True
        assert await adapter._handle_callback_event(second) is True
        assert adapter.handle_message.await_count == 2

    @pytest.mark.asyncio
    async def test_send_flag_one_self_echo_is_ignored(self):
        from gateway.platforms.juhe import JuheAdapter

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "app",
                    "app_secret": "secret",
                    "guid": "guid-123",
                    "group_policy": "allowlist",
                    "group_allow_from": ["R:2001"],
                },
            )
        )
        adapter.handle_message = AsyncMock()

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 2,
                    "content_type": 2,
                    "content": "bot echo",
                    "sender": "1688858038755018",
                    "receiver": "0",
                    "roomid": "2001",
                    "send_flag": 1,
                    "id": "juhe-id-5",
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_not_awaited()


class TestJuheSend:
    @pytest.mark.asyncio
    async def test_send_text_uses_guid_request_and_requires_prefixed_target(self):
        adapter = _make_adapter()
        adapter._guid_request = AsyncMock(return_value={"err_code": 0, "data": {"ret": 0}})

        result = await adapter.send("S:1001", "hello juhe")

        assert result.success is True
        adapter._guid_request.assert_awaited_once()
        payload = adapter._guid_request.await_args.kwargs
        assert payload["path"] == "/msg/send_text"
        assert payload["data"]["guid"] == "guid-123"
        assert payload["data"]["conversation_id"] == "S:1001"
        assert payload["data"]["content"] == "hello juhe"

    @pytest.mark.asyncio
    async def test_send_rejects_unprefixed_target(self):
        adapter = _make_adapter()

        result = await adapter.send("1001", "hello juhe")

        assert result.success is False
        assert "S:" in (result.error or "")

    def test_build_sdk_client_passes_private_base_url(self):
        adapter = _make_adapter()
        adapter._private_base_url = "https://private.example/base"

        client = adapter._build_sdk_client()

        assert client.public_base_url == adapter._base_url
        assert client.private_base_url == "https://private.example/base"

    @pytest.mark.asyncio
    async def test_send_document_uses_small_file_flow_for_http_url(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()
        small_upload = AsyncMock(return_value={"error_code": 0, "data": {"message_id": "msg-file-1"}})
        big_upload = AsyncMock(return_value={"error_code": 0})
        monkeypatch.setattr(juhe_module, "send_small_file_from_url", small_upload)
        monkeypatch.setattr(juhe_module, "send_big_file_from_url", big_upload)
        monkeypatch.setattr(adapter, "_get_remote_file_size_hint", AsyncMock(return_value=1024))

        result = await adapter.send_document("S:1001", "https://files.example.com/report.pdf")

        assert result.success is True
        assert result.message_id == "msg-file-1"
        small_upload.assert_awaited_once_with(
            client=adapter._get_sdk_client(),
            conversation_id="S:1001",
            file_url="https://files.example.com/report.pdf",
            file_name="report.pdf",
            file_type=5,
        )
        big_upload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_image_uses_big_file_flow_when_remote_size_is_large(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()
        small_upload = AsyncMock(return_value={"error_code": 0})
        big_upload = AsyncMock(return_value={"error_code": 0, "data": {"message_id": "msg-file-2"}})
        monkeypatch.setattr(juhe_module, "send_small_file_from_url", small_upload)
        monkeypatch.setattr(juhe_module, "send_big_file_from_url", big_upload)
        monkeypatch.setattr(adapter, "_get_remote_file_size_hint", AsyncMock(return_value=25 * 1024 * 1024))

        result = await adapter.send_image("S:1001", "https://files.example.com/photo.png")

        assert result.success is True
        assert result.message_id == "msg-file-2"
        big_upload.assert_awaited_once_with(
            client=adapter._get_sdk_client(),
            conversation_id="S:1001",
            file_url="https://files.example.com/photo.png",
            file_name="photo.png",
            file_type=2,
        )
        small_upload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_document_stages_local_path_via_temp_s3(self, monkeypatch, tmp_path):
        adapter = _make_adapter()
        file_path = tmp_path / "report.txt"
        file_path.write_text("hello", encoding="utf-8")
        cleanup = AsyncMock()
        monkeypatch.setattr(
            adapter,
            "_stage_local_file_for_upload",
            AsyncMock(return_value=("https://temp.example.com/report.txt?sig=1", cleanup)),
        )
        monkeypatch.setattr(
            adapter,
            "_upload_file_from_url",
            AsyncMock(return_value={"error_code": 0, "data": {"message_id": "msg-local-1"}}),
        )

        result = await adapter.send_document("S:1001", str(file_path))

        assert result.success is True
        assert result.message_id == "msg-local-1"
        adapter._stage_local_file_for_upload.assert_awaited_once_with(str(file_path))
        adapter._upload_file_from_url.assert_awaited_once_with(
            client=adapter._get_sdk_client(),
            conversation_id="S:1001",
            file_url="https://temp.example.com/report.txt?sig=1",
            file_name="report.txt",
            file_type=5,
        )
        cleanup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_document_rejects_local_path_without_temp_s3(self, tmp_path):
        adapter = _make_adapter()
        adapter._temp_s3_bucket = None
        file_path = tmp_path / "report.txt"
        file_path.write_text("hello", encoding="utf-8")

        result = await adapter.send_document("S:1001", str(file_path))

        assert result.success is False
        assert "JUHE_S3_BUCKET" in (result.error or "")


class TestGatewayIntegration:
    def test_platform_enum_value(self):
        assert Platform.JUHE.value == "juhe"

    def test_gateway_authorization_trusts_juhe_adapter_filtering(self):
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig()
        runner.pairing_store = MagicMock()
        runner.pairing_store.is_approved.return_value = False

        source = SessionSource(
            platform=Platform.JUHE,
            user_id="1001",
            chat_id="S:1001",
            user_name="tester",
            chat_type="dm",
        )

        with patch.dict(os.environ, {}, clear=True):
            assert runner._is_user_authorized(source) is True

    def test_juhe_in_send_message_platform_map(self):
        import tools.send_message_tool as smt

        source = inspect.getsource(smt._handle_send)
        assert '"juhe"' in source

    def test_send_to_platform_has_juhe_branch(self):
        import tools.send_message_tool as smt

        source = inspect.getsource(smt._send_to_platform)
        assert "Platform.JUHE" in source

    def test_juhe_in_cron_platform_map(self):
        import cron.scheduler

        source = inspect.getsource(cron.scheduler)
        assert '"juhe"' in source

    def test_juhe_toolset_exists(self):
        from toolsets import TOOLSETS

        assert "hermes-juhe" in TOOLSETS
        assert "hermes-juhe" in TOOLSETS["hermes-gateway"]["includes"]

    def test_juhe_in_cli_platform_registry(self):
        from hermes_cli.platforms import PLATFORMS

        assert "juhe" in PLATFORMS
        assert PLATFORMS["juhe"].default_toolset == "hermes-juhe"

    def test_juhe_in_platform_hints(self):
        from agent.prompt_builder import PLATFORM_HINTS

        assert "juhe" in PLATFORM_HINTS
        assert "juhe" in PLATFORM_HINTS["juhe"].lower()


class TestSendJuheStandalone:
    @pytest.mark.asyncio
    async def test_send_juhe_uses_adapter_send(self):
        from tools.send_message_tool import _send_juhe

        adapter = MagicMock()
        adapter.connect = AsyncMock(return_value=True)
        adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="msg-123", error=None))
        adapter.disconnect = AsyncMock()

        with patch("gateway.platforms.juhe.check_juhe_requirements", return_value=True), \
             patch("tools.send_message_tool.JuheAdapter", return_value=adapter):
            result = await _send_juhe(
                {"app_key": "app", "app_secret": "secret", "guid": "guid"},
                "S:1001",
                "hello juhe",
            )

        assert result == {
            "success": True,
            "platform": "juhe",
            "chat_id": "S:1001",
            "message_id": "msg-123",
        }

    @pytest.mark.asyncio
    async def test_send_juhe_routes_media_via_adapter_methods(self):
        from tools.send_message_tool import _send_juhe

        adapter = MagicMock()
        adapter.connect = AsyncMock(return_value=True)
        adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="text-123", error=None))
        adapter.send_document = AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="file-456", error=None)
        )
        adapter.disconnect = AsyncMock()

        with patch("gateway.platforms.juhe.check_juhe_requirements", return_value=True), \
             patch("tools.send_message_tool.JuheAdapter", return_value=adapter):
            result = await _send_juhe(
                {"app_key": "app", "app_secret": "secret", "guid": "guid"},
                "S:1001",
                "hello juhe",
                media_files=[("https://files.example.com/report.pdf", False)],
            )

        assert result == {
            "success": True,
            "platform": "juhe",
            "chat_id": "S:1001",
            "message_id": "file-456",
        }
        adapter.send.assert_awaited_once_with("S:1001", "hello juhe")
        adapter.send_document.assert_awaited_once_with("S:1001", "https://files.example.com/report.pdf")

    @pytest.mark.asyncio
    async def test_send_juhe_expands_local_media_home_path(self, monkeypatch, tmp_path):
        from tools.send_message_tool import _send_juhe

        home_dir = tmp_path / "home"
        home_dir.mkdir()
        file_path = home_dir / "report.pdf"
        file_path.write_bytes(b"%PDF-1.4\n")
        monkeypatch.setenv("HOME", str(home_dir))

        adapter = MagicMock()
        adapter.connect = AsyncMock(return_value=True)
        adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="text-123", error=None))
        adapter.send_document = AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="file-789", error=None)
        )
        adapter.disconnect = AsyncMock()

        with patch("gateway.platforms.juhe.check_juhe_requirements", return_value=True), \
             patch("tools.send_message_tool.JuheAdapter", return_value=adapter):
            result = await _send_juhe(
                {"app_key": "app", "app_secret": "secret", "guid": "guid"},
                "S:1001",
                "",
                media_files=[("~/report.pdf", False)],
            )

        assert result == {
            "success": True,
            "platform": "juhe",
            "chat_id": "S:1001",
            "message_id": "file-789",
        }
        adapter.send.assert_not_awaited()
        adapter.send_document.assert_awaited_once_with("S:1001", str(file_path))
