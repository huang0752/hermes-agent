"""Tests for the Juhe platform adapter."""

import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig, _apply_env_overrides
from gateway.juhe_cache import JuheCacheStore
from gateway.platforms.base import MessageType
from gateway.session import SessionSource
from hermes_state import SessionDB


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
                "trigger_user_ids": ["1001", "1002"],
                "group_sessions_per_user": False,
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
        monkeypatch.delenv("JUHE_S3_ACCESS_KEY", raising=False)
        monkeypatch.delenv("JUHE_S3_SECRET_KEY", raising=False)
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
        monkeypatch.delenv("JUHE_INBOUND_S3_ENDPOINT_URL", raising=False)
        monkeypatch.delenv("JUHE_INBOUND_S3_ACCESS_KEY", raising=False)
        monkeypatch.delenv("JUHE_INBOUND_S3_SECRET_KEY", raising=False)
        monkeypatch.delenv("JUHE_INBOUND_S3_REGION", raising=False)
        monkeypatch.delenv("JUHE_INBOUND_S3_BUCKET", raising=False)
        monkeypatch.delenv("JUHE_INBOUND_S3_USE_HTTPS", raising=False)
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

    def test_reads_inbound_s3_presigned_url_expiry_from_config(self):
        from gateway.platforms.juhe import JuheAdapter

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "cfg-app",
                    "app_secret": "cfg-secret",
                    "guid": "cfg-guid",
                    "inbound_s3_url_expires_seconds": 900,
                },
            )
        )

        assert adapter._inbound_s3_url_expires_seconds == 900

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

    def test_resolve_inbound_s3_bucket_key_rejects_weixin_download_url(self):
        from gateway.platforms.juhe import JuheAdapter

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "cfg-app",
                    "app_secret": "cfg-secret",
                    "guid": "cfg-guid",
                    "inbound_s3_endpoint_url": "http://124.220.81.138:9000",
                    "inbound_s3_bucket": "wework",
                },
            )
        )

        with pytest.raises(RuntimeError):
            adapter._resolve_inbound_s3_bucket_key(
                "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=abc"
            )

    def test_resolve_inbound_s3_bucket_key_rejects_minio_cgi_proxy_url(self):
        from gateway.platforms.juhe import JuheAdapter

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "cfg-app",
                    "app_secret": "cfg-secret",
                    "guid": "cfg-guid",
                    "inbound_s3_endpoint_url": "http://124.220.81.138:9000",
                    "inbound_s3_bucket": "wework",
                },
            )
        )

        with pytest.raises(RuntimeError):
            adapter._resolve_inbound_s3_bucket_key(
                "http://124.220.81.138:9000/cgi-bin/mmae-bin/tpdownloadmedia?param=abc"
            )

    def test_generate_inbound_presigned_url_reuses_existing_private_object(self, monkeypatch):
        from gateway.platforms.juhe import JuheAdapter

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "cfg-app",
                    "app_secret": "cfg-secret",
                    "guid": "cfg-guid",
                    "inbound_s3_bucket": "wework",
                    "inbound_s3_url_expires_seconds": 900,
                },
            )
        )
        fake_client = MagicMock()
        fake_client.generate_presigned_url.return_value = (
            "https://signed.example.com/wework/wwcdn/private/report.pdf?sig=1"
        )
        monkeypatch.setattr(adapter, "_build_inbound_s3_client", MagicMock(return_value=fake_client))

        result = adapter._generate_inbound_s3_presigned_get_url_sync(
            "http://124.220.81.138:9000/wework/wwcdn/private/report.pdf"
        )

        assert result == "https://signed.example.com/wework/wwcdn/private/report.pdf?sig=1"
        fake_client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={"Bucket": "wework", "Key": "wwcdn/private/report.pdf"},
            ExpiresIn=900,
        )

    @pytest.mark.asyncio
    async def test_build_attachment_event_resolves_private_url_before_presigning_when_only_weixin_file_id_exists(
        self, monkeypatch
    ):
        from gateway.platforms.juhe import JuheAdapter

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "cfg-app",
                    "app_secret": "cfg-secret",
                    "guid": "cfg-guid",
                    "inbound_s3_endpoint_url": "http://124.220.81.138:9000",
                    "inbound_s3_bucket": "wework",
                },
            )
        )
        adapter._running = True
        source = SessionSource(
            platform=Platform.JUHE,
            chat_id="S:1001",
            chat_type="dm",
            user_id="1001",
        )
        message = SimpleNamespace(
            text="",
            message_id="msg-private-image-1",
            message_type=5,
            raw_message={
                "msg_type": 5,
                "file_name": "license.jpg",
                "cdn": {
                    "file_id": "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=abc",
                    "aes_key": "aes",
                    "auth_key": "auth",
                },
            },
        )

        monkeypatch.setattr(
            adapter,
            "_resolve_private_attachment_object_url",
            AsyncMock(return_value="http://124.220.81.138:9000/wework/wwcdn/private/license.jpg"),
        )
        monkeypatch.setattr(
            adapter,
            "_generate_inbound_s3_presigned_get_url_sync",
            MagicMock(return_value="http://124.220.81.138:9000/wework/wwcdn/private/license.jpg?X-Amz-Signature=1"),
        )

        event = await adapter._build_attachment_event(message=message, source=source)

        adapter._resolve_private_attachment_object_url.assert_awaited_once()
        adapter._generate_inbound_s3_presigned_get_url_sync.assert_called_once_with(
            "http://124.220.81.138:9000/wework/wwcdn/private/license.jpg"
        )
        assert getattr(event, "_juhe_media_access_urls") == [
            "http://124.220.81.138:9000/wework/wwcdn/private/license.jpg?X-Amz-Signature=1"
        ]

    @pytest.mark.asyncio
    async def test_build_attachment_event_treats_xlsx_as_url_only_and_enqueues_preheat(
        self, monkeypatch
    ):
        from gateway.platforms.juhe import JuheAdapter, _ResolvedAttachmentAccessTarget

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "cfg-app",
                    "app_secret": "cfg-secret",
                    "guid": "cfg-guid",
                    "inbound_s3_endpoint_url": "http://124.220.81.138:9000",
                    "inbound_s3_bucket": "wework",
                },
            )
        )
        adapter._running = True
        source = SessionSource(
            platform=Platform.JUHE,
            chat_id="S:1001",
            chat_type="dm",
            user_id="1001",
        )
        message = SimpleNamespace(
            text="",
            message_id="msg-sheet-1",
            message_type=8,
            raw_message={
                "msg_type": 8,
                "file_name": "sheet.xlsx",
                "size": 2048,
                "cdn": {
                    "file_id": "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=sheet",
                    "object_url": "http://124.220.81.138:9000/wework/wwcdn/private/sheet.xlsx",
                },
            },
        )

        monkeypatch.setattr(
            adapter,
            "_resolve_inbound_attachment_access_target",
            AsyncMock(
                return_value=_ResolvedAttachmentAccessTarget(
                    url="http://124.220.81.138:9000/wework/wwcdn/private/sheet.xlsx",
                    object_url="http://124.220.81.138:9000/wework/wwcdn/private/sheet.xlsx",
                )
            ),
        )
        monkeypatch.setattr(
            adapter,
            "_generate_inbound_s3_presigned_get_url_sync",
            MagicMock(
                return_value="http://124.220.81.138:9000/wework/wwcdn/private/sheet.xlsx?X-Amz-Signature=1"
            ),
        )
        schedule_mock = MagicMock()
        monkeypatch.setattr(adapter, "_schedule_attachment_preheat", schedule_mock, raising=False)
        monkeypatch.setattr(
            "gateway.platforms.juhe.download_callback_attachment",
            AsyncMock(side_effect=AssertionError("download_callback_attachment should not be called for xlsx")),
        )

        event = await adapter._build_attachment_event(message=message, source=source)

        assert event.message_type == MessageType.DOCUMENT
        assert event.media_urls[0].startswith("juhe://attachment/")
        assert event.media_urls[0].endswith("/sheet.xlsx")
        assert event.media_types == ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"]
        assert getattr(event, "_juhe_media_access_urls") == [
            "http://124.220.81.138:9000/wework/wwcdn/private/sheet.xlsx?X-Amz-Signature=1"
        ]
        schedule_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_build_attachment_event_prefers_stable_file_identity_when_resolved_object_url_changes(
        self, monkeypatch
    ):
        from gateway.platforms.juhe import JuheAdapter, _ResolvedAttachmentAccessTarget

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "cfg-app",
                    "app_secret": "cfg-secret",
                    "guid": "cfg-guid",
                    "inbound_s3_endpoint_url": "http://124.220.81.138:9000",
                    "inbound_s3_bucket": "wework",
                },
            )
        )
        adapter._running = True
        source = SessionSource(
            platform=Platform.JUHE,
            chat_id="S:1001",
            chat_type="dm",
            user_id="1001",
        )
        message = SimpleNamespace(
            text="",
            message_id="msg-image-stable-id-1",
            message_type=5,
            raw_message={
                "msg_type": 5,
                "file_name": "image.jpg",
                "cdn": {
                    "file_id": "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=abc",
                    "md5": "same-md5",
                    "size": 2048,
                    "aes_key": "aes",
                    "auth_key": "auth",
                },
            },
        )

        first_target = _ResolvedAttachmentAccessTarget(
            url="http://124.220.81.138:9000/wework/wwcdn/2026-04-20/first_image.jpg",
            object_url="http://124.220.81.138:9000/wework/wwcdn/2026-04-20/first_image.jpg",
        )
        second_target = _ResolvedAttachmentAccessTarget(
            url="http://124.220.81.138:9000/wework/wwcdn/2026-04-20/second_image.jpg",
            object_url="http://124.220.81.138:9000/wework/wwcdn/2026-04-20/second_image.jpg",
        )
        monkeypatch.setattr(
            adapter,
            "_resolve_inbound_attachment_access_target",
            AsyncMock(side_effect=[first_target, second_target]),
        )
        monkeypatch.setattr(
            adapter,
            "_generate_inbound_s3_presigned_get_url_sync",
            MagicMock(
                side_effect=[
                    "http://124.220.81.138:9000/wework/wwcdn/2026-04-20/first_image.jpg?X-Amz-Signature=1",
                    "http://124.220.81.138:9000/wework/wwcdn/2026-04-20/second_image.jpg?X-Amz-Signature=1",
                ]
            ),
        )
        schedule_mock = MagicMock()
        monkeypatch.setattr(adapter, "_schedule_attachment_preheat", schedule_mock, raising=False)

        first_event = await adapter._build_attachment_event(message=message, source=source)
        second_event = await adapter._build_attachment_event(message=message, source=source)

        first_identity = getattr(first_event, "_juhe_attachment_identities")[0]
        second_identity = getattr(second_event, "_juhe_attachment_identities")[0]
        assert first_identity["file_id"] == second_identity["file_id"]
        assert first_identity["file_md5"] == second_identity["file_md5"] == "same-md5"
        assert first_identity["bucket"] == ""
        assert first_identity["object_key"] == ""
        assert second_identity["bucket"] == ""
        assert second_identity["object_key"] == ""
        assert first_event.media_urls == second_event.media_urls
        assert schedule_mock.call_count == 2

    @pytest.mark.asyncio
    async def test_build_attachment_event_skips_preheat_for_large_supported_document(
        self, monkeypatch
    ):
        from gateway.platforms.juhe import JuheAdapter, _ResolvedAttachmentAccessTarget

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "cfg-app",
                    "app_secret": "cfg-secret",
                    "guid": "cfg-guid",
                    "inbound_s3_endpoint_url": "http://124.220.81.138:9000",
                    "inbound_s3_bucket": "wework",
                },
            )
        )
        source = SessionSource(
            platform=Platform.JUHE,
            chat_id="S:1001",
            chat_type="dm",
            user_id="1001",
        )
        message = SimpleNamespace(
            text="",
            message_id="msg-report-1",
            message_type=8,
            raw_message={
                "msg_type": 8,
                "file_name": "report.pdf",
                "size": 5 * 1024 * 1024,
                "cdn": {
                    "file_id": "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=report",
                    "object_url": "http://124.220.81.138:9000/wework/wwcdn/private/report.pdf",
                },
            },
        )

        monkeypatch.setattr(
            adapter,
            "_resolve_inbound_attachment_access_target",
            AsyncMock(
                return_value=_ResolvedAttachmentAccessTarget(
                    url="http://124.220.81.138:9000/wework/wwcdn/private/report.pdf",
                    object_url="http://124.220.81.138:9000/wework/wwcdn/private/report.pdf",
                )
            ),
        )
        monkeypatch.setattr(
            adapter,
            "_generate_inbound_s3_presigned_get_url_sync",
            MagicMock(
                return_value="http://124.220.81.138:9000/wework/wwcdn/private/report.pdf?X-Amz-Signature=1"
            ),
        )
        schedule_mock = MagicMock()
        monkeypatch.setattr(adapter, "_schedule_attachment_preheat", schedule_mock, raising=False)

        event = await adapter._build_attachment_event(message=message, source=source)

        assert event.message_type == MessageType.DOCUMENT
        assert event.media_urls[0].endswith("/report.pdf")
        schedule_mock.assert_called_once()
        assert schedule_mock.call_args.kwargs["file_size"] == 5 * 1024 * 1024


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

    def test_trigger_user_ids_and_mentions_control_group_triggering(self):
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
                    "trigger_user_ids": ["1001"],
                },
            )
        )

        assert adapter._should_trigger_group_reply("R:2001", "1001", [], "") is False
        assert adapter._should_trigger_group_reply("R:2001", "1001", ["bot"], "hello") is True
        assert adapter._should_trigger_group_reply("R:2001", "1001", [], "@bot hello") is True
        assert adapter._should_trigger_group_reply("R:2001", "1002", ["bot"], "hello") is False

    def test_dm_allowlist_uses_dynamic_access_control_json_when_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("JUHE_ALLOWED_USERS", "S:1001")
        from gateway.platforms.juhe import JuheAdapter

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "app",
                    "app_secret": "secret",
                    "guid": "guid",
                    "dm_policy": "allowlist",
                },
            )
        )

        assert adapter._is_dm_allowed("1001") is True
        assert adapter._is_dm_allowed("1002") is False

        access_path = tmp_path / "juhe" / "access_control.json"
        access_path.parent.mkdir(parents=True, exist_ok=True)
        access_path.write_text(
            json.dumps({"dm_allow_from": ["S:1002"], "group_trigger_user_ids": []}),
            encoding="utf-8",
        )

        assert adapter._is_dm_allowed("1001") is False
        assert adapter._is_dm_allowed("1002") is True

    def test_group_trigger_users_reload_from_dynamic_access_control_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("JUHE_TRIGGER_USER_IDS", "S:1001")
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
                },
            )
        )

        assert adapter._should_trigger_group_reply("R:2001", "1001", ["bot"], "hello") is True
        assert adapter._should_trigger_group_reply("R:2001", "1002", ["bot"], "hello") is False

        access_path = tmp_path / "juhe" / "access_control.json"
        access_path.parent.mkdir(parents=True, exist_ok=True)
        access_path.write_text(
            json.dumps({"dm_allow_from": [], "group_trigger_user_ids": ["S:1002"]}),
            encoding="utf-8",
        )

        assert adapter._should_trigger_group_reply("R:2001", "1001", ["bot"], "hello") is False
        assert adapter._should_trigger_group_reply("R:2001", "1002", ["bot"], "hello") is True


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
    async def test_image_message_dispatches_url_only_attachment_event(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        monkeypatch.setattr(
            juhe_module,
            "download_callback_attachment",
            AsyncMock(side_effect=AssertionError("download_callback_attachment should not be called")),
        )
        monkeypatch.setattr(
            juhe_module,
            "cache_image_from_bytes",
            MagicMock(side_effect=AssertionError("cache_image_from_bytes should not be called")),
        )
        monkeypatch.setattr(
            adapter,
            "_generate_inbound_s3_presigned_get_url_sync",
            MagicMock(return_value="http://124.220.81.138:9000/wework/wwcdn/private/photo.jpg?X-Amz-Signature=1"),
        )

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 5,
                    "content": json.dumps(
                        {
                            "data": {
                                "image": {
                                    "url": "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=abc",
                                    "file_name": "photo.jpg",
                                    "object_url": "http://124.220.81.138:9000/wework/wwcdn/private/photo.jpg",
                                }
                            }
                        }
                    ),
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
        assert len(event.media_urls) == 1
        assert event.media_urls[0].startswith("juhe://attachment/")
        assert event.media_urls[0].endswith("/photo.jpg")
        assert event.media_types == ["image/jpeg"]
        assert getattr(event, "_juhe_media_access_urls") == [
            "http://124.220.81.138:9000/wework/wwcdn/private/photo.jpg?X-Amz-Signature=1"
        ]
        assert getattr(event, "_juhe_media_descriptions") == [
            "图片 photo.jpg：已收到，但暂未生成描述"
        ]

    @pytest.mark.asyncio
    async def test_cdn_image_callback_dispatches_url_only_attachment_event(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        monkeypatch.setattr(
            juhe_module,
            "download_callback_attachment",
            AsyncMock(side_effect=AssertionError("download_callback_attachment should not be called")),
        )
        monkeypatch.setattr(
            juhe_module,
            "cache_image_from_bytes",
            MagicMock(side_effect=AssertionError("cache_image_from_bytes should not be called")),
        )
        monkeypatch.setattr(
            adapter,
            "_generate_inbound_s3_presigned_get_url_sync",
            MagicMock(return_value="http://124.220.81.138:9000/wework/wwcdn/private/image.jpg?X-Amz-Signature=1"),
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
                        "object_url": "http://124.220.81.138:9000/wework/wwcdn/private/image.jpg",
                    },
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "[Received image: image.jpg]"
        assert event.message_type == juhe_module.MessageType.PHOTO
        assert len(event.media_urls) == 1
        assert event.media_urls[0].startswith("juhe://attachment/")
        assert event.media_urls[0].endswith("/image.jpg")
        assert event.media_types == ["image/jpeg"]
        assert getattr(event, "_juhe_media_access_urls") == [
            "http://124.220.81.138:9000/wework/wwcdn/private/image.jpg?X-Amz-Signature=1"
        ]
        assert getattr(event, "_juhe_media_descriptions") == [
            "图片 image.jpg：已收到，但暂未生成描述"
        ]

    @pytest.mark.asyncio
    async def test_image_attachment_normalizes_octet_stream_media_type(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        monkeypatch.setattr(
            juhe_module,
            "download_callback_attachment",
            AsyncMock(side_effect=AssertionError("download_callback_attachment should not be called")),
        )
        monkeypatch.setattr(
            juhe_module,
            "cache_image_from_bytes",
            MagicMock(side_effect=AssertionError("cache_image_from_bytes should not be called")),
        )
        monkeypatch.setattr(
            adapter,
            "_generate_inbound_s3_presigned_get_url_sync",
            MagicMock(return_value="http://124.220.81.138:9000/wework/wwcdn/private/image.jpg?X-Amz-Signature=1"),
        )

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 5,
                    "content_type": 101,
                    "sender": "1001",
                    "id": "img-octet-1",
                    "file_name": "image.jpg",
                    "cdn": {
                        "size": 224064,
                        "md5": "md5",
                        "aes_key": "aes",
                        "auth_key": "auth",
                        "file_id": "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=abc",
                        "object_url": "http://124.220.81.138:9000/wework/wwcdn/private/image.jpg",
                    },
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.message_type == juhe_module.MessageType.PHOTO
        assert len(event.media_urls) == 1
        assert event.media_urls[0].startswith("juhe://attachment/")
        assert event.media_urls[0].endswith("/image.jpg")
        assert event.media_types == ["image/jpeg"]

    @pytest.mark.asyncio
    async def test_private_image_object_reference_attaches_presigned_minio_url_without_download(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        monkeypatch.setattr(
            juhe_module,
            "download_callback_attachment",
            AsyncMock(side_effect=AssertionError("download_callback_attachment should not be called")),
        )
        monkeypatch.setattr(
            adapter,
            "_generate_inbound_s3_presigned_get_url_sync",
            MagicMock(return_value="http://124.220.81.138:9000/wework/wwcdn/private/license.jpg?X-Amz-Signature=1"),
        )

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 5,
                    "content": json.dumps(
                        {
                            "data": {
                                "image": {
                                    "file_name": "license.jpg",
                                    "url": "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=abc",
                                    "object_url": "http://124.220.81.138:9000/wework/wwcdn/private/license.jpg",
                                }
                            }
                        }
                    ),
                    "sender": "1001",
                    "msg_id": "msg-image-private-1",
                },
            }
        )

        assert success is True
        adapter._generate_inbound_s3_presigned_get_url_sync.assert_called_once_with(
            "http://124.220.81.138:9000/wework/wwcdn/private/license.jpg"
        )
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert len(event.media_urls) == 1
        assert event.media_urls[0].startswith("juhe://attachment/")
        assert event.media_urls[0].endswith("/license.jpg")
        assert event.media_types == ["image/jpeg"]
        assert getattr(event, "_juhe_media_access_urls") == [
            "http://124.220.81.138:9000/wework/wwcdn/private/license.jpg?X-Amz-Signature=1"
        ]
        assert getattr(event, "_juhe_media_descriptions") == [
            "图片 license.jpg：已收到，但暂未生成描述"
        ]

    @pytest.mark.asyncio
    async def test_build_attachment_event_avoids_presigning_minio_cgi_proxy_url(self, monkeypatch):
        from gateway.platforms.juhe import JuheAdapter

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "cfg-app",
                    "app_secret": "cfg-secret",
                    "guid": "cfg-guid",
                    "inbound_s3_endpoint_url": "http://124.220.81.138:9000",
                    "inbound_s3_bucket": "wework",
                },
            )
        )
        adapter._running = True
        source = SessionSource(
            platform=Platform.JUHE,
            chat_id="S:1001",
            chat_type="dm",
            user_id="1001",
        )
        message = SimpleNamespace(
            text="",
            message_id="msg-private-image-proxy-1",
            message_type=5,
            raw_message={
                "msg_type": 5,
                "file_name": "license.jpg",
                "cdn": {
                    "file_id": "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=abc",
                    "aes_key": "aes",
                    "auth_key": "auth",
                },
            },
        )

        proxy_url = "http://124.220.81.138:9000/cgi-bin/mmae-bin/tpdownloadmedia?param=abc"
        monkeypatch.setattr(
            adapter,
            "_resolve_private_attachment_object_url",
            AsyncMock(return_value=proxy_url),
        )
        monkeypatch.setattr(
            adapter,
            "_generate_inbound_s3_presigned_get_url_sync",
            MagicMock(side_effect=AssertionError("proxy URLs must not be presigned as object keys")),
        )

        event = await adapter._build_attachment_event(message=message, source=source)

        adapter._resolve_private_attachment_object_url.assert_awaited_once()
        assert getattr(event, "_juhe_media_access_urls") == [proxy_url]

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
    async def test_file_message_without_access_url_falls_back_to_metadata_text(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        monkeypatch.setattr(
            juhe_module,
            "download_callback_attachment",
            AsyncMock(side_effect=AssertionError("download_callback_attachment should not be called")),
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
    async def test_download_inbound_attachment_uses_raw_cdn_fields_when_message_attrs_missing(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()
        mocked_download = AsyncMock(
            return_value=SimpleNamespace(
                data=b"spreadsheet-bytes",
                file_name="report.xlsx",
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        )
        monkeypatch.setattr(juhe_module, "download_callback_attachment", mocked_download)

        message = SimpleNamespace(
            raw_message={
                "msg_type": 8,
                "file_name": "report.xlsx",
                "cdn": {
                    "size": 35285,
                    "md5": "8a88ec207c3c10a885ff755e1a91b9cf",
                    "aes_key": "aes-key",
                    "auth_key": "auth-key",
                    "file_id": "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=abc",
                },
            },
            message_type=8,
        )

        result = await adapter._download_inbound_attachment(message)

        assert result.file_name == "report.xlsx"
        mocked_download.assert_awaited_once()
        kwargs = mocked_download.await_args.kwargs
        assert kwargs["download_url"] == "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=abc"
        assert kwargs["file_id"] == "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=abc"
        assert kwargs["file_name"] == "report.xlsx"
        assert kwargs["file_size"] == 35285
        assert kwargs["aes_key"] == "aes-key"
        assert kwargs["auth_key"] == "auth-key"
        assert kwargs["attachment_kind"] == "document"

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
    async def test_private_document_object_reference_attaches_presigned_minio_url_without_download(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()

        monkeypatch.setattr(
            juhe_module,
            "download_callback_attachment",
            AsyncMock(side_effect=AssertionError("download_callback_attachment should not be called")),
        )
        monkeypatch.setattr(
            adapter,
            "_generate_inbound_s3_presigned_get_url_sync",
            MagicMock(return_value="http://124.220.81.138:9000/wework/wwcdn/private/report.pdf?X-Amz-Signature=1"),
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
                                    "object_url": "http://124.220.81.138:9000/wework/wwcdn/private/report.pdf",
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
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "[Received document: report.pdf]"
        assert event.message_type == juhe_module.MessageType.DOCUMENT
        assert len(event.media_urls) == 1
        assert event.media_urls[0].startswith("juhe://attachment/")
        assert event.media_urls[0].endswith("/report.pdf")
        assert event.media_types == ["application/pdf"]
        assert getattr(event, "_juhe_media_access_urls") == [
            "http://124.220.81.138:9000/wework/wwcdn/private/report.pdf?X-Amz-Signature=1"
        ]
        assert getattr(event, "_juhe_media_descriptions") == [
            "文件 report.pdf：已收到，待解析"
        ]

    @pytest.mark.asyncio
    async def test_private_document_object_reference_without_presign_falls_back_to_metadata(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        monkeypatch.setattr(
            juhe_module,
            "download_callback_attachment",
            AsyncMock(side_effect=AssertionError("download_callback_attachment should not be called")),
        )
        monkeypatch.setattr(
            adapter,
            "_generate_inbound_s3_presigned_get_url_sync",
            MagicMock(side_effect=RuntimeError("presign failed")),
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
                                    "object_url": "http://124.220.81.138:9000/wework/wwcdn/private/report.pdf",
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
    async def test_message_with_text_and_document_preserves_text_without_local_cache(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        monkeypatch.setattr(
            juhe_module,
            "download_callback_attachment",
            AsyncMock(side_effect=AssertionError("download_callback_attachment should not be called")),
        )
        monkeypatch.setattr(
            adapter,
            "_generate_inbound_s3_presigned_get_url_sync",
            MagicMock(return_value="http://124.220.81.138:9000/wework/wwcdn/private/report.pdf?X-Amz-Signature=1"),
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
                                    "object_url": "http://124.220.81.138:9000/wework/wwcdn/private/report.pdf",
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
        assert len(event.media_urls) == 1
        assert event.media_urls[0].startswith("juhe://attachment/")
        assert event.media_urls[0].endswith("/report.pdf")
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
    async def test_non_trigger_group_message_is_buffered_without_dispatch(self, tmp_path, monkeypatch):
        from gateway.platforms.juhe import JuheAdapter

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "app",
                    "app_secret": "secret",
                    "guid": "guid-123",
                    "group_policy": "allowlist",
                    "group_allow_from": ["R:2001"],
                    "trigger_user_ids": ["1001"],
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
                    "content": '{"msg":"hello group backlog"}',
                    "sender": "1002",
                    "roomid": "2001",
                    "id": "juhe-id-buffer-1",
                    "at_list": ["bot"],
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_not_awaited()
        room_log = SessionDB(db_path=tmp_path / "state.db")
        try:
            history = room_log.get_room_history("juhe", "R:2001", limit=10)
        finally:
            room_log.close()
        assert [item["message_id"] for item in history] == ["juhe-id-buffer-1"]
        assert history[0]["triggered"] is False
        assert history[0]["consumed_for_context"] is False

    @pytest.mark.asyncio
    async def test_non_trigger_group_attachment_schedules_background_preheat(self, tmp_path, monkeypatch):
        from gateway.platforms.juhe import JuheAdapter, _ResolvedAttachmentAccessTarget

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "app",
                    "app_secret": "secret",
                    "guid": "guid-123",
                    "group_policy": "allowlist",
                    "group_allow_from": ["R:2001"],
                    "trigger_user_ids": ["1001"],
                    "inbound_s3_endpoint_url": "http://124.220.81.138:9000",
                    "inbound_s3_bucket": "wework",
                },
            )
        )
        adapter.handle_message = AsyncMock()
        schedule_mock = MagicMock()
        monkeypatch.setattr(adapter, "_schedule_attachment_preheat", schedule_mock, raising=False)
        monkeypatch.setattr(
            adapter,
            "_resolve_inbound_attachment_access_target",
            AsyncMock(
                return_value=_ResolvedAttachmentAccessTarget(
                    url="http://124.220.81.138:9000/wework/wwcdn/private/group-sheet.xlsx",
                    object_url="http://124.220.81.138:9000/wework/wwcdn/private/group-sheet.xlsx",
                )
            ),
        )
        monkeypatch.setattr(
            adapter,
            "_generate_inbound_s3_presigned_get_url_sync",
            MagicMock(
                return_value=(
                    "http://124.220.81.138:9000/wework/wwcdn/private/group-sheet.xlsx"
                    "?X-Amz-Signature=1"
                )
            ),
        )

        success = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 8,
                    "content_type": 102,
                    "sender": "1002",
                    "roomid": "2001",
                    "id": "juhe-id-buffer-file-1",
                    "file_name": "group-sheet.xlsx",
                    "cdn": {
                        "size": 20139,
                        "md5": "2755fb84185cee98987b35bb9f4481b8",
                        "aes_key": "aes-key",
                        "auth_key": "auth-key",
                        "file_id": "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=group-file",
                        "object_url": "http://124.220.81.138:9000/wework/wwcdn/private/group-sheet.xlsx",
                    },
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_not_awaited()
        schedule_mock.assert_called_once()
        room_log = SessionDB(db_path=tmp_path / "state.db")
        try:
            history = room_log.get_room_history("juhe", "R:2001", limit=10)
        finally:
            room_log.close()
        assert [item["message_id"] for item in history] == ["juhe-id-buffer-file-1"]
        assert history[0]["triggered"] is False
        assert history[0]["consumed_for_context"] is False

    @pytest.mark.asyncio
    async def test_trigger_group_message_rehydrates_buffered_document_attachment(self, tmp_path, monkeypatch):
        from gateway.platforms.juhe import JuheAdapter

        import gateway.platforms.juhe as juhe_module

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(
            juhe_module,
            "download_callback_attachment",
            AsyncMock(
                return_value=SimpleNamespace(
                    data=b"sheet-bytes",
                    file_name="group-sheet.xlsx",
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            ),
        )
        monkeypatch.setattr(
            juhe_module,
            "cache_document_from_bytes",
            MagicMock(return_value="/tmp/group-sheet.xlsx"),
        )

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "app",
                    "app_secret": "secret",
                    "guid": "guid-123",
                    "group_policy": "allowlist",
                    "group_allow_from": ["R:2001"],
                    "trigger_user_ids": ["1001"],
                },
            )
        )
        adapter.handle_message = AsyncMock()

        buffered = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 8,
                    "content_type": 102,
                    "sender": "1002",
                    "roomid": "2001",
                    "id": "juhe-id-group-file-1",
                    "file_name": "group-sheet.xlsx",
                    "cdn": {
                        "size": 20139,
                        "md5": "2755fb84185cee98987b35bb9f4481b8",
                        "aes_key": "aes-key",
                        "auth_key": "auth-key",
                        "file_id": "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=group-file",
                    },
                },
            }
        )

        assert buffered is True
        adapter.handle_message.assert_not_awaited()

        triggered = await adapter._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 2,
                    "content_type": 2,
                    "content": "@bot 我发的文件你读取得到吗",
                    "sender": "1001",
                    "roomid": "2001",
                    "id": "juhe-id-group-file-2",
                    "at_list": ["bot"],
                },
            }
        )

        assert triggered is True
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "@bot 我发的文件你读取得到吗"
        assert event.media_urls == ["/tmp/group-sheet.xlsx"]
        assert (
            event.media_types
            == ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"]
        )
        assert "[文件] group-sheet.xlsx" in getattr(event, "_juhe_pending_context_text", "")

    @pytest.mark.asyncio
    async def test_trigger_group_message_with_structured_mention_reuses_persistent_room_context(self, tmp_path, monkeypatch):
        from gateway.platforms.juhe import JuheAdapter

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        first = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "app",
                    "app_secret": "secret",
                    "guid": "guid-123",
                    "group_policy": "allowlist",
                    "group_allow_from": ["R:2001"],
                    "trigger_user_ids": ["1001"],
                    "group_sessions_per_user": False,
                },
            )
        )
        first.handle_message = AsyncMock()

        buffered = await first._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 2,
                    "content": "Earlier customer context",
                    "sender": "1002",
                    "roomid": "2001",
                    "id": "juhe-id-room-1",
                },
            }
        )
        assert buffered is True
        first.handle_message.assert_not_awaited()

        not_triggered = await first._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 2,
                    "content_type": 2,
                    "content": "ping juhe without mention",
                    "sender": "1001",
                    "roomid": "2001",
                    "id": "juhe-id-room-2",
                    "at_list": [],
                },
            }
        )

        assert not_triggered is True
        first.handle_message.assert_not_awaited()

        room_log = SessionDB(db_path=tmp_path / "state.db")
        try:
            history = room_log.get_room_history("juhe", "R:2001", limit=10)
        finally:
            room_log.close()

        assert [item["message_id"] for item in history] == ["juhe-id-room-1", "juhe-id-room-2"]
        assert history[0]["consumed_for_context"] is False
        assert history[1]["triggered"] is False
        assert history[1]["consumed_for_context"] is False

    @pytest.mark.asyncio
    async def test_trigger_group_message_allows_textual_mention_fallback_when_at_list_missing(self, tmp_path, monkeypatch):
        from gateway.platforms.juhe import JuheAdapter

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "app",
                    "app_secret": "secret",
                    "guid": "guid-123",
                    "group_policy": "allowlist",
                    "group_allow_from": ["R:2001"],
                    "trigger_user_ids": ["1001"],
                    "group_sessions_per_user": False,
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
                    "content": "@bot ping juhe with textual mention fallback",
                    "sender": "1001",
                    "roomid": "2001",
                    "id": "juhe-id-room-textual-mention-1",
                    "at_list": [],
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "@bot ping juhe with textual mention fallback"

    @pytest.mark.asyncio
    async def test_trigger_group_message_requires_mention_and_reuses_persistent_room_context(self, tmp_path, monkeypatch):
        from gateway.platforms.juhe import JuheAdapter

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        first = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "app",
                    "app_secret": "secret",
                    "guid": "guid-123",
                    "group_policy": "allowlist",
                    "group_allow_from": ["R:2001"],
                    "trigger_user_ids": ["1001"],
                    "group_sessions_per_user": False,
                },
            )
        )
        first.handle_message = AsyncMock()

        buffered = await first._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 2,
                    "content": "Earlier customer context",
                    "sender": "1002",
                    "roomid": "2001",
                    "id": "juhe-id-room-1",
                },
            }
        )
        assert buffered is True
        first.handle_message.assert_not_awaited()

        not_triggered = await first._handle_callback_event(
            {
                "guid": "guid-123",
                "notify_type": 11010,
                "data": {
                    "msg_type": 2,
                    "content_type": 2,
                    "content": "ping juhe without mention",
                    "sender": "1001",
                    "roomid": "2001",
                    "id": "juhe-id-room-2",
                    "at_list": [],
                },
            }
        )

        assert not_triggered is True
        first.handle_message.assert_not_awaited()

        room_log = SessionDB(db_path=tmp_path / "state.db")
        try:
            history = room_log.get_room_history("juhe", "R:2001", limit=10)
        finally:
            room_log.close()

        assert [item["message_id"] for item in history] == ["juhe-id-room-1", "juhe-id-room-2"]
        assert history[0]["consumed_for_context"] is False
        assert history[1]["triggered"] is False
        assert history[1]["consumed_for_context"] is False

        adapter = JuheAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_key": "app",
                    "app_secret": "secret",
                    "guid": "guid-123",
                    "group_policy": "allowlist",
                    "group_allow_from": ["R:2001"],
                    "trigger_user_ids": ["1001"],
                    "group_sessions_per_user": False,
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
                    "content": "ping juhe with mention",
                    "sender": "1001",
                    "roomid": "2001",
                    "id": "juhe-id-room-3",
                    "at_list": ["bot"],
                },
            }
        )

        assert success is True
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "ping juhe with mention"
        assert event.source.chat_id == "R:2001"
        assert event.source.chat_type == "group"
        assert getattr(event.source, "shared_session", False) is True
        assert "Earlier customer context" in getattr(event, "_juhe_pending_context_text", "")
        assert "ping juhe without mention" in getattr(event, "_juhe_pending_context_text", "")

        room_log = SessionDB(db_path=tmp_path / "state.db")
        try:
            history = room_log.get_room_history("juhe", "R:2001", limit=10)
        finally:
            room_log.close()

        assert [item["message_id"] for item in history] == ["juhe-id-room-1", "juhe-id-room-2", "juhe-id-room-3"]
        assert history[0]["consumed_for_context"] is True
        assert history[1]["triggered"] is False
        assert history[1]["consumed_for_context"] is True
        assert history[2]["triggered"] is True
        assert history[2]["consumed_for_context"] is True

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
    def test_format_message_strips_visible_markdown(self):
        adapter = _make_adapter()

        formatted = adapter.format_message(
            "这个是**优化md**\n\n1. **购买项目**：`云服务费`\n2. [查看详情](https://example.com)"
        )

        assert formatted == (
            "这个是优化md\n\n1. 购买项目：云服务费\n2. 查看详情"
        )

    def test_format_message_preserves_media_directives(self):
        adapter = _make_adapter()

        formatted = adapter.format_message(
            "先看**说明**\nMEDIA:/tmp/test invoice.pdf\n再看[链接](https://example.com)"
        )

        assert formatted == (
            "先看说明\nMEDIA:/tmp/test invoice.pdf\n再看链接"
        )

    def test_format_message_strips_certificate_skill_instruction_leakage(self):
        adapter = _make_adapter()

        formatted = adapter.format_message(
            "确认后我直接出执行草案。\n"
            " artifact, or equivalent non-link deliverable.\\n"
            "- If any business meaning is uncertain, stop and ask directly. "
            "Do not guess templates, certificate names, professional terms, "
            "logo choices, or multi-date meanings.\\n\\nPrefer"
        )

        assert formatted == "确认后我直接出执行草案。"

    def test_format_message_strips_local_delivery_path_from_visible_text(self):
        adapter = _make_adapter()

        formatted = adapter.format_message(
            "压缩包已生成。\n本地路径：/tmp/certificate-delivery/final.zip\n请查收。"
        )

        assert "压缩包已生成。" in formatted
        assert "请查收。" in formatted
        assert "/tmp/certificate-delivery/final.zip" not in formatted
        assert "本地路径" not in formatted

    @pytest.mark.asyncio
    async def test_on_processing_complete_sanitizes_room_memory_assistant_response(self, monkeypatch):
        from gateway.platforms.juhe import ProcessingOutcome

        adapter = _make_adapter()
        captured = {}

        def _fake_schedule_room_memory_update(_room_id, **kwargs):
            captured["room_id"] = _room_id
            captured.update(kwargs)

        monkeypatch.setattr(
            "gateway.platforms.juhe.schedule_room_memory_update",
            _fake_schedule_room_memory_update,
        )

        event = SimpleNamespace(
            source=SimpleNamespace(chat_type="group", chat_id="R:2001"),
            text="@魔法老头 出证书",
            _final_response_text=(
                "确认后我直接出执行草案。\n"
                " artifact, or equivalent non-link deliverable.\\n"
                "- If any business meaning is uncertain, stop and ask directly. "
                "Do not guess templates, certificate names, professional terms, "
                "logo choices, or multi-date meanings.\\n\\nPrefer"
            ),
            _juhe_pending_context_text="",
            _juhe_relevant_room_history_text="",
        )

        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

        assert captured["assistant_response"] == "确认后我直接出执行草案。"

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
    async def test_send_text_formats_plain_text_for_juhe(self):
        adapter = _make_adapter()
        adapter._guid_request = AsyncMock(return_value={"err_code": 0, "data": {"ret": 0}})

        result = await adapter.send(
            "S:1001",
            "这个是**优化md**这张电子发票是**腾讯云**开具给[广东第二师范学院](https://example.com)的。",
        )

        assert result.success is True
        payload = adapter._guid_request.await_args.kwargs
        assert payload["data"]["content"] == (
            "这个是优化md这张电子发票是腾讯云开具给广东第二师范学院的。"
        )

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
    async def test_send_document_materializes_http_url_before_upload(self, monkeypatch):
        adapter = _make_adapter()
        cleanup = AsyncMock()
        monkeypatch.setattr(
            adapter,
            "_materialize_remote_document_for_upload",
            AsyncMock(return_value=("/tmp/materialized-report.pdf", cleanup)),
        )
        monkeypatch.setattr(
            adapter,
            "_stage_local_file_for_upload",
            AsyncMock(return_value=("https://temp.example.com/report.pdf?sig=1", AsyncMock())),
        )
        monkeypatch.setattr(
            adapter,
            "_upload_file_from_url",
            AsyncMock(return_value={"error_code": 0, "data": {"message_id": "msg-file-1"}}),
        )

        result = await adapter.send_document("S:1001", "https://files.example.com/report.pdf")

        assert result.success is True
        assert result.message_id == "msg-file-1"
        adapter._materialize_remote_document_for_upload.assert_awaited_once_with(
            "https://files.example.com/report.pdf",
            "report.pdf",
        )
        adapter._stage_local_file_for_upload.assert_awaited_once_with("/tmp/materialized-report.pdf")
        cleanup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_document_materializes_signed_http_url_before_upload(self, monkeypatch):
        adapter = _make_adapter()
        cleanup = AsyncMock()
        monkeypatch.setattr(
            adapter,
            "_materialize_remote_document_for_upload",
            AsyncMock(return_value=("/tmp/materialized-final.zip", cleanup)),
        )
        monkeypatch.setattr(
            adapter,
            "_stage_local_file_for_upload",
            AsyncMock(return_value=("https://temp.example.com/final.zip?sig=1", AsyncMock())),
        )
        monkeypatch.setattr(
            adapter,
            "_upload_file_from_url",
            AsyncMock(return_value={"error_code": 0, "data": {"message_id": "msg-signed-1"}}),
        )

        result = await adapter.send_document(
            "S:1001",
            "https://minio.example.com/exports/final.zip?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=secret",
        )

        assert result.success is True
        assert result.message_id == "msg-signed-1"
        adapter._materialize_remote_document_for_upload.assert_awaited_once_with(
            "https://minio.example.com/exports/final.zip?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=secret",
            "final.zip",
        )
        adapter._stage_local_file_for_upload.assert_awaited_once_with("/tmp/materialized-final.zip")
        cleanup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_document_materializes_remote_document_before_upload(self, monkeypatch):
        adapter = _make_adapter()
        cleanup = AsyncMock()
        monkeypatch.setattr(
            adapter,
            "_materialize_remote_document_for_upload",
            AsyncMock(return_value=("/tmp/materialized-report.pdf", cleanup)),
        )
        monkeypatch.setattr(
            adapter,
            "_stage_local_file_for_upload",
            AsyncMock(return_value=("https://temp.example.com/report.pdf?sig=1", AsyncMock())),
        )
        monkeypatch.setattr(
            adapter,
            "_upload_file_from_url",
            AsyncMock(return_value={"error_code": 0, "data": {"message_id": "msg-materialized-1"}}),
        )

        result = await adapter.send_document("S:1001", "https://files.example.com/report.pdf")

        assert result.success is True
        adapter._materialize_remote_document_for_upload.assert_awaited_once_with(
            "https://files.example.com/report.pdf",
            "report.pdf",
        )
        adapter._stage_local_file_for_upload.assert_awaited_once_with("/tmp/materialized-report.pdf")
        cleanup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_document_materializes_certificate_render_job_remote_url(self, monkeypatch):
        adapter = _make_adapter()
        materialized_cleanup = AsyncMock()
        staged_cleanup = AsyncMock()
        monkeypatch.setattr(
            adapter,
            "_materialize_remote_document_for_upload",
            AsyncMock(return_value=("/tmp/materialized-certificate.zip", materialized_cleanup)),
        )
        monkeypatch.setattr(
            adapter,
            "_stage_local_file_for_upload",
            AsyncMock(return_value=("https://temp.example.com/materialized-certificate.zip?sig=1", staged_cleanup)),
        )
        monkeypatch.setattr(
            adapter,
            "_upload_file_from_url",
            AsyncMock(return_value={"error_code": 0, "data": {"message_id": "msg-cert-url-1"}}),
        )

        result = await adapter.send_document(
            "S:1001",
            "http://49.233.103.196:9000/certificate-dev/certificate_render_jobs/2026/04/22/job-92/final.zip"
            "?AWSAccessKeyId=test&Signature=abc&Expires=123",
        )

        assert result.success is True
        adapter._materialize_remote_document_for_upload.assert_awaited_once_with(
            "http://49.233.103.196:9000/certificate-dev/certificate_render_jobs/2026/04/22/job-92/final.zip"
            "?AWSAccessKeyId=test&Signature=abc&Expires=123",
            "final.zip",
        )
        adapter._stage_local_file_for_upload.assert_awaited_once_with("/tmp/materialized-certificate.zip")
        materialized_cleanup.assert_awaited_once()
        staged_cleanup.assert_awaited_once()

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
    async def test_upload_file_from_url_retries_big_upload_after_small_timeout(self, monkeypatch):
        import gateway.platforms.juhe as juhe_module

        adapter = _make_adapter()
        small_upload = AsyncMock(side_effect=RuntimeError("HTTP error: (ReadTimeout)"))
        big_upload = AsyncMock(return_value={"error_code": 0, "data": {"message_id": "msg-big-fallback"}})
        monkeypatch.setattr(juhe_module, "send_small_file_from_url", small_upload)
        monkeypatch.setattr(juhe_module, "send_big_file_from_url", big_upload)
        monkeypatch.setattr(adapter, "_get_remote_file_size_hint", AsyncMock(return_value=1024))

        result = await adapter._upload_file_from_url(
            client=adapter._get_sdk_client(),
            conversation_id="S:1001",
            file_url="https://temp.example.com/report.zip?sig=1",
            file_name="report.zip",
            file_type=5,
        )

        assert result["data"]["message_id"] == "msg-big-fallback"
        small_upload.assert_awaited_once()
        big_upload.assert_awaited_once()

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
        assert "MEDIA:https://..." not in PLATFORM_HINTS["juhe"]
        assert "certificate_workflow_tool" in PLATFORM_HINTS["juhe"]
        assert "materialize_render_job_artifact" in PLATFORM_HINTS["juhe"]
        assert "hidden artifact channel" in PLATFORM_HINTS["juhe"]
        assert "MEDIA:/absolute/path" not in PLATFORM_HINTS["juhe"]
        assert "Do not write local filesystem paths" in PLATFORM_HINTS["juhe"]


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
    async def test_send_juhe_sends_document_before_text_for_document_media(self):
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
                "证书压缩包已就绪：\nreport.zip",
                media_files=[("https://files.example.com/report.zip", False)],
            )

        assert result == {
            "success": True,
            "platform": "juhe",
            "chat_id": "S:1001",
            "message_id": "file-456",
        }
        assert adapter.mock_calls.index(call.send_document("S:1001", "https://files.example.com/report.zip")) < (
            adapter.mock_calls.index(call.send("S:1001", "证书压缩包已就绪：\nreport.zip"))
        )

    @pytest.mark.asyncio
    async def test_send_juhe_suppresses_success_text_when_document_delivery_fails(self):
        from tools.send_message_tool import _send_juhe

        adapter = MagicMock()
        adapter.connect = AsyncMock(return_value=True)
        adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="text-123", error=None))
        adapter.send_document = AsyncMock(
            return_value=SimpleNamespace(success=False, message_id="", error="ReadTimeout")
        )
        adapter.disconnect = AsyncMock()

        with patch("gateway.platforms.juhe.check_juhe_requirements", return_value=True), \
             patch("tools.send_message_tool.JuheAdapter", return_value=adapter):
            result = await _send_juhe(
                {"app_key": "app", "app_secret": "secret", "guid": "guid"},
                "S:1001",
                "证书压缩包已就绪：\nreport.zip",
                media_files=[("https://files.example.com/report.zip", False)],
            )

        assert "error" in result
        assert "ReadTimeout" in result["error"]
        adapter.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_juhe_forwards_certificate_render_job_remote_media(self):
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
                "",
                media_files=[
                    (
                        "http://49.233.103.196:9000/certificate-dev/certificate_render_jobs/2026/04/22/job-92/final.zip"
                        "?AWSAccessKeyId=test&Signature=abc&Expires=123",
                        False,
                    )
                ],
            )

        assert result["success"] is True
        adapter.send_document.assert_awaited_once()
        adapter.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_juhe_forwards_certificate_render_job_url_text_to_adapter(self):
        from tools.send_message_tool import _send_juhe

        adapter = MagicMock()
        adapter.connect = AsyncMock(return_value=True)
        adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="text-222", error=None))
        adapter.disconnect = AsyncMock()

        with patch("gateway.platforms.juhe.check_juhe_requirements", return_value=True), \
             patch("tools.send_message_tool.JuheAdapter", return_value=adapter):
            result = await _send_juhe(
                {"app_key": "app", "app_secret": "secret", "guid": "guid"},
                "S:1001",
                (
                    "压缩包已生成。\n"
                    "下载链接："
                    "http://49.233.103.196:9000/certificate-dev/certificate_render_jobs/"
                    "2026/04/22/job-92/final.zip?AWSAccessKeyId=test&Signature=abc&Expires=123"
                ),
                media_files=[],
            )

        assert result["success"] is True
        adapter.send.assert_awaited_once()
        sent_text = adapter.send.await_args.args[1]
        assert "certificate_render_jobs" in sent_text
        assert "下载链接" in sent_text

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
