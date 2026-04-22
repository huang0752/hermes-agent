"""
Tests for MEDIA tag extraction from tool results.

Verifies that MEDIA tags (e.g., from TTS tool) are only extracted from
messages in the CURRENT turn, not from the full conversation history.
This prevents voice messages from accumulating and being sent multiple
times per reply. (Regression test for #160)
"""

import json
import re

import pytest

from gateway.run import (
    _augment_final_response_with_tool_media,
    _collect_tool_result_media_tags,
    _current_turn_messages,
)
from gateway.platforms.base import BasePlatformAdapter


def extract_media_tags_fixed(result_messages, history_len):
    """
    Extract MEDIA tags from tool results, but ONLY from new messages
    (those added after history_len). This is the fixed behavior.
    
    Args:
        result_messages: Full list of messages including history + new
        history_len: Length of history before this turn
        
    Returns:
        Tuple of (media_tags list, has_voice_directive bool)
    """
    media_tags = []
    has_voice_directive = False
    
    # Only process new messages from this turn
    new_messages = result_messages[history_len:] if len(result_messages) > history_len else []
    
    for msg in new_messages:
        if msg.get("role") == "tool" or msg.get("role") == "function":
            content = msg.get("content", "")
            if "MEDIA:" in content:
                for match in re.finditer(r'MEDIA:(\S+)', content):
                    path = match.group(1).strip().rstrip('",}')
                    if path:
                        media_tags.append(f"MEDIA:{path}")
                if "[[audio_as_voice]]" in content:
                    has_voice_directive = True
    
    return media_tags, has_voice_directive


def extract_media_tags_broken(result_messages):
    """
    The BROKEN behavior: extract MEDIA tags from ALL messages including history.
    This causes TTS voice messages to accumulate and be re-sent on every reply.
    """
    media_tags = []
    has_voice_directive = False
    
    for msg in result_messages:
        if msg.get("role") == "tool" or msg.get("role") == "function":
            content = msg.get("content", "")
            if "MEDIA:" in content:
                for match in re.finditer(r'MEDIA:(\S+)', content):
                    path = match.group(1).strip().rstrip('",}')
                    if path:
                        media_tags.append(f"MEDIA:{path}")
                if "[[audio_as_voice]]" in content:
                    has_voice_directive = True
    
    return media_tags, has_voice_directive


