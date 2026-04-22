"""Gateway STT config tests — honor stt.enabled: false from config.yaml."""

from pathlib import Path
from types import SimpleNamespace
import zipfile
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from gateway.config import GatewayConfig, Platform, load_gateway_config
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from hermes_state import SessionDB


def _write_minimal_docx(path: Path, paragraphs: list[str]) -> None:
    document_xml = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">',
        "<w:body>",
    ]
    for paragraph in paragraphs:
        document_xml.append(f"<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>")
    document_xml.extend(["</w:body>", "</w:document>"])

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
""",
        )
        archive.writestr("word/document.xml", "\n".join(document_xml))


def _write_minimal_pdf(path: Path, text: str) -> None:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT\n/F1 12 Tf\n72 72 Td\n({escaped}) Tj\nET".encode("utf-8")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(content))
        content.extend(f"{index} 0 obj\n".encode("ascii"))
        content.extend(body)
        content.extend(b"\nendobj\n")

    startxref = len(content)
    content.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    content.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    content.extend(
        (
            f"trailer\n<< /Root 1 0 R /Size {len(objects) + 1} >>\n"
            f"startxref\n{startxref}\n%%EOF\n"
        ).encode("ascii")
    )
    path.write_bytes(bytes(content))


def _write_minimal_xlsx(path: Path, rows: list[list[str]]) -> None:
    shared_strings: list[str] = []
    shared_index: dict[str, int] = {}

    def _shared_string_index(value: str) -> int:
        if value not in shared_index:
            shared_index[value] = len(shared_strings)
            shared_strings.append(value)
        return shared_index[value]

    sheet_rows: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells: list[str] = []
        for col_index, value in enumerate(row, start=1):
            cell_ref = f"{chr(64 + col_index)}{row_index}"
            string_index = _shared_string_index(value)
            cells.append(f'<c r="{cell_ref}" t="s"><v>{string_index}</v></c>')
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    shared_string_items = "".join(
        f"<si><t>{value}</t></si>" for value in shared_strings
    )

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>
""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
""",
        )
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Sheet1" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>
""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
</Relationships>
""",
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(shared_strings)}" uniqueCount="{len(shared_strings)}">'
                f"{shared_string_items}</sst>"
            ),
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f"<sheetData>{''.join(sheet_rows)}</sheetData>"
                "</worksheet>"
            ),
        )


def test_gateway_config_stt_disabled_from_dict_nested():
    config = GatewayConfig.from_dict({"stt": {"enabled": False}})
    assert config.stt_enabled is False


def test_load_gateway_config_bridges_stt_enabled_from_config_yaml(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.dump({"stt": {"enabled": False}}),
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    config = load_gateway_config()

    assert config.stt_enabled is False


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_skips_when_stt_disabled():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=False)

    with patch(
        "tools.transcription_tools.transcribe_audio",
        side_effect=AssertionError("transcribe_audio should not be called when STT is disabled"),
    ):
        result = await runner._enrich_message_with_transcription(
            "caption",
            ["/tmp/voice.ogg"],
        )

    assert "transcription is disabled" in result.lower()
    assert "caption" in result


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_avoids_bogus_no_provider_message_for_backend_key_errors():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": False, "error": "VOICE_TOOLS_OPENAI_KEY not set"},
    ):
        result = await runner._enrich_message_with_transcription(
            "caption",
            ["/tmp/voice.ogg"],
        )

    assert "No STT provider is configured" not in result
    assert "trouble transcribing" in result
    assert "caption" in result


@pytest.mark.asyncio
async def test_prepare_inbound_message_text_transcribes_queued_voice_event():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="123",
        chat_type="dm",
    )
    event = MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        source=source,
        media_urls=["/tmp/queued-voice.ogg"],
        media_types=["audio/ogg"],
    )

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={
            "success": True,
            "transcript": "queued voice transcript",
            "provider": "local_command",
        },
    ):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result is not None
    assert "queued voice transcript" in result
    assert "voice message" in result.lower()


@pytest.mark.asyncio
async def test_prepare_inbound_message_text_prefixes_sender_for_shared_group_session():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False

    source = SessionSource(
        platform=Platform.JUHE,
        chat_id="R:2001",
        chat_type="group",
        user_id="1001",
        user_name="Alice",
    )
    setattr(source, "shared_session", True)
    event = MessageEvent(
        text="Please review the latest change.",
        message_type=MessageType.TEXT,
        source=source,
    )

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert result == "[Alice] Please review the latest change."


@pytest.mark.asyncio
async def test_prepare_inbound_message_text_includes_document_note_for_text_event_with_document_media():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False

    source = SessionSource(
        platform=Platform.JUHE,
        chat_id="R:2001",
        chat_type="group",
        user_id="1001",
        user_name="Alice",
    )
    event = MessageEvent(
        text="[Alice] 我发的文件你读取得到吗",
        message_type=MessageType.TEXT,
        source=source,
        media_urls=["/tmp/queued-group-sheet.xlsx"],
        media_types=["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
    )

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert result is not None
    assert "The user sent a document" in result
    assert "/tmp/queued-group-sheet.xlsx" in result
    assert "[Alice] 我发的文件你读取得到吗" in result


@pytest.mark.asyncio
async def test_prepare_inbound_message_text_uses_file_extension_for_octet_stream_images():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False

    source = SessionSource(
        platform=Platform.JUHE,
        chat_id="R:2001",
        chat_type="group",
        user_id="1001",
        user_name="Alice",
    )
    event = MessageEvent(
        text="[Alice] 看下这个营业执照",
        message_type=MessageType.TEXT,
        source=source,
        media_urls=["/tmp/license.jpg"],
        media_types=["application/octet-stream"],
    )

    with patch(
        "tools.vision_tools.vision_analyze_tool",
        return_value='{"success": true, "analysis": "营业执照 OCR 内容"}',
    ):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result is not None
    assert "营业执照 OCR 内容" in result
    assert "[Alice] 看下这个营业执照" in result


@pytest.mark.asyncio
async def test_prepare_inbound_message_text_prefers_juhe_presigned_image_url():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False

    source = SessionSource(
        platform=Platform.JUHE,
        chat_id="R:2001",
        chat_type="group",
        user_id="1001",
        user_name="Alice",
    )
    event = MessageEvent(
        text="[Alice] 看下这个营业执照",
        message_type=MessageType.TEXT,
        source=source,
        media_urls=["/tmp/license.jpg"],
        media_types=["image/jpeg"],
    )
    setattr(
        event,
        "_juhe_media_access_urls",
        ["http://124.220.81.138:9000/wework/wwcdn/private/license.jpg?X-Amz-Signature=1"],
    )

    with patch(
        "gateway.juhe_media_runtime.async_call_llm",
        new=AsyncMock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="营业执照 OCR 内容"))]
            )
        ),
    ) as juhe_vision_mock, patch(
        "gateway.juhe_media_runtime.extract_content_or_reasoning",
        return_value="营业执照 OCR 内容",
    ):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result is not None
    assert "营业执照 OCR 内容" in result
    assert "X-Amz-Signature" not in result
    juhe_vision_mock.assert_awaited_once()
    assert juhe_vision_mock.await_args.kwargs["task"] == "juhe_media"
    assert juhe_vision_mock.await_args.kwargs["messages"][0]["content"][1]["image_url"]["url"] == (
        "http://124.220.81.138:9000/wework/wwcdn/private/license.jpg?X-Amz-Signature=1"
    )
    assert getattr(event, "_juhe_media_descriptions") == [
        "图片 license.jpg：营业执照 OCR 内容"
    ]


def test_build_document_context_for_agent_includes_docx_content(tmp_path):
    from gateway.document_text import build_document_context_for_agent

    docx_path = tmp_path / "business-license.docx"
    _write_minimal_docx(docx_path, ["南京明慧教育科技有限公司", "统一社会信用代码：91320116MACBA1KL5R"])

    result = build_document_context_for_agent(
        str(docx_path),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert result is not None
    assert "business-license.docx" in result
    assert "南京明慧教育科技有限公司" in result
    assert "91320116MACBA1KL5R" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_name", "media_type"),
    [
        ("license.pdf", "application/pdf"),
        ("license.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("license.doc", "application/msword"),
    ],
)
async def test_prepare_inbound_message_text_prefers_juhe_presigned_document_url(file_name, media_type):
    from gateway.run import GatewayRunner
    from gateway.document_text import RemoteDocumentAgentContext

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False

    source = SessionSource(
        platform=Platform.JUHE,
        chat_id="R:2001",
        chat_type="group",
        user_id="1001",
        user_name="Alice",
    )
    event = MessageEvent(
        text="[Alice] 帮我看看这个文件",
        message_type=MessageType.DOCUMENT,
        source=source,
        media_urls=[f"juhe://attachment/test/{file_name}"],
        media_types=[media_type],
    )
    setattr(
        event,
        "_juhe_media_access_urls",
        [f"http://124.220.81.138:9000/wework/wwcdn/private/{file_name}?X-Amz-Signature=1"],
    )
    setattr(
        event,
        "_juhe_attachment_identities",
        [
            {
                "bucket": "wework",
                "object_key": f"wwcdn/private/{file_name}",
                "object_url": f"http://124.220.81.138:9000/wework/wwcdn/private/{file_name}",
                "file_id": f"file-{file_name}",
                "file_md5": f"md5-{file_name}",
            }
        ],
    )

    with patch(
        "gateway.document_text.build_remote_document_context_for_agent",
        new=AsyncMock(
            return_value=RemoteDocumentAgentContext(
                display_name=file_name,
                extracted_text="文档远程识别内容",
                display_description=f"文件 {file_name}：文档远程识别内容",
                agent_context=f"文件 {file_name}：文档远程识别内容\n\n[Content of {file_name}]:\n文档远程识别内容",
                parser="dashscope_doc_url",
                from_cache=False,
            )
        ),
    ) as remote_doc_mock:
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result is not None
    assert "文档远程识别内容" in result
    remote_doc_mock.assert_called_once()
    assert getattr(event, "_juhe_media_descriptions") == [
        f"文件 {file_name}：文档远程识别内容"
    ]


def test_build_document_context_for_agent_includes_pdf_text_layer(tmp_path):
    from gateway.document_text import build_document_context_for_agent

    pdf_path = tmp_path / "business-license.pdf"
    _write_minimal_pdf(pdf_path, "Unified Credit Code 91320116MACBA1KL5R")

    result = build_document_context_for_agent(
        str(pdf_path),
        "application/pdf",
    )

    assert result is not None
    assert "business-license.pdf" in result
    assert "Unified Credit Code 91320116MACBA1KL5R" in result


def test_extract_local_document_text_includes_xlsx_cells(tmp_path):
    from gateway.document_text import extract_local_document_text

    xlsx_path = tmp_path / "business-license.xlsx"
    _write_minimal_xlsx(
        xlsx_path,
        [
            ["公司名称", "南京明慧教育科技有限公司"],
            ["统一社会信用代码", "91320116MACBA1KL5R"],
        ],
    )

    result = extract_local_document_text(
        str(xlsx_path),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    assert result is not None
    assert result.display_name == "business-license.xlsx"
    assert "公司名称" in result.text
    assert "南京明慧教育科技有限公司" in result.text
    assert "91320116MACBA1KL5R" in result.text


@pytest.mark.asyncio
async def test_parse_juhe_document_bundle_prefers_local_xlsx_extract_before_mineru(tmp_path):
    from gateway.document_text import parse_juhe_document_bundle

    xlsx_path = tmp_path / "business-license.xlsx"
    _write_minimal_xlsx(
        xlsx_path,
        [
            ["公司名称", "南京明慧教育科技有限公司"],
            ["统一社会信用代码", "91320116MACBA1KL5R"],
        ],
    )

    with patch(
        "gateway.document_text._download_document_to_temp_path",
        new=AsyncMock(return_value=xlsx_path),
    ), patch(
        "gateway.document_text._extract_document_text_via_mineru_lightweight_url",
        new=AsyncMock(side_effect=AssertionError("lightweight MinerU should not be used when local xlsx extraction succeeds")),
    ), patch(
        "gateway.document_text._extract_document_text_via_mineru_precise_url",
        new=AsyncMock(side_effect=AssertionError("precise MinerU should not be used when local xlsx extraction succeeds")),
    ):
        bundle = await parse_juhe_document_bundle(
            document_url="https://example.com/business-license.xlsx",
            display_name="business-license.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    assert bundle.display_name == "business-license.xlsx"
    assert "南京明慧教育科技有限公司" in bundle.extracted_text
    assert bundle.display_description.startswith("文件 business-license.xlsx：")


@pytest.mark.asyncio
async def test_parse_juhe_document_bundle_uses_precise_fallback_for_doc(tmp_path):
    from gateway.document_text import ExtractedDocumentText, parse_juhe_document_bundle

    doc_path = tmp_path / "business-license.doc"
    doc_path.write_bytes(b"fake-doc")

    with patch(
        "gateway.document_text._download_document_to_temp_path",
        new=AsyncMock(return_value=doc_path),
    ), patch(
        "gateway.document_text._extract_legacy_word_text",
        return_value="",
    ), patch(
        "gateway.document_text._extract_document_text_via_mineru_lightweight_url",
        new=AsyncMock(side_effect=AssertionError("lightweight MinerU should be skipped for .doc fallback")),
    ), patch(
        "gateway.document_text._extract_document_text_via_mineru_precise_url",
        new=AsyncMock(
            return_value=ExtractedDocumentText(
                display_name="business-license.doc",
                text="精准接口提取出的 DOC 内容",
            )
        ),
    ) as precise_mock:
        bundle = await parse_juhe_document_bundle(
            document_url="https://example.com/business-license.doc",
            display_name="business-license.doc",
            media_type="application/msword",
        )

    precise_mock.assert_awaited_once()
    assert bundle.display_name == "business-license.doc"
    assert bundle.extracted_text == "精准接口提取出的 DOC 内容"
    assert bundle.display_description == "文件 business-license.doc：精准接口提取出的 DOC 内容"


@pytest.mark.asyncio
async def test_prepare_inbound_message_text_reuses_juhe_document_parse_cache(tmp_path):
    from gateway.run import GatewayRunner

    db = SessionDB(db_path=tmp_path / "state.db")
    db.upsert_juhe_attachment_parse_cache(
        platform="juhe",
        bucket="wework",
        object_key="wwcdn/private/license.pdf",
        object_url="http://124.220.81.138:9000/wework/wwcdn/private/license.pdf",
        file_id="file-license",
        file_md5="md5-license",
        media_type="application/pdf",
        file_name="license.pdf",
        parser="dashscope_doc_url",
        extracted_text="缓存中的 PDF 内容",
    )

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._session_db = db
    runner._has_setup_skill = lambda: False

    source = SessionSource(
        platform=Platform.JUHE,
        chat_id="R:2001",
        chat_type="group",
        user_id="1001",
        user_name="Alice",
    )
    event = MessageEvent(
        text="[Alice] 再看看这个 PDF",
        message_type=MessageType.DOCUMENT,
        source=source,
        media_urls=["juhe://attachment/test/license.pdf"],
        media_types=["application/pdf"],
    )
    setattr(
        event,
        "_juhe_media_access_urls",
        ["http://124.220.81.138:9000/wework/wwcdn/private/license.pdf?X-Amz-Signature=1"],
    )
    setattr(
        event,
        "_juhe_attachment_identities",
        [
            {
                "bucket": "wework",
                "object_key": "wwcdn/private/license.pdf",
                "object_url": "http://124.220.81.138:9000/wework/wwcdn/private/license.pdf",
                "file_id": "file-license",
                "file_md5": "md5-license",
            }
        ],
    )

    with patch(
        "gateway.document_text._extract_document_text_via_dashscope_doc_url",
        new=AsyncMock(side_effect=AssertionError("remote parser should not be called on cache hit")),
    ):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result is not None
    assert "缓存中的 PDF 内容" in result
    assert getattr(event, "_juhe_media_descriptions") == [
        "文件 license.pdf：缓存中的 PDF 内容"
    ]


@pytest.mark.asyncio
async def test_prepare_inbound_message_text_reuses_juhe_image_parse_cache(tmp_path):
    from gateway.run import GatewayRunner

    db = SessionDB(db_path=tmp_path / "state.db")
    db.upsert_juhe_attachment_parse_cache(
        platform="juhe",
        bucket="wework",
        object_key="wwcdn/private/license.jpg",
        object_url="http://124.220.81.138:9000/wework/wwcdn/private/license.jpg",
        file_id="file-license",
        file_md5="md5-license",
        media_type="image/jpeg",
        file_name="license.jpg",
        parser="vision_preheat",
        extracted_text="营业执照 OCR 内容",
        content_kind="image",
        content_format="vision",
        display_description="图片 license.jpg：营业执照 OCR 内容",
        parse_status="success",
    )

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._session_db = db
    runner._has_setup_skill = lambda: False

    source = SessionSource(
        platform=Platform.JUHE,
        chat_id="R:2001",
        chat_type="group",
        user_id="1001",
        user_name="Alice",
    )
    event = MessageEvent(
        text="[Alice] 看下这个营业执照",
        message_type=MessageType.TEXT,
        source=source,
        media_urls=["juhe://attachment/test/license.jpg"],
        media_types=["image/jpeg"],
    )
    setattr(
        event,
        "_juhe_media_access_urls",
        ["http://124.220.81.138:9000/wework/wwcdn/private/license.jpg?X-Amz-Signature=1"],
    )
    setattr(
        event,
        "_juhe_attachment_identities",
        [
            {
                "bucket": "wework",
                "object_key": "wwcdn/private/license.jpg",
                "object_url": "http://124.220.81.138:9000/wework/wwcdn/private/license.jpg",
                "file_id": "file-license",
                "file_md5": "md5-license",
            }
        ],
    )

    with patch(
        "tools.vision_tools.vision_analyze_tool",
        new=AsyncMock(side_effect=AssertionError("vision_analyze_tool should not run on Juhe image cache hit")),
    ):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result is not None
    assert "营业执照 OCR 内容" in result
    assert getattr(event, "_juhe_media_descriptions") == [
        "图片 license.jpg：营业执照 OCR 内容"
    ]


@pytest.mark.asyncio
async def test_prepare_inbound_message_text_stores_juhe_document_parse_cache(tmp_path):
    from gateway.run import GatewayRunner
    from gateway.document_text import RemoteDocumentAgentContext

    db = SessionDB(db_path=tmp_path / "state.db")

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._session_db = db
    runner._has_setup_skill = lambda: False

    source = SessionSource(
        platform=Platform.JUHE,
        chat_id="R:2001",
        chat_type="group",
        user_id="1001",
        user_name="Alice",
    )
    event = MessageEvent(
        text="[Alice] 帮我识别这个 PDF",
        message_type=MessageType.DOCUMENT,
        source=source,
        media_urls=["juhe://attachment/test/license.pdf"],
        media_types=["application/pdf"],
    )
    setattr(
        event,
        "_juhe_media_access_urls",
        ["http://124.220.81.138:9000/wework/wwcdn/private/license.pdf?X-Amz-Signature=1"],
    )
    setattr(
        event,
        "_juhe_attachment_identities",
        [
            {
                "bucket": "wework",
                "object_key": "wwcdn/private/license.pdf",
                "object_url": "http://124.220.81.138:9000/wework/wwcdn/private/license.pdf",
                "file_id": "file-license",
                "file_md5": "md5-license",
            }
        ],
    )

    with patch(
        "gateway.document_text.build_remote_document_context_for_agent",
        new=AsyncMock(
            return_value=RemoteDocumentAgentContext(
                display_name="license.pdf",
                extracted_text="首次远程识别内容",
                display_description="文件 license.pdf：首次远程识别内容",
                agent_context="文件 license.pdf：首次远程识别内容\n\n[Content of license.pdf]:\n首次远程识别内容",
                parser="dashscope_doc_url",
                from_cache=False,
            )
        ),
    ) as remote_doc_mock:
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result is not None
    assert "首次远程识别内容" in result
    remote_doc_mock.assert_awaited_once()
    assert getattr(event, "_juhe_media_descriptions") == [
        "文件 license.pdf：首次远程识别内容"
    ]


@pytest.mark.asyncio
async def test_prepare_inbound_message_text_reports_juhe_document_remote_failure_without_local_fallback():
    from gateway.run import GatewayRunner
    from gateway.document_text import RemoteDocumentAgentContext

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._session_db = None
    runner._has_setup_skill = lambda: False

    source = SessionSource(
        platform=Platform.JUHE,
        chat_id="R:2001",
        chat_type="group",
        user_id="1001",
        user_name="Alice",
    )
    event = MessageEvent(
        text="[Alice] 这个 PDF 现在能读吗",
        message_type=MessageType.DOCUMENT,
        source=source,
        media_urls=["juhe://attachment/test/license.pdf"],
        media_types=["application/pdf"],
    )
    setattr(
        event,
        "_juhe_media_access_urls",
        ["http://124.220.81.138:9000/wework/wwcdn/private/license.pdf?X-Amz-Signature=1"],
    )
    setattr(
        event,
        "_juhe_attachment_identities",
        [
            {
                "bucket": "wework",
                "object_key": "wwcdn/private/license.pdf",
                "object_url": "http://124.220.81.138:9000/wework/wwcdn/private/license.pdf",
            }
        ],
    )

    with patch(
        "gateway.document_text.build_remote_document_context_for_agent",
        new=AsyncMock(
            return_value=RemoteDocumentAgentContext(
                display_name="license.pdf",
                extracted_text="",
                display_description="文件 license.pdf：已收到，但暂未提取到正文",
                agent_context="[The user sent a document: 'license.pdf'. Hermes couldn't extract readable text from the remote document URL, so only the file metadata is available.]",
                parser="dashscope_doc_url",
                from_cache=False,
            )
        ),
    ), patch(
        "gateway.document_text.extract_local_document_text",
        side_effect=AssertionError("local fallback should not be used for Juhe document URLs"),
    ):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result is not None
    assert "license.pdf" in result
    assert "couldn't extract" in result.lower()
    assert getattr(event, "_juhe_media_descriptions") == [
        "文件 license.pdf：已收到，但暂未提取到正文"
    ]


@pytest.mark.asyncio
async def test_build_remote_document_context_for_agent_retries_when_cache_is_pending(tmp_path):
    from gateway.document_text import ExtractedDocumentText, build_remote_document_context_for_agent

    db = SessionDB(db_path=tmp_path / "state.db")
    db.upsert_juhe_attachment_parse_cache(
        platform="juhe",
        bucket="wework",
        object_key="wwcdn/private/license.pdf",
        object_url="http://124.220.81.138:9000/wework/wwcdn/private/license.pdf",
        file_id="file-license",
        file_md5="md5-license",
        media_type="application/pdf",
        file_name="license.pdf",
        parser="preheat_queue",
        extracted_text="",
        content_kind="document",
        content_format="markdown",
        display_description="文件 license.pdf：已收到，后台正在解析",
        parse_status="pending",
    )

    with patch(
        "gateway.document_text._download_document_to_temp_path",
        new=AsyncMock(return_value=None),
    ), patch(
        "gateway.document_text._extract_document_text_via_mineru_lightweight_url",
        new=AsyncMock(
            return_value=ExtractedDocumentText(
                display_name="license.pdf",
                text="MinerU 补齐的正文内容",
            )
        ),
    ) as mineru_mock:
        bundle = await build_remote_document_context_for_agent(
            media_ref="juhe://attachment/test/license.pdf",
            document_url="https://example.com/license.pdf",
            media_type="application/pdf",
            display_name="license.pdf",
            session_db=db,
            cache_identity={
                "bucket": "wework",
                "object_key": "wwcdn/private/license.pdf",
                "object_url": "http://124.220.81.138:9000/wework/wwcdn/private/license.pdf",
                "file_id": "file-license",
                "file_md5": "md5-license",
            },
        )

    mineru_mock.assert_awaited_once()
    assert bundle.display_description == "文件 license.pdf：MinerU 补齐的正文内容"
    assert "MinerU 补齐的正文内容" in bundle.agent_context


def test_build_media_placeholder_does_not_leak_juhe_signed_urls():
    from gateway.run import _build_media_placeholder

    source = SessionSource(
        platform=Platform.JUHE,
        chat_id="R:2001",
        chat_type="group",
        user_id="1001",
        user_name="Alice",
    )
    event = MessageEvent(
        text="",
        message_type=MessageType.DOCUMENT,
        source=source,
        media_urls=["juhe://attachment/test/license.pdf"],
        media_types=["application/pdf"],
    )
    setattr(
        event,
        "_juhe_media_access_urls",
        ["http://124.220.81.138:9000/wework/wwcdn/private/license.pdf?X-Amz-Signature=1"],
    )
    setattr(
        event,
        "_juhe_media_descriptions",
        ["文件 license.pdf：营业执照扫描件，包含统一社会信用代码等信息"],
    )

    placeholder = _build_media_placeholder(event)

    assert "营业执照扫描件" in placeholder
    assert "juhe://attachment/test/license.pdf" not in placeholder
    assert "X-Amz-Signature" not in placeholder


def test_pending_event_preview_text_prefers_juhe_media_description_over_generic_notice():
    from gateway.run import _pending_event_preview_text

    source = SessionSource(
        platform=Platform.JUHE,
        chat_id="R:2001",
        chat_type="group",
        user_id="1001",
        user_name="Alice",
    )
    event = MessageEvent(
        text="[Received document: license.pdf]",
        message_type=MessageType.DOCUMENT,
        source=source,
        media_urls=["juhe://attachment/test/license.pdf"],
        media_types=["application/pdf"],
    )
    setattr(event, "_juhe_media_descriptions", ["文件 license.pdf：营业执照扫描件"])

    preview = _pending_event_preview_text(event)

    assert preview == "文件 license.pdf：营业执照扫描件"


def test_augment_agent_message_with_platform_context_includes_current_juhe_attachment_bridge_without_leaking_urls():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.JUHE,
        chat_id="R:2001",
        chat_type="group",
        user_id="1001",
        user_name="Alice",
    )
    event = MessageEvent(
        text="第一个是品牌 logo 第二个是证书 logo",
        message_type=MessageType.TEXT,
        source=source,
        media_urls=[
            "juhe://attachment/test/brand-logo.jpg",
            "juhe://attachment/test/certificate-logo.jpg",
        ],
        media_types=["image/jpeg", "image/jpeg"],
    )
    setattr(
        event,
        "_juhe_media_access_urls",
        [
            "http://124.220.81.138:9000/wework/wwcdn/private/brand-logo.jpg?X-Amz-Signature=1",
            "http://124.220.81.138:9000/wework/wwcdn/private/certificate-logo.jpg?X-Amz-Signature=2",
        ],
    )
    setattr(
        event,
        "_juhe_media_descriptions",
        [
            "图片 brand-logo.jpg：蓝底品牌 logo",
            "图片 certificate-logo.jpg：白底证书 logo",
        ],
    )

    result = runner._augment_agent_message_with_platform_context(
        event=event,
        message_text="请直接拿这两张图继续处理",
    )

    assert "Current Juhe attachments" in result
    assert "list_current_attachments" in result
    assert "upload_current_attachment_to_certificate" in result
    assert "图片 brand-logo.jpg：蓝底品牌 logo" in result
    assert "X-Amz-Signature" not in result


def test_augment_agent_message_with_platform_context_orders_juhe_recall_layers():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.JUHE,
        chat_id="R:2001",
        chat_type="group",
        user_id="1001",
        user_name="Alice",
    )
    event = MessageEvent(
        text="之前有和你说过 logo 的事吗",
        message_type=MessageType.TEXT,
        source=source,
    )
    setattr(event, "_juhe_room_memory_text", "# MEMORY\n- 群里长期使用客户确认后的品牌素材。")
    setattr(event, "_juhe_pending_context_text", "2026-04-21 10:00 | Bob: 这次先确认 logo。")
    setattr(event, "_juhe_relevant_room_history_text", "2026-04-18 09:00 | Alice | [Room history] 品牌 logo 需要更换。")
    setattr(event, "_juhe_relevant_prior_agent_turns_text", "2026-04-18 09:05 | assistant | [Prior agent turns] 我会按新 logo 处理。")

    result = runner._augment_agent_message_with_platform_context(
        event=event,
        message_text="之前有和你说过 logo 的事吗",
    )

    room_memory_idx = result.index("[Room memory]")
    recent_idx = result.index("[Recent room context]")
    history_idx = result.index("[Relevant room history]")
    prior_idx = result.index("[Relevant prior agent turns]")
    current_idx = result.rindex("之前有和你说过 logo 的事吗")

    assert room_memory_idx < recent_idx < history_idx < prior_idx < current_idx
