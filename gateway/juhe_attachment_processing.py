"""Compatibility exports for Juhe attachment processing helpers."""

from gateway.juhe_media_runtime import (
    JuheAttachmentParseBundle,
    analyze_juhe_image_bundle,
    build_image_agent_context,
    build_image_media_description,
    bundle_from_cache_record,
    truncate_media_description_body,
)

__all__ = [
    "JuheAttachmentParseBundle",
    "analyze_juhe_image_bundle",
    "build_image_agent_context",
    "build_image_media_description",
    "bundle_from_cache_record",
    "truncate_media_description_body",
]
