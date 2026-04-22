import json

from gateway.delivery_artifacts import (
    DeliveryArtifact,
    DeliveryArtifactStore,
    extract_delivery_artifacts,
    sanitize_tool_result_for_model,
)


def test_extract_delivery_artifacts_returns_hidden_local_artifact():
    content = json.dumps(
        {
            "status": "completed",
            "job_id": 92,
            "delivery": {
                "filename": "final.zip",
                "local_path": "/tmp/final.zip",
                "size": 2048,
                "media_tag": "MEDIA:/tmp/final.zip",
            },
        },
        ensure_ascii=False,
    )

    artifacts = extract_delivery_artifacts(
        "mcp_local_materialize_render_job_artifact",
        content,
    )

    assert artifacts == [
        DeliveryArtifact(
            tool_name="mcp_local_materialize_render_job_artifact",
            local_path="/tmp/final.zip",
            filename="final.zip",
            size=2048,
            media_tag="MEDIA:/tmp/final.zip",
        )
    ]


def test_extract_delivery_artifacts_unwraps_fastmcp_envelope():
    inner_payload = {
        "status": "completed",
        "job_id": 95,
        "delivery": {
            "filename": "wrapped.zip",
            "local_path": "/tmp/wrapped.zip",
            "size": 4096,
            "media_tag": "MEDIA:/tmp/wrapped.zip",
        },
    }
    content = json.dumps(
        {
            "result": json.dumps(inner_payload, ensure_ascii=False),
            "structuredContent": inner_payload,
        },
        ensure_ascii=False,
    )

    artifacts = extract_delivery_artifacts(
        "mcp_local_materialize_render_job_artifact",
        content,
    )

    assert artifacts == [
        DeliveryArtifact(
            tool_name="mcp_local_materialize_render_job_artifact",
            local_path="/tmp/wrapped.zip",
            filename="wrapped.zip",
            size=4096,
            media_tag="MEDIA:/tmp/wrapped.zip",
        )
    ]


def test_sanitize_tool_result_for_model_strips_local_delivery_secrets():
    content = json.dumps(
        {
            "status": "completed",
            "job_id": 92,
            "download_url": "https://example.com/final.zip?sig=1",
            "output_url": "https://example.com/final.zip?sig=1",
            "delivery": {
                "filename": "final.zip",
                "local_path": "/tmp/final.zip",
                "size": 2048,
                "media_tag": "MEDIA:/tmp/final.zip",
            },
        },
        ensure_ascii=False,
    )

    sanitized = sanitize_tool_result_for_model(
        "mcp_local_materialize_render_job_artifact",
        content,
    )
    payload = json.loads(sanitized)

    assert payload["delivery"]["filename"] == "final.zip"
    assert payload["delivery"]["size"] == 2048
    assert "local_path" not in payload["delivery"]
    assert "media_tag" not in payload["delivery"]
    assert "download_url" not in payload
    assert "output_url" not in payload


def test_sanitize_tool_result_for_model_unwraps_fastmcp_envelope():
    inner_payload = {
        "status": "completed",
        "job_id": 95,
        "download_url": "https://example.com/wrapped.zip?sig=1",
        "delivery": {
            "filename": "wrapped.zip",
            "local_path": "/tmp/wrapped.zip",
            "size": 4096,
            "media_tag": "MEDIA:/tmp/wrapped.zip",
        },
    }
    content = json.dumps(
        {
            "result": json.dumps(inner_payload, ensure_ascii=False),
            "structuredContent": inner_payload,
        },
        ensure_ascii=False,
    )

    sanitized = sanitize_tool_result_for_model(
        "mcp_local_materialize_render_job_artifact",
        content,
    )
    payload = json.loads(sanitized)

    assert "result" not in payload
    assert "structuredContent" not in payload
    assert payload["job_id"] == 95
    assert payload["delivery"]["filename"] == "wrapped.zip"
    assert "local_path" not in payload["delivery"]
    assert "media_tag" not in payload["delivery"]
    assert "download_url" not in payload


def test_delivery_artifact_store_records_media_tags_by_session():
    store = DeliveryArtifactStore()
    content = json.dumps(
        {
            "delivery": {
                "filename": "final.zip",
                "local_path": "/tmp/final.zip",
                "size": 2048,
                "media_tag": "MEDIA:/tmp/final.zip",
            }
        },
        ensure_ascii=False,
    )

    recorded = store.record(
        session_key="session-1",
        tool_use_id="tool-call-1",
        tool_name="mcp_local_materialize_render_job_artifact",
        content=content,
    )

    assert len(recorded) == 1
    assert store.media_tags_for_session("session-1") == ["MEDIA:/tmp/final.zip"]
    store.clear_session("session-1")
    assert store.media_tags_for_session("session-1") == []
