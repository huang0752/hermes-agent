"""Tests for Juhe dynamic access control runtime store."""

import json
from pathlib import Path

from gateway.juhe_access_control import JuheAccessControlStore


class TestJuheAccessControlStore:
    def test_falls_back_to_static_lists_when_file_missing(self, tmp_path):
        store = JuheAccessControlStore(
            base_dir=tmp_path / "juhe",
            fallback_dm_allow_from=["S:1001"],
            fallback_group_trigger_user_ids=["S:2001"],
        )

        assert store.get_dm_allow_from() == ["S:1001"]
        assert store.get_group_trigger_user_ids() == ["S:2001"]

    def test_uses_valid_file_contents_and_reloads_after_change(self, tmp_path):
        base_dir = tmp_path / "juhe"
        base_dir.mkdir(parents=True, exist_ok=True)
        access_path = base_dir / "access_control.json"
        access_path.write_text(
            json.dumps(
                {
                    "dm_allow_from": ["S:1002"],
                    "group_trigger_user_ids": ["S:2002"],
                }
            ),
            encoding="utf-8",
        )

        store = JuheAccessControlStore(
            base_dir=base_dir,
            fallback_dm_allow_from=["S:1001"],
            fallback_group_trigger_user_ids=["S:2001"],
        )

        assert store.get_dm_allow_from() == ["S:1002"]
        assert store.get_group_trigger_user_ids() == ["S:2002"]

        access_path.write_text(
            json.dumps(
                {
                    "dm_allow_from": ["S:1003", "S:1004"],
                    "group_trigger_user_ids": ["S:2003"],
                }
            ),
            encoding="utf-8",
        )

        assert store.get_dm_allow_from() == ["S:1003", "S:1004"]
        assert store.get_group_trigger_user_ids() == ["S:2003"]

    def test_keeps_last_known_good_when_file_becomes_invalid(self, tmp_path):
        base_dir = tmp_path / "juhe"
        base_dir.mkdir(parents=True, exist_ok=True)
        access_path = base_dir / "access_control.json"
        access_path.write_text(
            json.dumps(
                {
                    "dm_allow_from": ["S:1002"],
                    "group_trigger_user_ids": ["S:2002"],
                }
            ),
            encoding="utf-8",
        )

        store = JuheAccessControlStore(
            base_dir=base_dir,
            fallback_dm_allow_from=["S:1001"],
            fallback_group_trigger_user_ids=["S:2001"],
        )

        assert store.get_dm_allow_from() == ["S:1002"]
        assert store.get_group_trigger_user_ids() == ["S:2002"]

        access_path.write_text("{not-valid-json", encoding="utf-8")

        assert store.get_dm_allow_from() == ["S:1002"]
        assert store.get_group_trigger_user_ids() == ["S:2002"]

    def test_invalid_file_without_last_known_good_falls_back_to_static_lists(self, tmp_path):
        base_dir = tmp_path / "juhe"
        base_dir.mkdir(parents=True, exist_ok=True)
        access_path = base_dir / "access_control.json"
        access_path.write_text("{not-valid-json", encoding="utf-8")

        store = JuheAccessControlStore(
            base_dir=base_dir,
            fallback_dm_allow_from=["S:1001"],
            fallback_group_trigger_user_ids=["S:2001"],
        )

        assert store.get_dm_allow_from() == ["S:1001"]
        assert store.get_group_trigger_user_ids() == ["S:2001"]

    def test_reuses_cached_values_when_file_is_unchanged(self, tmp_path, monkeypatch):
        base_dir = tmp_path / "juhe"
        base_dir.mkdir(parents=True, exist_ok=True)
        access_path = base_dir / "access_control.json"
        access_path.write_text(
            json.dumps(
                {
                    "dm_allow_from": ["S:1002"],
                    "group_trigger_user_ids": ["S:2002"],
                }
            ),
            encoding="utf-8",
        )

        read_calls = {"count": 0}
        original_read_text = Path.read_text

        def counted_read_text(self, *args, **kwargs):
            if self == access_path:
                read_calls["count"] += 1
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", counted_read_text)

        store = JuheAccessControlStore(base_dir=base_dir)

        assert store.get_dm_allow_from() == ["S:1002"]
        assert store.get_dm_allow_from() == ["S:1002"]
        assert store.get_group_trigger_user_ids() == ["S:2002"]
        assert read_calls["count"] == 1