class TestMediaExtraction:
    """Tests for MEDIA tag extraction from tool results."""
    
    def test_media_tags_not_extracted_from_history(self):
        """MEDIA tags from previous turns should NOT be extracted again."""
        # Simulate conversation history with a TTS call from a previous turn
        history = [
            {"role": "user", "content": "Say hello as audio"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "text_to_speech"}}]},
            {"role": "tool", "tool_call_id": "1", "content": '{"success": true, "media_tag": "[[audio_as_voice]]\\nMEDIA:/path/to/audio1.ogg"}'},
            {"role": "assistant", "content": "I've said hello for you!"},
        ]
        
        # New turn: user asks a simple question
        new_messages = [
            {"role": "user", "content": "What time is it?"},
            {"role": "assistant", "content": "It's 3:30 AM."},
        ]
        
        all_messages = history + new_messages
        history_len = len(history)
        
        # Fixed behavior: should extract NO media tags (none in new messages)
        tags, voice_directive = extract_media_tags_fixed(all_messages, history_len)
        assert tags == [], "Fixed extraction should not find tags in history"
        assert voice_directive is False
        
        # Broken behavior: would incorrectly extract the old media tag
        broken_tags, broken_voice = extract_media_tags_broken(all_messages)
        assert len(broken_tags) == 1, "Broken extraction finds tags in history"
        assert "audio1.ogg" in broken_tags[0]
    
    def test_media_tags_extracted_from_current_turn(self):
        """MEDIA tags from the current turn SHOULD be extracted."""
        # History without TTS
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        
        # New turn with TTS call
        new_messages = [
            {"role": "user", "content": "Say goodbye as audio"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "2", "function": {"name": "text_to_speech"}}]},
            {"role": "tool", "tool_call_id": "2", "content": '{"success": true, "media_tag": "[[audio_as_voice]]\\nMEDIA:/path/to/audio2.ogg"}'},
            {"role": "assistant", "content": "I've said goodbye!"},
        ]
        
        all_messages = history + new_messages
        history_len = len(history)
        
        # Fixed behavior: should extract the new media tag
        tags, voice_directive = extract_media_tags_fixed(all_messages, history_len)
        assert len(tags) == 1, "Should extract media tag from current turn"
        assert "audio2.ogg" in tags[0]
        assert voice_directive is True
    
    def test_multiple_tts_calls_in_history_not_accumulated(self):
        """Multiple TTS calls in history should NOT accumulate in new responses."""
        # History with multiple TTS calls
        history = [
            {"role": "user", "content": "Say hello"},
            {"role": "tool", "tool_call_id": "1", "content": 'MEDIA:/audio/hello.ogg'},
            {"role": "assistant", "content": "Done!"},
            {"role": "user", "content": "Say goodbye"},
            {"role": "tool", "tool_call_id": "2", "content": 'MEDIA:/audio/goodbye.ogg'},
            {"role": "assistant", "content": "Done!"},
            {"role": "user", "content": "Say thanks"},
            {"role": "tool", "tool_call_id": "3", "content": 'MEDIA:/audio/thanks.ogg'},
            {"role": "assistant", "content": "Done!"},
        ]
        
        # New turn: no TTS
        new_messages = [
            {"role": "user", "content": "What time is it?"},
            {"role": "assistant", "content": "3 PM"},
        ]
        
        all_messages = history + new_messages
        history_len = len(history)
        
        # Fixed: no tags
        tags, _ = extract_media_tags_fixed(all_messages, history_len)
        assert tags == [], "Should not accumulate tags from history"
        
        # Broken: would have 3 tags (all the old ones)
        broken_tags, _ = extract_media_tags_broken(all_messages)
        assert len(broken_tags) == 3, "Broken version accumulates all history tags"
    
    def test_deduplication_within_current_turn(self):
        """Multiple MEDIA tags in current turn should be deduplicated."""
        history = []
        
        # Current turn with multiple tool calls producing same media
        new_messages = [
            {"role": "user", "content": "Multiple TTS"},
            {"role": "tool", "tool_call_id": "1", "content": 'MEDIA:/audio/same.ogg'},
            {"role": "tool", "tool_call_id": "2", "content": 'MEDIA:/audio/same.ogg'},  # duplicate
            {"role": "tool", "tool_call_id": "3", "content": 'MEDIA:/audio/different.ogg'},
            {"role": "assistant", "content": "Done!"},
        ]
        
        all_messages = history + new_messages
        
        tags, _ = extract_media_tags_fixed(all_messages, 0)
        # Even though same.ogg appears twice, deduplication happens after extraction
        # The extraction itself should get both, then caller deduplicates
        assert len(tags) == 3  # Raw extraction gets all
        
        # Deduplication as done in the actual code:
        seen = set()
        unique = [t for t in tags if t not in seen and not seen.add(t)]
        assert len(unique) == 2  # After dedup: same.ogg and different.ogg


