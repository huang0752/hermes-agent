"""Tests for the certificate workflow tool wrapper."""

import json
import subprocess
from pathlib import Path


def _completed(argv, stdout):
    return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def test_certificate_workflow_tool_executes_wrapper_with_argv(monkeypatch, tmp_path):
    import tools.certificate_workflow_tool as mod

    wrapper = tmp_path / "certificate-workflow-wrapper.js"
    wrapper.write_text("// test wrapper\n", encoding="utf-8")
    monkeypatch.setenv("CERTIFICATE_WORKFLOW_WRAPPER_PATH", str(wrapper))
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/node" if name == "node" else None)

    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _completed(
            argv,
            json.dumps(
                {
                    "mode": "execute",
                    "delivery": {"media_tag": "MEDIA:/tmp/certificate.zip"},
                },
                ensure_ascii=False,
            ),
        )

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    result = json.loads(
        mod.certificate_workflow_tool(
            {
                "command": "deliver_system_certificate",
                "input": {
                    "enterpriseNameCn": "安阳宝华冶金耐材有限公司",
                    "partnerCompanyId": 6,
                    "certificateTemplateId": 314,
                    "issuedDate": "2026-01-25",
                },
                "execute": True,
            }
        )
    )

    assert result["mode"] == "execute"
    assert result["delivery"]["media_tag"] == "MEDIA:/tmp/certificate.zip"
    assert captured["argv"] == [
        "/usr/bin/node",
        str(wrapper),
        "deliver-system-certificate",
        "--raw",
        json.dumps(
            {
                "enterpriseNameCn": "安阳宝华冶金耐材有限公司",
                "partnerCompanyId": 6,
                "certificateTemplateId": 314,
                "issuedDate": "2026-01-25",
            },
            ensure_ascii=False,
        ),
        "--execute",
        "-o",
        "json",
    ]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == 900


def test_certificate_workflow_tool_uses_shorter_timeout_for_preview(monkeypatch, tmp_path):
    import tools.certificate_workflow_tool as mod

    wrapper = tmp_path / "certificate-workflow-wrapper.js"
    wrapper.write_text("// test wrapper\n", encoding="utf-8")
    monkeypatch.setenv("CERTIFICATE_WORKFLOW_WRAPPER_PATH", str(wrapper))
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/node" if name == "node" else None)

    seen = {}

    def fake_run(argv, **kwargs):
        seen["timeout"] = kwargs["timeout"]
        return _completed(argv, json.dumps({"mode": "preview"}, ensure_ascii=False))

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    result = json.loads(
        mod.certificate_workflow_tool(
            {
                "command": "ensure_archive_company",
                "input": {"enterpriseNameCn": "测试公司"},
            }
        )
    )

    assert result["mode"] == "preview"
    assert seen["timeout"] == 120


def test_certificate_workflow_tool_returns_error_for_invalid_command():
    import tools.certificate_workflow_tool as mod

    result = json.loads(
        mod.certificate_workflow_tool(
            {
                "command": "bad_command",
                "input": {},
            }
        )
    )

    assert "error" in result
    assert "Unknown certificate workflow command" in result["error"]


def test_certificate_workflow_tool_passes_download_dir_flag(monkeypatch, tmp_path):
    import tools.certificate_workflow_tool as mod

    wrapper = tmp_path / "certificate-workflow-wrapper.js"
    wrapper.write_text("// test wrapper\n", encoding="utf-8")
    monkeypatch.setenv("CERTIFICATE_WORKFLOW_WRAPPER_PATH", str(wrapper))
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/node" if name == "node" else None)

    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _completed(argv, json.dumps({"mode": "execute"}, ensure_ascii=False))

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    download_dir = Path("/tmp/certificate-downloads")
    json.loads(
        mod.certificate_workflow_tool(
            {
                "command": "deliver_honor_certificate",
                "input": {"enterpriseNameCn": "测试公司"},
                "execute": True,
                "download_dir": str(download_dir),
                "timeout_seconds": 321,
            }
        )
    )

    assert captured["argv"] == [
        "/usr/bin/node",
        str(wrapper),
        "deliver-honor-certificate",
        "--raw",
        json.dumps({"enterpriseNameCn": "测试公司"}, ensure_ascii=False),
        "--execute",
        "--download-dir",
        str(download_dir),
        "-o",
        "json",
    ]
