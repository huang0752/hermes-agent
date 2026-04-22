"""Unified Juhe media runtime for URL-native image analysis and document parsing."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Optional

from agent.auxiliary_client import async_call_llm, extract_content_or_reasoning
from gateway.platforms.base import MessageType, safe_url_for_log

logger = logging.getLogger(__name__)

_JUHE_IMAGE_ANALYSIS_PROMPT = (
    "Describe everything visible in this image in thorough detail. "
    "Include any text, code, data, objects, people, layout, colors, "
    "and any other notable visual information."
)


@dataclass(frozen=True)
class JuheAttachmentParseBundle:
    display_name: str
    content_kind: str
    extracted_text: str
    display_description: str
    parser: str
    content_format: str
    parse_status: str = "success"
    error_message: str = ""


def truncate_media_description_body(text: str, limit: int = 240) -> str:
    normalized = " ".join(str(text or "").replace("\r\n", "\n").replace("\r", "\n").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(limit - 1, 1)].rstrip() + "…"


def build_image_media_description(display_name: str, analysis_text: str) -> str:
    display = str(display_name or "").strip() or "image"
    body = truncate_media_description_body(analysis_text)
    if not body:
        return f"图片 {display}：已收到，但暂未生成描述"
    return f"图片 {display}：{body}"


def bundle_from_cache_record(
    record: Optional[dict[str, Any]],
    display_name: str,
    *,
    default_kind: str,
) -> Optional[JuheAttachmentParseBundle]:
    if not record:
        return None
    resolved_name = str(display_name or record.get("file_name") or "attachment").strip() or "attachment"
    content_kind = str(record.get("content_kind") or default_kind).strip().lower() or default_kind
    extracted_text = str(record.get("extracted_text") or "")
    display_description = str(record.get("display_description") or "").strip()
    if not display_description:
        if content_kind == "image":
            display_description = build_image_media_description(resolved_name, extracted_text)
        else:
            display_description = f"文件 {resolved_name}：已收到，但暂未提取到正文"
    return JuheAttachmentParseBundle(
        display_name=resolved_name,
        content_kind=content_kind,
        extracted_text=extracted_text,
        display_description=display_description,
        parser=str(record.get("parser") or "cache"),
        content_format=str(record.get("content_format") or ("vision" if content_kind == "image" else "text")),
        parse_status=str(record.get("parse_status") or "success").strip().lower() or "success",
        error_message=str(record.get("error_message") or ""),
    )


def _get_auxiliary_task_config(task: str) -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        config = load_config()
    except Exception:
        return {}

    auxiliary = config.get("auxiliary", {}) if isinstance(config, dict) else {}
    task_config = auxiliary.get(task, {}) if isinstance(auxiliary, dict) else {}
    return task_config if isinstance(task_config, dict) else {}


def _task_timeout(task: str, default: float = 120.0) -> float:
    task_config = _get_auxiliary_task_config(task)
    raw_timeout = task_config.get("timeout")
    if raw_timeout is None:
        return default
    try:
        return float(raw_timeout)
    except (TypeError, ValueError):
        return default


def _juhe_media_fallback_configured() -> bool:
    task_config = _get_auxiliary_task_config("juhe_media_fallback")
    return any(str(task_config.get(key) or "").strip() for key in ("provider", "model", "base_url", "api_key"))


def _image_messages(image_url: str, prompt: str) -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        }
    ]


async def _run_juhe_image_llm_task(*, task: str, image_url: str, prompt: str) -> str:
    messages = _image_messages(image_url, prompt)
    response = await async_call_llm(
        task=task,
        messages=messages,
        temperature=0.1,
        max_tokens=2000,
        timeout=_task_timeout(task),
    )
    analysis = extract_content_or_reasoning(response).strip()
    if analysis:
        return analysis

    logger.warning(
        "Juhe image analysis returned empty content task=%s image=%s; retrying once",
        task,
        safe_url_for_log(image_url),
    )
    retry_response = await async_call_llm(
        task=task,
        messages=messages,
        temperature=0.1,
        max_tokens=2000,
        timeout=_task_timeout(task),
    )
    return extract_content_or_reasoning(retry_response).strip()


async def analyze_juhe_image_bundle(*, image_url: str, display_name: str) -> JuheAttachmentParseBundle:
    parser = "juhe_media_url"
    image_log_ref = safe_url_for_log(image_url)

    try:
        logger.info("Juhe image analysis starting task=juhe_media image=%s", image_log_ref)
        analysis = await _run_juhe_image_llm_task(
            task="juhe_media",
            image_url=image_url,
            prompt=_JUHE_IMAGE_ANALYSIS_PROMPT,
        )
    except Exception as exc:
        logger.warning(
            "Juhe image analysis primary failed image=%s error=%s",
            image_log_ref,
            exc.__class__.__name__,
            exc_info=True,
        )
        if not _juhe_media_fallback_configured():
            return JuheAttachmentParseBundle(
                display_name=display_name,
                content_kind="image",
                extracted_text="",
                display_description=build_image_media_description(display_name, ""),
                parser=parser,
                content_format="vision",
                parse_status="failed",
                error_message=str(exc),
            )

        try:
            parser = "juhe_media_fallback_url"
            logger.info(
                "Juhe image analysis retrying with fallback task=juhe_media_fallback image=%s",
                image_log_ref,
            )
            analysis = await _run_juhe_image_llm_task(
                task="juhe_media_fallback",
                image_url=image_url,
                prompt=_JUHE_IMAGE_ANALYSIS_PROMPT,
            )
        except Exception as fallback_exc:
            logger.error(
                "Juhe image analysis fallback failed image=%s error=%s",
                image_log_ref,
                fallback_exc.__class__.__name__,
                exc_info=True,
            )
            return JuheAttachmentParseBundle(
                display_name=display_name,
                content_kind="image",
                extracted_text="",
                display_description=build_image_media_description(display_name, ""),
                parser=parser,
                content_format="vision",
                parse_status="failed",
                error_message=str(fallback_exc),
            )

    if analysis:
        logger.info(
            "Juhe image analysis completed parser=%s image=%s chars=%s",
            parser,
            image_log_ref,
            len(analysis),
        )
        return JuheAttachmentParseBundle(
            display_name=display_name,
            content_kind="image",
            extracted_text=analysis,
            display_description=build_image_media_description(display_name, analysis),
            parser=parser,
            content_format="vision",
            parse_status="success",
        )

    logger.warning("Juhe image analysis produced no usable text parser=%s image=%s", parser, image_log_ref)
    return JuheAttachmentParseBundle(
        display_name=display_name,
        content_kind="image",
        extracted_text="",
        display_description=build_image_media_description(display_name, ""),
        parser=parser,
        content_format="vision",
        parse_status="failed",
        error_message="empty_analysis",
    )


def build_image_agent_context(
    bundle: JuheAttachmentParseBundle,
    *,
    is_juhe_event: bool,
    image_url: str,
) -> str:
    if bundle.parse_status == "success" and bundle.extracted_text.strip():
        if is_juhe_event:
            return f"[The user sent an image~ Here's what I can see:\n{bundle.extracted_text}]"
        return (
            f"[The user sent an image~ Here's what I can see:\n{bundle.extracted_text}]\n"
            f"[If you need a closer look, use vision_analyze with image_url: {image_url} ~]"
        )

    if is_juhe_event:
        return "[The user sent an image but I couldn't quite see it this time (>_<).]"
    return (
        "[The user sent an image but I couldn't quite see it this time (>_<) "
        f"You can try looking at it yourself with vision_analyze using image_url: {image_url}]"
    )


async def parse_juhe_media_bundle(
    *,
    access_url: str,
    display_name: str,
    media_type: str,
    attachment_type: MessageType,
):
    from gateway.document_text import parse_juhe_document_bundle

    if attachment_type == MessageType.PHOTO or str(media_type or "").strip().lower().startswith("image/"):
        return await analyze_juhe_image_bundle(
            image_url=access_url,
            display_name=display_name,
        )

    return await parse_juhe_document_bundle(
        document_url=access_url,
        display_name=display_name,
        media_type=media_type,
    )
