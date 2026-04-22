#!/usr/bin/env python3
"""Safe wrapper around the certificate workflow CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from tools.registry import registry, tool_error, tool_result


DEFAULT_WRAPPER_PATH = Path(
    "/Users/chou/code/certificate/tools/certificate-workflow-cli/certificate-workflow-wrapper.js"
)
WRAPPER_PATH_ENV = "CERTIFICATE_WORKFLOW_WRAPPER_PATH"
DEFAULT_PREVIEW_TIMEOUT_SECONDS = 120
DEFAULT_EXECUTE_TIMEOUT_SECONDS = 900

COMMAND_MAP = {
    "ensure_archive_company": "ensure-archive-company",
    "deliver_system_certificate": "deliver-system-certificate",
    "deliver_honor_certificate": "deliver-honor-certificate",
    "ensure-archive-company": "ensure-archive-company",
    "deliver-system-certificate": "deliver-system-certificate",
    "deliver-honor-certificate": "deliver-honor-certificate",
}


CERTIFICATE_WORKFLOW_TOOL_SCHEMA = {
    "name": "certificate_workflow_tool",
    "description": (
        "Run the certificate admin workflow wrapper through a safe argv-based "
        "subprocess call. Use this for the standard certificate workflow path "
        "instead of terminal shell commands when you need to ensure an archive "
        "company or deliver system/honor certificates. The tool returns the "
        "wrapper's structured JSON result directly, including wrapper-provided "
        "delivery artifact metadata."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": [
                    "ensure_archive_company",
                    "deliver_system_certificate",
                    "deliver_honor_certificate",
                ],
                "description": "High-level certificate workflow command to run.",
            },
            "input": {
                "type": "object",
                "description": (
                    "Structured wrapper input object. This is passed to the wrapper "
                    "through --raw JSON."
                ),
            },
            "execute": {
                "type": "boolean",
                "description": (
                    "When true, run the write path. When false or omitted, run the "
                    "preview/draft path."
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Optional subprocess timeout override. Defaults to 120 seconds "
                    "for preview mode and 900 seconds for execute mode."
                ),
            },
            "download_dir": {
                "type": "string",
                "description": (
                    "Optional download directory to pass through to the wrapper."
                ),
            },
        },
        "required": ["command"],
    },
}


def _get_wrapper_path() -> Path:
    raw = str(os.getenv(WRAPPER_PATH_ENV, "") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_WRAPPER_PATH


def _get_node_path() -> str | None:
    return shutil.which("node")


def _check_certificate_workflow_tool() -> bool:
    return _get_wrapper_path().exists() and bool(_get_node_path())


def _resolve_command(command: Any) -> str | None:
    text = str(command or "").strip()
    return COMMAND_MAP.get(text)


def _normalize_timeout(execute: bool, timeout_seconds: Any) -> int:
    if timeout_seconds is None:
        return DEFAULT_EXECUTE_TIMEOUT_SECONDS if execute else DEFAULT_PREVIEW_TIMEOUT_SECONDS
    try:
        timeout = int(timeout_seconds)
    except (TypeError, ValueError):
        raise ValueError("timeout_seconds must be an integer")
    if timeout <= 0:
        raise ValueError("timeout_seconds must be greater than 0")
    return timeout


def _build_argv(
    *,
    node_path: str,
    wrapper_path: Path,
    wrapper_command: str,
    input_payload: dict[str, Any],
    execute: bool,
    download_dir: str | None,
) -> list[str]:
    argv = [
        node_path,
        str(wrapper_path),
        wrapper_command,
        "--raw",
        json.dumps(input_payload, ensure_ascii=False),
    ]
    if execute:
        argv.append("--execute")
    if download_dir:
        argv.extend(["--download-dir", download_dir])
    argv.extend(["-o", "json"])
    return argv


def _decode_process_error(command: str, completed: subprocess.CompletedProcess[str]) -> str:
    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    if stderr:
        return stderr
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return stdout
        if isinstance(payload, dict) and payload.get("error"):
            return str(payload["error"])
        return stdout
    return f"Certificate workflow command '{command}' failed with exit code {completed.returncode}."


def certificate_workflow_tool(args: dict[str, Any], **_kw: Any) -> str:
    """Safely call the certificate workflow wrapper without a shell."""
    if not isinstance(args, dict):
        return tool_error("Arguments must be a JSON object.")

    wrapper_command = _resolve_command(args.get("command"))
    if not wrapper_command:
        valid_commands = ", ".join(sorted({name for name in COMMAND_MAP if "_" in name}))
        return tool_error(
            f"Unknown certificate workflow command: {args.get('command')!r}. "
            f"Use one of: {valid_commands}."
        )

    input_payload = args.get("input", {})
    if input_payload is None:
        input_payload = {}
    if not isinstance(input_payload, dict):
        return tool_error("'input' must be a JSON object.")

    execute = bool(args.get("execute"))
    download_dir_raw = args.get("download_dir")
    download_dir = str(download_dir_raw).strip() if download_dir_raw is not None else None
    if download_dir == "":
        download_dir = None

    wrapper_path = _get_wrapper_path()
    if not wrapper_path.exists():
        return tool_error(f"Certificate workflow wrapper not found: {wrapper_path}")

    node_path = _get_node_path()
    if not node_path:
        return tool_error("Node.js executable not found in PATH.")

    try:
        timeout_seconds = _normalize_timeout(execute, args.get("timeout_seconds"))
    except ValueError as exc:
        return tool_error(str(exc))

    argv = _build_argv(
        node_path=node_path,
        wrapper_path=wrapper_path,
        wrapper_command=wrapper_command,
        input_payload=input_payload,
        execute=execute,
        download_dir=download_dir,
    )

    try:
        completed = subprocess.run(
            argv,
            cwd=str(wrapper_path.parent),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            shell=False,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return tool_error(
            f"Certificate workflow command '{wrapper_command}' timed out after {timeout_seconds} seconds."
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        return tool_error(f"Certificate workflow command failed to start: {exc}")

    if completed.returncode != 0:
        return tool_error(_decode_process_error(wrapper_command, completed))

    stdout = (completed.stdout or "").strip()
    if not stdout:
        return tool_error(
            f"Certificate workflow command '{wrapper_command}' returned no JSON output."
        )

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return tool_error(
            f"Certificate workflow command '{wrapper_command}' returned invalid JSON: {exc}"
        )

    return tool_result(payload)


registry.register(
    name="certificate_workflow_tool",
    toolset="certificate",
    schema=CERTIFICATE_WORKFLOW_TOOL_SCHEMA,
    handler=certificate_workflow_tool,
    check_fn=_check_certificate_workflow_tool,
    emoji="📜",
    max_result_size_chars=float("inf"),
)