class TestStructuredDeliveryUrlExtraction:
    """Tests for structured tool results that expose file delivery URLs."""

    def test_collects_download_url_from_tool_json(self):
        """Structured download_url values should be promoted to MEDIA tags."""
        url = "http://example.com/files/archive.zip?AWSAccessKeyId=test&Signature=abc&Expires=123"
        messages = [
            {
                "role": "tool",
                "tool_call_id": "1",
                "content": json.dumps({"success": True, "job_id": 91, "download_url": url}),
            }
        ]

        tags, voice_directive, raw_urls = _collect_tool_result_media_tags(messages, set())

        assert tags == [f"MEDIA:{url}"]
        assert voice_directive is False
        assert raw_urls == [url]

    def test_collects_nested_output_url_from_structured_content(self):
        """Nested structuredContent output_url values should also be promoted."""
        url = "https://example.com/render/output/report.pdf?token=secret"
        messages = [
            {
                "role": "tool",
                "tool_call_id": "2",
                "content": "render complete",
                "structuredContent": {
                    "success": True,
                    "delivery": {"output_url": url},
                },
            }
        ]

        tags, voice_directive, raw_urls = _collect_tool_result_media_tags(messages, set())

        assert tags == [f"MEDIA:{url}"]
        assert voice_directive is False
        assert raw_urls == [url]

    def test_certificate_render_job_download_url_stays_internal(self):
        """Certificate render-job URLs should be stripped from visible text, not promoted to MEDIA."""
        url = (
            "http://49.233.103.196:9000/certificate-dev/certificate_render_jobs/"
            "2026/04/22/job-92/certificates.zip?AWSAccessKeyId=test&Signature=abc&Expires=123"
        )
        messages = [
            {
                "role": "tool",
                "tool_call_id": "cert-job",
                "content": json.dumps({"success": True, "download_url": url}),
            }
        ]

        tags, voice_directive, raw_urls = _collect_tool_result_media_tags(messages, set())

        assert tags == []
        assert voice_directive is False
        assert raw_urls == [url]

    def test_prefers_local_delivery_artifact_over_remote_download_url(self):
        """When a tool result contains a local delivery artifact, remote links stay internal."""
        url = "https://example.com/render/output/report.zip?token=secret"
        local_path = "/tmp/certificate-delivery/report.zip"
        messages = [
            {
                "role": "tool",
                "tool_call_id": "local-first",
                "content": json.dumps(
                    {
                        "mode": "execute",
                        "delivery": {
                            "local_path": local_path,
                            "media_tag": f"MEDIA:{local_path}",
                        },
                        "execution": {
                            "render_job": {
                                "download_url": url,
                            }
                        },
                    }
                ),
            }
        ]

        tags, voice_directive, raw_urls = _collect_tool_result_media_tags(messages, set())

        assert tags == [f"MEDIA:{local_path}"]
        assert voice_directive is False
        assert raw_urls == []

    def test_prefers_local_artifact_when_certificate_url_is_present(self):
        """Explicit local delivery artifacts must keep remote render URLs internal."""
        url = (
            "http://49.233.103.196:9000/certificate-dev/certificate_render_jobs/"
            "2026/04/22/job-92/certificates.zip?AWSAccessKeyId=test&Signature=abc&Expires=123"
        )
        local_path = "/tmp/certificate-delivery/certificates.zip"
        messages = [
            {
                "role": "tool",
                "tool_call_id": "local-preferred-juhe",
                "content": json.dumps(
                    {
                        "delivery": {
                            "local_path": local_path,
                            "media_tag": f"MEDIA:{local_path}",
                            "download_url": url,
                        }
                    }
                ),
            }
        ]

        tags, voice_directive, raw_urls = _collect_tool_result_media_tags(messages, set())

        assert tags == [f"MEDIA:{local_path}"]
        assert voice_directive is False
        assert raw_urls == []

    def test_collect_tool_result_media_prefers_hidden_local_artifact(self):
        messages = [
            {
                "role": "tool",
                "tool_call_id": "cert-job",
                "content": json.dumps(
                    {
                        "status": "completed",
                        "job_id": 92,
                        "delivery": {
                            "filename": "final.zip",
                            "size": 2048,
                        },
                    }
                ),
            }
        ]
        hidden_media_tags = ["MEDIA:/tmp/final.zip"]

        augmented = _augment_final_response_with_tool_media(
            "压缩包已生成。",
            messages,
            set(),
            hidden_media_tags=hidden_media_tags,
        )

        assert "MEDIA:/tmp/final.zip" in augmented
        assert "download_url" not in augmented
        assert "/tmp/final.zip" in augmented

    def test_augment_final_response_strips_raw_download_link_and_appends_media(self):
        """Visible text should not retain a raw delivery URL once MEDIA is synthesized."""
        url = "http://example.com/certificates/job-91/archive.zip?AWSAccessKeyId=test&Signature=abc&Expires=123"
        messages = [
            {
                "role": "tool",
                "tool_call_id": "3",
                "content": json.dumps({"success": True, "download_url": url}),
            }
        ]

        final_response = f"压缩包已生成。\n下载链接：{url}"

        augmented = _augment_final_response_with_tool_media(final_response, messages, set())
        _, visible_text = BasePlatformAdapter.extract_media(augmented)

        assert "下载链接" not in visible_text
        assert url not in visible_text
        assert "压缩包已生成。" in visible_text
        assert f"MEDIA:{url}" in augmented

    def test_augment_final_response_strips_certificate_render_job_link_without_media(self):
        """Certificate render-job links should be removed from visible text without becoming MEDIA."""
        url = (
            "http://49.233.103.196:9000/certificate-dev/certificate_render_jobs/"
            "2026/04/22/job-92/certificates.zip?AWSAccessKeyId=test&Signature=abc&Expires=123"
        )
        messages = [
            {
                "role": "tool",
                "tool_call_id": "cert-job-visible",
                "content": json.dumps({"success": True, "download_url": url}),
            }
        ]

        final_response = f"压缩包已生成。\n下载链接：{url}"

        augmented = _augment_final_response_with_tool_media(final_response, messages, set())
        _, visible_text = BasePlatformAdapter.extract_media(augmented)

        assert "下载链接" not in visible_text
        assert url not in visible_text
        assert "压缩包已生成。" in visible_text
        assert f"MEDIA:{url}" not in augmented

    def test_augment_final_response_appends_missing_media_even_if_media_already_present(self):
        """Existing MEDIA tags in the reply should not suppress new tool-result delivery media."""
        url = "https://example.com/results/final-bundle.zip?token=abc"
        messages = [
            {
                "role": "tool",
                "tool_call_id": "4",
                "content": json.dumps({"success": True, "download_url": url}),
            }
        ]

        final_response = "语音已生成。\n[[audio_as_voice]]\nMEDIA:/tmp/voice.ogg"

        augmented = _augment_final_response_with_tool_media(final_response, messages, set())

        assert "MEDIA:/tmp/voice.ogg" in augmented
        assert f"MEDIA:{url}" in augmented

    def test_augment_final_response_strips_local_delivery_paths_from_visible_text(self):
        """Local artifact paths from tool results should not leak into visible assistant text."""
        local_path = "/tmp/certificate-delivery/final-package.zip"
        messages = [
            {
                "role": "tool",
                "tool_call_id": "local-path-1",
                "content": json.dumps(
                    {
                        "mode": "execute",
                        "delivery": {
                            "filename": "final-package.zip",
                            "local_path": local_path,
                            "media_tag": f"MEDIA:{local_path}",
                        },
                    }
                ),
            }
        ]

        final_response = f"压缩包已生成。\n本地路径：{local_path}\n请查收。"
        augmented = _augment_final_response_with_tool_media(final_response, messages, set())
        _, visible_text = BasePlatformAdapter.extract_media(augmented)

        assert local_path not in visible_text
        assert "本地路径" not in visible_text
        assert "压缩包已生成。" in visible_text
        assert "请查收。" in visible_text
        assert f"MEDIA:{local_path}" in augmented

    def test_augment_final_response_strips_manual_media_tag_with_whitespace_before_reappending(self):
        """Manual MEDIA tags using optional post-colon whitespace should not leave an orphaned prefix."""
        local_path = "/tmp/certificate-delivery/final-package.zip"
        hidden_media_tags = [f"MEDIA:{local_path}"]

        augmented = _augment_final_response_with_tool_media(
            f"压缩包已生成。\nMEDIA: {local_path}",
            [],
            set(),
            hidden_media_tags=hidden_media_tags,
        )
        media, visible_text = BasePlatformAdapter.extract_media(augmented)

        assert media == [(local_path, False)]
        assert visible_text == "压缩包已生成。"
        assert augmented.count("MEDIA:") == 1

    def test_augment_final_response_strips_manual_media_tag_with_quoted_path_before_reappending(self):
        """Quoted manual MEDIA tags should not leave wrapper debris in the visible response."""
        local_path = "/tmp/certificate-delivery/final package.zip"
        hidden_media_tags = [f"MEDIA:{local_path}"]

        augmented = _augment_final_response_with_tool_media(
            f'压缩包已生成。\nMEDIA: "{local_path}"',
            [],
            set(),
            hidden_media_tags=hidden_media_tags,
        )
        media, visible_text = BasePlatformAdapter.extract_media(augmented)

        assert media == [(local_path, False)]
        assert visible_text == "压缩包已生成。"
        assert augmented.count("MEDIA:") == 1

    def test_current_turn_message_slice_excludes_stale_history_tool_payloads(self):
        """Old tool payloads must not be rescanned for local delivery artifacts."""
        old_path = "/tmp/certificate-render-artifacts/old-anyang.zip"
        all_messages = [
            {"role": "user", "content": "安阳宝华冶金耐材有限公司 三体系"},
            {
                "role": "tool",
                "tool_call_id": "old-tool",
                "content": json.dumps(
                    {
                        "status": "completed",
                        "delivery": {
                            "filename": "old-anyang.zip",
                            "local_path": old_path,
                            "media_tag": f"MEDIA:{old_path}",
                        },
                    }
                ),
            },
            {"role": "user", "content": "深圳市万洁环境产业有限公司 中天三体系 26年4.12 范围全要"},
            {
                "role": "tool",
                "tool_call_id": "current-tool",
                "content": json.dumps({"status": "draft_only"}),
            },
        ]

        current_messages = _current_turn_messages(all_messages, 2)
        augmented = _augment_final_response_with_tool_media(
            "已查到档案公司。",
            current_messages,
            set(),
        )

        assert augmented == "已查到档案公司。"
        assert old_path not in augmented

    def test_current_turn_message_slice_keeps_new_local_delivery_artifact(self):
        """Current-turn artifacts should still be promoted after history slicing."""
        old_path = "/tmp/certificate-render-artifacts/old-anyang.zip"
        current_path = "/tmp/certificate-render-artifacts/current-wanjie.zip"
        all_messages = [
            {"role": "user", "content": "安阳宝华冶金耐材有限公司 三体系"},
            {
                "role": "tool",
                "tool_call_id": "old-tool",
                "content": json.dumps(
                    {
                        "status": "completed",
                        "delivery": {
                            "filename": "old-anyang.zip",
                            "local_path": old_path,
                            "media_tag": f"MEDIA:{old_path}",
                        },
                    }
                ),
            },
            {"role": "user", "content": "深圳市万洁环境产业有限公司 中天三体系 26年4.12 范围全要"},
            {
                "role": "tool",
                "tool_call_id": "current-tool",
                "content": json.dumps(
                    {
                        "status": "completed",
                        "delivery": {
                            "filename": "current-wanjie.zip",
                            "local_path": current_path,
                            "media_tag": f"MEDIA:{current_path}",
                        },
                    }
                ),
            },
        ]

        current_messages = _current_turn_messages(all_messages, 2)
        augmented = _augment_final_response_with_tool_media(
            "万洁草案如下。",
            current_messages,
            set(),
        )

        assert f"MEDIA:{current_path}" in augmented
        assert old_path not in augmented


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
