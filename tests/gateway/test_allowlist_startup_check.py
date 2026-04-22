"""Tests for the startup allowlist warning check in gateway/run.py."""

import os
import json
from unittest.mock import patch

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import _should_warn_about_missing_user_allowlists


class TestAllowlistStartupCheck:

    def test_no_config_emits_warning(self):
        with patch.dict(os.environ, {}, clear=True):
            assert _should_warn_about_missing_user_allowlists(GatewayConfig()) is True

    def test_signal_group_allowed_users_suppresses_warning(self):
        with patch.dict(os.environ, {"SIGNAL_GROUP_ALLOWED_USERS": "user1"}, clear=True):
            assert _should_warn_about_missing_user_allowlists(GatewayConfig()) is False

    def test_juhe_env_allowlist_suppresses_warning(self):
        with patch.dict(os.environ, {"JUHE_ALLOWED_USERS": "S:1001"}, clear=True):
            assert _should_warn_about_missing_user_allowlists(GatewayConfig()) is False

    def test_juhe_config_group_allowlist_suppresses_warning(self):
        config = GatewayConfig(
            platforms={
                Platform.JUHE: PlatformConfig(
                    enabled=True,
                    extra={"group_allow_from": ["R:2001"]},
                )
            }
        )
        with patch.dict(os.environ, {}, clear=True):
            assert _should_warn_about_missing_user_allowlists(config) is False

    def test_juhe_dynamic_access_control_file_suppresses_warning(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "juhe").mkdir(parents=True, exist_ok=True)
        (tmp_path / "juhe" / "access_control.json").write_text(
            json.dumps({"dm_allow_from": ["S:1001"], "group_trigger_user_ids": []}),
            encoding="utf-8",
        )
        config = GatewayConfig(
            platforms={
                Platform.JUHE: PlatformConfig(
                    enabled=True,
                    extra={},
                )
            }
        )

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}, clear=True):
            assert _should_warn_about_missing_user_allowlists(config) is False

    def test_telegram_allow_all_users_suppresses_warning(self):
        with patch.dict(os.environ, {"TELEGRAM_ALLOW_ALL_USERS": "true"}, clear=True):
            assert _should_warn_about_missing_user_allowlists(GatewayConfig()) is False

    def test_gateway_allow_all_users_suppresses_warning(self):
        with patch.dict(os.environ, {"GATEWAY_ALLOW_ALL_USERS": "yes"}, clear=True):
            assert _should_warn_about_missing_user_allowlists(GatewayConfig()) is False
