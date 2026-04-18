"""Tests for the Juhe platform adapter."""

import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig, _apply_env_overrides
from gateway.juhe_cache import JuheCacheStore
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

    def test_apply_env_overrides_configures_juhe_inbound_s3_from_minio_aliases(self):
        config = GatewayConfig()

        with patch.dict(
            os.environ,
            {
                "JUHE_APP_KEY": "env-app-key",
                "JUHE_APP_SECRET": "env-app-secret",
                "JUHE_GUID": "env-guid",
                "MINIO_ENDPOINT": "124.220.81.138:9000",
                "MINIO_ACCESS_KEY": "minio-ak",
                "MINIO_SECRET_KEY": "minio-sk",
                "MINIO_USE_HTTPS": "false",
                "MINIO_REGION": "us-east-1",
                "MINIO_DEFAULT_BUCKET": "wework",
            },
            clear=True,
        ):
            _apply_env_overrides(config)

        platform_config = config.platforms[Platform.JUHE]
        assert platform_config.extra["inbound_s3_endpoint_url"] == "http://124.220.81.138:9000"
        assert platform_config.extra["inbound_s3_access_key"] == "minio-ak"
        assert platform_config.extra["inbound_s3_secret_key"] == "minio-sk"
        assert platform_config.extra["inbound_s3_region"] == "us-east-1"
        assert platform_config.extra["inbound_s3_bucket"] == "wework"
        assert platform_config.extra["inbound_s3_use_https"] is False

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

    def test_reads_inbound_s3_config_from_minio_env_aliases(self, monkeypatch):
        monkeypatch.setenv("JUHE_APP_KEY", "env-app")
        monkeypatch.setenv("JUHE_APP_SECRET", "env-secret")
        monkeypatch.setenv("JUHE_GUID", "env-guid")
        monkeypatch.setenv("MINIO_ENDPOINT", "124.220.81.138:9000")
        monkeypatch.setenv("MINIO_ACCESS_KEY", "minio-ak")
        monkeypatch.setenv("MINIO_SECRET_KEY", "minio-sk")
        monkeypatch.setenv("MINIO_USE_HTTPS", "false")
        monkeypatch.setenv("MINIO_REGION", "us-east-1")
        monkeypatch.setenv("MINIO_DEFAULT_BUCKET", "wework")
        from gateway.platforms.juhe import JuheAdapter

        adapter = JuheAdapter(PlatformConfig(enabled=True))

        assert adapter._inbound_s3_endpoint_url == "http://124.220.81.138:9000"
        assert adapter._inbound_s3_access_key == "minio-ak"
        assert adapter._inbound_s3_secret_key == "minio-sk"
        assert adapter._inbound_s3_region == "us-east-1"
        assert adapter._inbound_s3_bucket == "wework"
        assert adapter._inbound_s3_addressing_style == "path"

    def test_resolve_inbound_s3_bucket_key_decodes_url_escaped_object_name(self):
        from gateway.platforms.juhe import JuheAdapter

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "cfg-app",
                    "app_secret": "cfg-secret",
                    "guid": "cfg-guid",
                    "inbound_s3_bucket": "wework",
                },
            )
        )

        bucket, key = adapter._resolve_inbound_s3_bucket_key(
            "http://124.220.81.138:9000/wework/wwcdn/2026-04-18/%E6%B5%8B%E8%AF%95.docx"
        )

        assert bucket == "wework"
        assert key == "wwcdn/2026-04-18/测试.docx"


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

    @pytest.mark.asyncio
    async def test_connect_runs_startup_sync(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module
        from gateway.platforms.juhe import JuheAdapter

        fake_ws_client = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
        fake_task = SimpleNamespace(cancel=MagicMock())
        
        def fake_create_task(coro):
            coro.close()
            return fake_task

        monkeypatch.setattr(juhe_module, "AIOHTTP_AVAILABLE", True)
        monkeypatch.setattr(juhe_module, "HTTPX_AVAILABLE", True)
        monkeypatch.setattr(juhe_module, "QWSAAS_AVAILABLE", True)
        monkeypatch.setattr(juhe_module, "JuheWsClient", MagicMock(return_value=fake_ws_client))
        monkeypatch.setattr(juhe_module.asyncio, "create_task", fake_create_task)

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={"app_key": "app", "app_secret": "secret", "guid": "guid"},
            )
        )
        monkeypatch.setattr(adapter, "_run_startup_sync", AsyncMock())

        success = await adapter.connect()

        assert success is True
        adapter._run_startup_sync.assert_awaited_once()


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
    async def test_callback_updates_local_cache_and_recent_message_index(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 2,
                    "content": '{"msg":"hello indexed"}',
                    "sender": "1001",
                    "sender_name": "Alice",
                    "roomid": "2001",
                    "room_name": "Dev Group",
                    "msg_id": "msg-1",
                    "appinfo": "appinfo-1",
                    "refer_id": 0,
                    "seq": "7272648",
                },
            }
        )

        assert success is True
        cache = JuheCacheStore()
        assert cache.search_contacts("Alice")[0]["user_id"] == "1001"
        assert cache.get_room("2001")["roomname"] == "Dev Group"
        assert cache.get_message(message_id="msg-1")["appinfo"] == "appinfo-1"
        assert cache.load_sync_state()["message_sync_key"] == "7272648"

    @pytest.mark.asyncio
    async def test_refer_id_nonzero_callback_is_indexed_but_not_dispatched(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 2,
                    "content": '{"msg":"duplicate callback"}',
                    "sender": "1001",
                    "sender_name": "Alice",
                    "msg_id": "msg-refer-1",
                    "appinfo": "appinfo-refer-1",
                    "refer_id": 123,
                    "seq": "7272649",
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_not_awaited()
        cache = JuheCacheStore()
        assert cache.get_message(message_id="msg-refer-1")["appinfo"] == "appinfo-refer-1"

    @pytest.mark.asyncio
    async def test_startup_sync_replays_new_message_sync_records(self, tmp_path, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()

        store = JuheCacheStore()
        store.update_sync_state({"message_sync_key": "1"})

        monkeypatch.setattr(
            juhe_module,
            "sync_msg",
            AsyncMock(
                return_value={
                    "error_code": 0,
                    "data": {
                        "max_sync_key": "2",
                        "continue_flag": 0,
                        "msg_list": [
                            {
                                "msg_type": 2,
                                "content": '{"msg":"hello from sync"}',
                                "sender": "1001",
                                "sender_name": "Alice",
                                "msg_id": "sync-msg-1",
                                "appinfo": "sync-appinfo-1",
                                "refer_id": 0,
                                "seq": "2",
                            }
                        ],
                    },
                }
            ),
        )
        monkeypatch.setattr(
            juhe_module,
            "sync_contact",
            AsyncMock(return_value={"error_code": 0, "data": {"last_seq": "", "contact_list": []}}),
        )
        monkeypatch.setattr(
            juhe_module,
            "get_room_list",
            AsyncMock(return_value={"error_code": 0, "data": {"roomdata": {"datas": []}, "next_start": 0, "total": 0}}),
        )
        monkeypatch.setattr(
            juhe_module,
            "sync_label_list",
            AsyncMock(return_value={"error_code": 0, "data": {"seq": "", "label_items": [], "has_next": False}}),
        )

        await adapter._run_startup_sync()

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "hello from sync"
        assert JuheCacheStore().load_sync_state()["message_sync_key"] == "2"

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
    async def test_image_message_dispatches_cached_attachment_event(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        monkeypatch.setattr(
            juhe_module,
            "download_callback_attachment",
            AsyncMock(
                return_value=SimpleNamespace(
                    data=b"image-bytes",
                    file_name="photo.jpg",
                    content_type="image/jpeg",
                )
            ),
        )
        monkeypatch.setattr(
            juhe_module,
            "cache_image_from_bytes",
            MagicMock(return_value="/tmp/cached-photo.jpg"),
        )

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 5,
                    "content": '{"data":{"image":{"url":"https://example.com/a.jpg","file_name":"photo.jpg"}}}',
                    "sender": "1001",
                    "msg_id": "msg-1",
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "[Received image: photo.jpg]"
        assert event.message_type == juhe_module.MessageType.PHOTO
        assert event.media_urls == ["/tmp/cached-photo.jpg"]
        assert event.media_types == ["image/jpeg"]

    @pytest.mark.asyncio
    async def test_cdn_image_callback_dispatches_cached_attachment_event(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        monkeypatch.setattr(
            juhe_module,
            "download_callback_attachment",
            AsyncMock(
                return_value=SimpleNamespace(
                    data=b"image-bytes",
                    file_name="image.jpg",
                    content_type="image/jpeg",
                )
            ),
        )
        monkeypatch.setattr(
            juhe_module,
            "cache_image_from_bytes",
            MagicMock(return_value="/tmp/cached-image.jpg"),
        )

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 5,
                    "content_type": 101,
                    "sender": "1001",
                    "id": "img-1",
                    "file_name": "",
                    "cdn": {
                        "size": 224064,
                        "md5": "md5",
                        "aes_key": "aes",
                        "auth_key": "auth",
                        "file_id": "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=abc",
                    },
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "[Received image: image.jpg]"
        assert event.message_type == juhe_module.MessageType.PHOTO
        assert event.media_urls == ["/tmp/cached-image.jpg"]
        assert event.media_types == ["image/jpeg"]

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
    async def test_file_message_download_failure_falls_back_to_metadata_text(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        monkeypatch.setattr(
            juhe_module,
            "download_callback_attachment",
            AsyncMock(side_effect=RuntimeError("download failed")),
        )

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 8,
                    "content": json.dumps(
                        {
                            "data": {
                                "file": {
                                    "file_name": "report.pdf",
                                    "url": "https://files.example/report.pdf",
                                }
                            }
                        }
                    ),
                    "sender": "1001",
                    "msg_id": "msg-file-1",
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "[Received document metadata only: report.pdf (download unavailable)]"
        assert event.message_type == juhe_module.MessageType.DOCUMENT
        assert event.media_urls == []
        assert event.media_types == []

    @pytest.mark.asyncio
    async def test_large_attachment_falls_back_to_metadata_only(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        monkeypatch.setattr(
            juhe_module,
            "download_callback_attachment",
            AsyncMock(side_effect=juhe_module.QwSaasRequestError("attachment exceeds max_bytes")),
        )

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 8,
                    "content": json.dumps(
                        {
                            "data": {
                                "file": {
                                    "file_name": "big.zip",
                                    "url": "https://files.example/big.zip",
                                    "file_size": 1000,
                                }
                            }
                        }
                    ),
                    "sender": "1001",
                    "msg_id": "msg-file-2",
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "[Received document metadata only: big.zip (exceeds download limit)]"
        assert event.message_type == juhe_module.MessageType.DOCUMENT
        assert event.media_urls == []

    @pytest.mark.asyncio
    async def test_private_object_access_uses_inbound_s3_fallback(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()
        adapter._inbound_s3_endpoint_url = "http://124.220.81.138:9000"
        adapter._inbound_s3_access_key = "minio-ak"
        adapter._inbound_s3_secret_key = "minio-sk"
        adapter._inbound_s3_bucket = "wework"
        adapter._inbound_s3_addressing_style = "path"
        adapter.handle_message = AsyncMock()

        monkeypatch.setattr(
            juhe_module,
            "download_callback_attachment",
            AsyncMock(
                side_effect=juhe_module.QwSaasPrivateObjectAccessError(
                    "resolved private CDN object is not publicly readable",
                    object_url="http://124.220.81.138:9000/wework/wwcdn/private/report.pdf",
                    status_code=403,
                )
            ),
        )
        fake_body = MagicMock()
        fake_body.read.return_value = b"%PDF-1.4\n"
        fake_client = MagicMock()
        fake_client.get_object.return_value = {"Body": fake_body, "ContentType": "application/pdf"}
        monkeypatch.setattr(adapter, "_build_inbound_s3_client", MagicMock(return_value=fake_client))
        monkeypatch.setattr(
            juhe_module,
            "cache_document_from_bytes",
            MagicMock(return_value="/tmp/report.pdf"),
        )

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 8,
                    "content": json.dumps(
                        {
                            "data": {
                                "file": {
                                    "file_name": "report.pdf",
                                    "url": "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=abc",
                                }
                            }
                        }
                    ),
                    "sender": "1001",
                    "msg_id": "msg-file-private-1",
                },
            }
        )

        assert success is True
        fake_client.get_object.assert_called_once_with(Bucket="wework", Key="wwcdn/private/report.pdf")
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "[Received document: report.pdf]"
        assert event.message_type == juhe_module.MessageType.DOCUMENT
        assert event.media_urls == ["/tmp/report.pdf"]
        assert event.media_types == ["application/pdf"]

    @pytest.mark.asyncio
    async def test_private_object_access_without_inbound_s3_config_falls_back_to_metadata(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        monkeypatch.setattr(
            juhe_module,
            "download_callback_attachment",
            AsyncMock(
                side_effect=juhe_module.QwSaasPrivateObjectAccessError(
                    "resolved private CDN object is not publicly readable",
                    object_url="http://124.220.81.138:9000/wework/wwcdn/private/report.pdf",
                    status_code=403,
                )
            ),
        )

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 8,
                    "content": json.dumps(
                        {
                            "data": {
                                "file": {
                                    "file_name": "report.pdf",
                                    "url": "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=abc",
                                }
                            }
                        }
                    ),
                    "sender": "1001",
                    "msg_id": "msg-file-private-2",
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "[Received document metadata only: report.pdf (download unavailable)]"
        assert event.message_type == juhe_module.MessageType.DOCUMENT
        assert event.media_urls == []

    @pytest.mark.asyncio
    async def test_message_with_text_and_document_preserves_text(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        monkeypatch.setattr(
            juhe_module,
            "download_callback_attachment",
            AsyncMock(
                return_value=SimpleNamespace(
                    data=b"%PDF-1.4\n",
                    file_name="report.pdf",
                    content_type="application/pdf",
                )
            ),
        )
        monkeypatch.setattr(
            juhe_module,
            "cache_document_from_bytes",
            MagicMock(return_value="/tmp/report.pdf"),
        )

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 8,
                    "content": json.dumps(
                        {
                            "msg": "please review",
                            "data": {
                                "file": {
                                    "file_name": "report.pdf",
                                    "url": "https://files.example/report.pdf",
                                }
                            },
                        }
                    ),
                    "sender": "1001",
                    "msg_id": "msg-file-3",
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "please review"
        assert event.message_type == juhe_module.MessageType.DOCUMENT
        assert event.media_urls == ["/tmp/report.pdf"]
        assert event.media_types == ["application/pdf"]

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
    async def test_get_remote_file_size_hint_uses_content_range_total_when_head_is_forbidden(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()

        class _FakeStreamResponse:
            status_code = 206
            headers = {
                "Content-Length": "1",
                "Content-Range": "bytes 0-0/33123874",
            }

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def head(self, url):
                return SimpleNamespace(status_code=403, headers={"Content-Length": "263"})

            def stream(self, method, url, headers=None):
                assert method == "GET"
                assert headers == {"Range": "bytes=0-0"}
                return _FakeStreamResponse()

        monkeypatch.setattr(juhe_module.httpx, "AsyncClient", _FakeAsyncClient)

        size_hint = await adapter._get_remote_file_size_hint("https://files.example.com/big.zip")

        assert size_hint == 33123874

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


class TestJuheExampleFiles:
    def test_env_example_has_juhe_vars(self):
        env_path = Path(__file__).resolve().parents[2] / ".env.example"
        content = env_path.read_text(encoding="utf-8")

        assert "JUHE_APP_KEY" in content
        assert "JUHE_INBOUND_ATTACHMENT_MAX_BYTES" in content
        assert "JUHE_INBOUND_S3_ENDPOINT_URL" in content
        assert "MINIO_ENDPOINT" in content

    def test_config_example_has_juhe_platform_block(self):
        config_path = Path(__file__).resolve().parents[2] / "config.example.yaml"
        content = config_path.read_text(encoding="utf-8")

        assert "platforms:" in content
        assert "juhe:" in content
        assert "inbound_attachment_max_bytes" in content
        assert "inbound_s3_endpoint_url" in content
