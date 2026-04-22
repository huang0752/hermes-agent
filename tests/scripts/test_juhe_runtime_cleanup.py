"""Tests for the Juhe runtime cleanup utility."""

from __future__ import annotations

import importlib
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path


def _load_module():
    sys.modules.pop("scripts.juhe_runtime_cleanup", None)
    return importlib.import_module("scripts.juhe_runtime_cleanup")


def _write_file(path: Path, content: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _seed_runtime_tree(hermes_home: Path, temp_root: Path) -> None:
    _write_file(hermes_home / "state.db", "sqlite")
    _write_file(hermes_home / "sessions" / "session_a.json", "{}")
    _write_file(hermes_home / "juhe" / "cache.json", "{}")
    _write_file(hermes_home / "memories" / "juhe" / "room.md", "memory")
    _write_file(hermes_home / "tmp" / "juhe-current-attachments" / "file.txt", "attachment")
    _write_file(temp_root / "hermes-results" / "tool.json", "{}")
    _write_file(temp_root / "certificate-render-artifacts" / "artifact.zip", "zip")


def test_build_runtime_cleanup_plan_collects_existing_targets(tmp_path):
    module = _load_module()
    hermes_home = tmp_path / ".hermes"
    temp_root = tmp_path / "tmp-root"
    _seed_runtime_tree(hermes_home, temp_root)

    plan = module.build_runtime_cleanup_plan(
        hermes_home=hermes_home,
        temp_root=temp_root,
        timestamp="20260422-190000",
    )

    assert plan["backup_dir"] == hermes_home / "backups" / "juhe-runtime-cleanup-20260422-190000"
    existing_labels = [item["label"] for item in plan["operations"] if item["exists"]]
    assert existing_labels == [
        "state_db",
        "sessions",
        "juhe_cache",
        "juhe_memory",
        "juhe_current_attachments",
        "tmp_hermes_results",
        "tmp_certificate_render_artifacts",
    ]


def test_execute_runtime_cleanup_plan_moves_targets_and_recreates_live_dirs(tmp_path):
    module = _load_module()
    hermes_home = tmp_path / ".hermes"
    temp_root = tmp_path / "tmp-root"
    _seed_runtime_tree(hermes_home, temp_root)

    plan = module.build_runtime_cleanup_plan(
        hermes_home=hermes_home,
        temp_root=temp_root,
        timestamp="20260422-190500",
    )
    module.execute_runtime_cleanup_plan(plan)

    backup_dir = plan["backup_dir"]
    assert (backup_dir / "state.db").exists()
    assert (backup_dir / "sessions" / "session_a.json").exists()
    assert (backup_dir / "juhe" / "cache.json").exists()
    assert (backup_dir / "memories" / "juhe" / "room.md").exists()
    assert (backup_dir / "tmp" / "juhe-current-attachments" / "file.txt").exists()
    assert (backup_dir / "tmp-hermes-results" / "tool.json").exists()
    assert (backup_dir / "tmp-certificate-render-artifacts" / "artifact.zip").exists()

    assert not (hermes_home / "state.db").exists()
    assert (hermes_home / "sessions").exists()
    assert list((hermes_home / "sessions").iterdir()) == []
    assert not (hermes_home / "juhe").exists()
    assert not (hermes_home / "memories" / "juhe").exists()
    assert not (hermes_home / "tmp" / "juhe-current-attachments").exists()
    assert not (temp_root / "hermes-results").exists()
    assert not (temp_root / "certificate-render-artifacts").exists()


def test_main_dry_run_does_not_modify_runtime_tree(tmp_path, monkeypatch):
    module = _load_module()
    hermes_home = tmp_path / ".hermes"
    temp_root = tmp_path / "tmp-root"
    _seed_runtime_tree(hermes_home, temp_root)
    monkeypatch.setattr(module, "DEFAULT_TEMP_ROOT", temp_root)

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = module.main(
            [
                "--hermes-home",
                str(hermes_home),
                "--timestamp",
                "20260422-191000",
                "--dry-run",
            ]
        )

    output = stdout.getvalue()
    assert exit_code == 0
    assert "DRY RUN" in output
    assert "juhe-runtime-cleanup-20260422-191000" in output
    assert (hermes_home / "state.db").exists()
    assert (hermes_home / "sessions" / "session_a.json").exists()
    assert (temp_root / "hermes-results" / "tool.json").exists()
