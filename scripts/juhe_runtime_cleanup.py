#!/usr/bin/env python3
"""Backup and clean Juhe runtime state on explicit operator request."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from hermes_constants import get_hermes_home


DEFAULT_TEMP_ROOT = Path(tempfile.gettempdir())


def _build_operation(
    *,
    label: str,
    source: Path,
    backup_target: Path,
    recreate_dir: Path | None = None,
) -> dict[str, Any]:
    return {
        "label": label,
        "source": source,
        "backup_target": backup_target,
        "exists": source.exists(),
        "recreate_dir": recreate_dir,
    }


def build_runtime_cleanup_plan(
    *,
    hermes_home: Path,
    temp_root: Path,
    timestamp: str | None = None,
) -> dict[str, Any]:
    normalized_home = Path(hermes_home).expanduser().resolve()
    normalized_temp_root = Path(temp_root).expanduser().resolve()
    normalized_timestamp = str(timestamp or datetime.now().strftime("%Y%m%d-%H%M%S"))
    backup_dir = normalized_home / "backups" / f"juhe-runtime-cleanup-{normalized_timestamp}"

    operations = [
        _build_operation(
            label="state_db",
            source=normalized_home / "state.db",
            backup_target=backup_dir / "state.db",
        ),
        _build_operation(
            label="sessions",
            source=normalized_home / "sessions",
            backup_target=backup_dir / "sessions",
            recreate_dir=normalized_home / "sessions",
        ),
        _build_operation(
            label="juhe_cache",
            source=normalized_home / "juhe",
            backup_target=backup_dir / "juhe",
        ),
        _build_operation(
            label="juhe_memory",
            source=normalized_home / "memories" / "juhe",
            backup_target=backup_dir / "memories" / "juhe",
        ),
        _build_operation(
            label="juhe_current_attachments",
            source=normalized_home / "tmp" / "juhe-current-attachments",
            backup_target=backup_dir / "tmp" / "juhe-current-attachments",
        ),
        _build_operation(
            label="tmp_hermes_results",
            source=normalized_temp_root / "hermes-results",
            backup_target=backup_dir / "tmp-hermes-results",
        ),
        _build_operation(
            label="tmp_certificate_render_artifacts",
            source=normalized_temp_root / "certificate-render-artifacts",
            backup_target=backup_dir / "tmp-certificate-render-artifacts",
        ),
    ]

    return {
        "hermes_home": normalized_home,
        "temp_root": normalized_temp_root,
        "timestamp": normalized_timestamp,
        "backup_dir": backup_dir,
        "operations": operations,
    }


def execute_runtime_cleanup_plan(plan: dict[str, Any]) -> Path:
    backup_dir = Path(plan["backup_dir"])
    backup_dir.mkdir(parents=True, exist_ok=True)

    for operation in plan.get("operations", []):
        source = Path(operation["source"])
        target = Path(operation["backup_target"])
        if operation.get("exists"):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))

        recreate_dir = operation.get("recreate_dir")
        if recreate_dir:
            Path(recreate_dir).mkdir(parents=True, exist_ok=True)

    return backup_dir


def _format_plan(plan: dict[str, Any], *, execute: bool) -> str:
    mode = "EXECUTE" if execute else "DRY RUN"
    lines = [
        f"{mode}: Juhe runtime cleanup",
        f"Hermes home: {plan['hermes_home']}",
        f"Temp root: {plan['temp_root']}",
        f"Backup dir: {plan['backup_dir']}",
        "Operations:",
    ]
    for operation in plan.get("operations", []):
        status = "move" if operation.get("exists") else "skip"
        line = f"- {operation['label']}: {status} {operation['source']} -> {operation['backup_target']}"
        recreate_dir = operation.get("recreate_dir")
        if recreate_dir:
            line += f" (recreate {recreate_dir})"
        lines.append(line)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backup and clean Juhe runtime state.")
    parser.add_argument(
        "--hermes-home",
        default=str(get_hermes_home()),
        help="Hermes home directory to clean (default: current HERMES_HOME).",
    )
    parser.add_argument(
        "--temp-root",
        default=str(DEFAULT_TEMP_ROOT),
        help="Temp root containing hermes-results and certificate-render-artifacts.",
    )
    parser.add_argument(
        "--timestamp",
        default=None,
        help="Explicit timestamp suffix for the backup directory.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the cleanup plan without changing the filesystem.",
    )
    mode_group.add_argument(
        "--execute",
        action="store_true",
        help="Execute the cleanup plan after printing it.",
    )

    args = parser.parse_args(argv)
    execute = bool(args.execute)

    plan = build_runtime_cleanup_plan(
        hermes_home=Path(args.hermes_home),
        temp_root=Path(args.temp_root),
        timestamp=args.timestamp,
    )
    print(_format_plan(plan, execute=execute))

    if not execute:
        return 0

    backup_dir = execute_runtime_cleanup_plan(plan)
    print(f"Cleanup complete. Backup stored at {backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
