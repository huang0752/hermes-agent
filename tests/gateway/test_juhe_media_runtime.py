from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_analyze_juhe_image_bundle_uses_url_native_llm_messages():
    from gateway.juhe_media_runtime import analyze_juhe_image_bundle

    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="营业执照 OCR 内容"))]
    )

    with patch(
        "tools.vision_tools.vision_analyze_tool",
        new=AsyncMock(side_effect=AssertionError("legacy vision tool must not run for Juhe images")),
    ), patch(
        "gateway.juhe_media_runtime.async_call_llm",
        new=AsyncMock(return_value=response),
    ) as llm_mock, patch(
        "gateway.juhe_media_runtime.extract_content_or_reasoning",
        return_value="营业执照 OCR 内容",
    ):
        bundle = await analyze_juhe_image_bundle(
            image_url="https://minio.example.com/license.jpg?X-Amz-Signature=1",
            display_name="license.jpg",
        )

    assert bundle.parse_status == "success"
    assert bundle.extracted_text == "营业执照 OCR 内容"
    kwargs = llm_mock.await_args.kwargs
    assert kwargs["task"] == "juhe_media"
    assert kwargs["messages"][0]["content"][1]["type"] == "image_url"
    assert kwargs["messages"][0]["content"][1]["image_url"]["url"] == (
        "https://minio.example.com/license.jpg?X-Amz-Signature=1"
    )
    assert not kwargs["messages"][0]["content"][1]["image_url"]["url"].startswith("data:")
