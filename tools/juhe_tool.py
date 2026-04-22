"""Juhe-specific directory and conversation control tool."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

import httpx

import gateway.config as gateway_config
from gateway.config import Platform
from gateway.juhe_cache import JuheCacheStore
from gateway.platforms.juhe import JuheAdapter
from gateway.session_context import get_session_env
from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from tools.registry import registry


JUHE_TOOL_SCHEMA = {
    "name": "juhe_tool",
    "description": (
        "Juhe-specific enterprise WeChat operations. Use this for Juhe directory lookups "
        "(contacts, rooms, tags), Juhe-only conversation controls like group @, quote, revoke, and read state, "
        "and current inbound Juhe attachment bridging. `list_current_attachments` inspects the current message's "
        "attachments without exposing hidden access URLs, and `materialize_current_attachment` exports one current "
        "attachment to a temporary local file when another workflow needs to upload or reuse it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "list_contacts",
                    "search_contacts",
                    "list_rooms",
                    "get_room_detail",
                    "list_room_members",
                    "list_tags",
                    "get_contact_tags",
                    "get_room_history",
                    "search_room_history",
                    "send_room_at",
                    "send_quote",
                    "revoke_message",
                    "mark_read",
                    "confirm_internal_read",
                    "list_current_attachments",
                    "materialize_current_attachment",
                    "upload_current_attachment_to_certificate",
                ],
            },
            "query": {"type": "string"},
            "contact_id": {"type": "string"},
            "room_id": {"type": "string"},
            "conversation_id": {"type": "string"},
            "before_message_id": {"type": "string"},
            "limit": {"type": "integer"},
            "message_id": {"type": "string"},
            "appinfo": {"type": "string"},
            "content": {"type": "string"},
            "quote": {"type": "string"},
            "at_list": {"type": "array", "items": {"type": ["string", "integer"]}},
            "message_type": {"type": "integer"},
            "sender": {"type": "string"},
            "sender_name": {"type": "string"},
            "receiver": {"type": "string"},
            "roomid": {"type": "string"},
            "msgid": {"type": "string"},
            "index": {
                "type": "integer",
                "description": "Zero-based attachment index for the current Juhe message context.",
            },
            "position": {
                "type": "integer",
                "description": "One-based attachment position for the current Juhe message context. Prefer this when the user says things like 'the first image' or 'the second file'.",
            },
            "filename": {"type": "string"},
            "title": {"type": "string"},
        },
        "required": ["action"],
    },
}


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _strip_prefix(value: str) -> str:
    text = str(value or "").strip()
    if text.upper().startswith(("S:", "R:")):
        return text[2:]
    return text


def _require_enabled_juhe() -> JuheAdapter:
    config = gateway_config.load_gateway_config()
    platform_config = config.platforms.get(Platform.JUHE)
    if not platform_config or not platform_config.enabled:
        raise RuntimeError("Juhe platform is not configured")
    return JuheAdapter(platform_config)


def _check_juhe_tool() -> bool:
    try:
        if get_session_env("HERMES_SESSION_PLATFORM", "") == "juhe":
            return True
        from gateway.status import is_gateway_running

        config = gateway_config.load_gateway_config()
        platform_config = config.platforms.get(Platform.JUHE)
        return bool(platform_config and platform_config.enabled and is_gateway_running())
    except Exception:
        return False


def _load_current_media_manifest() -> list[dict[str, Any]]:
    raw = str(get_session_env("HERMES_SESSION_MEDIA_MANIFEST", "") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _select_current_attachment(args: dict[str, Any], manifest: list[dict[str, Any]]) -> dict[str, Any]:
    if not manifest:
        raise RuntimeError("No current Juhe attachments are available in this message context")

    if args.get("position") is not None:
        try:
            position = int(args.get("position"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("position must be an integer") from exc
        if position < 1 or position > len(manifest):
            raise RuntimeError(f"position must be between 1 and {len(manifest)}")
        return manifest[position - 1]

    if args.get("index") is not None:
        try:
            index = int(args.get("index"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("index must be an integer") from exc
        if index < 0 or index >= len(manifest):
            raise RuntimeError(f"index must be between 0 and {len(manifest) - 1}")
        return manifest[index]

    if len(manifest) == 1:
        return manifest[0]
    raise RuntimeError("Multiple current Juhe attachments are available; specify position or index")


async def _download_attachment_bytes(access_url: str) -> bytes:
    async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
        response = await client.get(access_url)
        response.raise_for_status()
        return response.content


async def _materialize_current_attachment(record: dict[str, Any]) -> str:
    access_url = str(record.get("access_url") or "").strip()
    media_ref = str(record.get("media_ref") or "").strip()
    display_name = Path(str(record.get("display_name") or "").strip() or "attachment").name or "attachment"

    if media_ref and not media_ref.startswith("juhe://") and os.path.exists(media_ref):
        return media_ref
    if access_url and os.path.exists(access_url):
        return access_url
    if not access_url:
        raise RuntimeError("No downloadable access URL is available for this attachment")

    session_key = str(get_session_env("HERMES_SESSION_KEY", "") or "").strip() or "default"
    materialized_dir = get_hermes_home() / "tmp" / "juhe-current-attachments" / session_key
    materialized_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="attachment-",
        suffix=f"-{display_name}",
        dir=materialized_dir,
        delete=False,
    ) as handle:
        handle.write(await _download_attachment_bytes(access_url))
        return handle.name


def _run_certificate_workflow_cli(command: str, args: list[str]) -> dict[str, Any]:
    cli_path = Path("/Users/chou/code/certificate/tools/certificate-workflow-cli/certificate-workflow.js")
    if not cli_path.exists():
        raise RuntimeError(f"Certificate workflow CLI not found: {cli_path}")

    completed = subprocess.run(
        ["node", str(cli_path), command, *args, "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(stderr or f"certificate workflow CLI failed with exit code {completed.returncode}")
    try:
        return json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("certificate workflow CLI returned invalid JSON") from exc


def _extract_certificate_asset_id(payload: Any) -> int | None:
    candidates: list[Any] = []
    if isinstance(payload, dict):
        candidates.extend(
            [
                payload.get("id"),
                payload.get("asset_id"),
                payload.get("resource_id"),
            ]
        )
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.extend([data.get("id"), data.get("asset_id"), data.get("resource_id")])
            resource = data.get("resource")
            if isinstance(resource, dict):
                candidates.extend([resource.get("id"), resource.get("asset_id")])
    for value in candidates:
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def juhe_tool(args: dict[str, Any], **_kw: Any) -> str:
    action = str(args.get("action") or "").strip()
    if not action:
        return _error("action is required")

    if action == "list_current_attachments":
        manifest = _load_current_media_manifest()
        attachments = []
        for item in manifest:
            attachments.append(
                {
                    "index": int(item.get("index") or 0),
                    "position": int(item.get("position") or (int(item.get("index") or 0) + 1)),
                    "display_name": str(item.get("display_name") or "").strip(),
                    "media_type": str(item.get("media_type") or "").strip(),
                    "description": str(item.get("description") or "").strip(),
                    "media_ref": str(item.get("media_ref") or "").strip(),
                }
            )
        return json.dumps({"attachments": attachments}, ensure_ascii=False)

    if action == "materialize_current_attachment":
        manifest = _load_current_media_manifest()
        try:
            record = _select_current_attachment(args, manifest)
            from model_tools import _run_async

            materialized_path = _run_async(_materialize_current_attachment(record))
        except Exception as exc:
            return _error(str(exc))
        return json.dumps(
            {
                "success": True,
                "path": materialized_path,
                "display_name": str(record.get("display_name") or "").strip(),
                "media_type": str(record.get("media_type") or "").strip(),
                "description": str(record.get("description") or "").strip(),
                "position": int(record.get("position") or (int(record.get("index") or 0) + 1)),
                "index": int(record.get("index") or 0),
            },
            ensure_ascii=False,
        )

    if action == "upload_current_attachment_to_certificate":
        manifest = _load_current_media_manifest()
        try:
            record = _select_current_attachment(args, manifest)
            access_url = str(record.get("access_url") or "").strip()
            if not access_url:
                raise RuntimeError("Current attachment has no reusable remote access URL for certificate upload")
            display_name = Path(
                str(args.get("filename") or record.get("display_name") or "attachment").strip() or "attachment"
            ).name or "attachment"
            cli_args = ["--url", access_url, "--filename", display_name]
            title = str(args.get("title") or "").strip()
            if title:
                cli_args.extend(["--title", title])
            upload_result = _run_certificate_workflow_cli("upload-file-from-url", cli_args)
            asset_id = _extract_certificate_asset_id(upload_result)
        except Exception as exc:
            return _error(str(exc))
        return json.dumps(
            {
                "success": True,
                "position": int(record.get("position") or (int(record.get("index") or 0) + 1)),
                "index": int(record.get("index") or 0),
                "display_name": display_name,
                "media_type": str(record.get("media_type") or "").strip(),
                "description": str(record.get("description") or "").strip(),
                "asset_id": asset_id,
                "upload": upload_result,
            },
            ensure_ascii=False,
        )

    try:
        adapter = _require_enabled_juhe()
    except Exception as exc:
        return _error(str(exc))

    store = JuheCacheStore()

    if action == "list_contacts":
        contacts = store.list_contacts()
        if not contacts:
            from model_tools import _run_async

            _run_async(adapter.refresh_contacts())
            contacts = store.list_contacts()
        return json.dumps({"contacts": contacts})

    if action == "search_contacts":
        query = str(args.get("query") or "").strip()
        if not query:
            return _error("query is required")
        results = store.search_contacts(query)
        if not results:
            from model_tools import _run_async

            _run_async(adapter.refresh_contacts())
            results = store.search_contacts(query)
        return json.dumps({"contacts": results})

    if action == "list_rooms":
        rooms = store.list_rooms()
        if not rooms:
            from model_tools import _run_async

            _run_async(adapter.refresh_rooms())
            rooms = store.list_rooms()
        return json.dumps({"rooms": rooms})

    if action == "get_room_detail":
        room_id = _strip_prefix(str(args.get("room_id") or "").strip())
        if not room_id:
            return _error("room_id is required")
        room = store.get_room(room_id)
        if room is None:
            from model_tools import _run_async

            _run_async(adapter.refresh_rooms())
            room = store.get_room(room_id)
        return json.dumps({"room": room})

    if action == "list_room_members":
        room_id = _strip_prefix(str(args.get("room_id") or "").strip())
        if not room_id:
            return _error("room_id is required")
        members = store.list_room_members(room_id)
        if not members:
            from model_tools import _run_async

            _run_async(adapter.refresh_rooms())
            members = store.list_room_members(room_id)
        return json.dumps({"members": members})

    if action == "list_tags":
        tags = store.list_tags()
        if not tags:
            from model_tools import _run_async

            _run_async(adapter.refresh_tags())
            tags = store.list_tags()
        return json.dumps({"tags": tags})

    if action == "get_contact_tags":
        contact_id = _strip_prefix(str(args.get("contact_id") or "").strip())
        if not contact_id:
            return _error("contact_id is required")
        tags = store.get_contact_tags(contact_id)
        if not tags:
            from model_tools import _run_async

            _run_async(adapter.refresh_contacts())
            _run_async(adapter.refresh_tags())
            tags = store.get_contact_tags(contact_id)
        return json.dumps({"tags": tags})

    if action == "get_room_history":
        conversation_id = str(args.get("conversation_id") or "").strip()
        if not conversation_id:
            return _error("conversation_id is required")
        limit = args.get("limit")
        try:
            limit_value = max(1, min(int(limit if limit is not None else 20), 100))
        except (TypeError, ValueError):
            return _error("limit must be an integer")
        db = SessionDB(db_path=get_hermes_home() / "state.db")
        try:
            messages = db.get_room_history(
                Platform.JUHE.value,
                conversation_id,
                before_message_id=str(args.get("before_message_id") or "").strip() or None,
                limit=limit_value,
            )
        finally:
            db.close()
        return json.dumps({"messages": messages}, ensure_ascii=False)

    if action == "search_room_history":
        conversation_id = str(args.get("conversation_id") or "").strip()
        if not conversation_id:
            return _error("conversation_id is required")
        query = str(args.get("query") or "").strip()
        if not query:
            return _error("query is required")
        limit = args.get("limit")
        try:
            limit_value = max(1, min(int(limit if limit is not None else 10), 50))
        except (TypeError, ValueError):
            return _error("limit must be an integer")
        db = SessionDB(db_path=get_hermes_home() / "state.db")
        try:
            messages = db.search_room_history(
                Platform.JUHE.value,
                conversation_id,
                query=query,
                limit=limit_value,
            )
        finally:
            db.close()
        return json.dumps({"messages": messages}, ensure_ascii=False)

    from model_tools import _run_async

    if action == "send_room_at":
        conversation_id = str(args.get("conversation_id") or "").strip()
        content = str(args.get("content") or "")
        at_list = args.get("at_list") or []
        return json.dumps(_run_async(adapter.send_room_at(conversation_id, content, at_list)))

    message_record = store.get_message(
        message_id=str(args.get("message_id") or "").strip() or None,
        appinfo=str(args.get("appinfo") or "").strip() or None,
    )

    if action == "send_quote":
        if message_record is None:
            return _error("message_id or appinfo must resolve to a cached Juhe message")
        content = str(args.get("content") or "")
        if not content:
            return _error("content is required")
        return json.dumps(
            _run_async(
                adapter.send_quote(
                    conversation_id=str(args.get("conversation_id") or message_record.get("conversation_id") or ""),
                    quote=str(args.get("quote") or message_record.get("text") or ""),
                    content=content,
                    appinfo=str(message_record.get("appinfo") or ""),
                    content_type=int(message_record.get("content_type") or 0),
                    sender=str(message_record.get("sender") or ""),
                    sender_name=str(
                        args.get("sender_name")
                        or message_record.get("sender_name")
                        or message_record.get("sender")
                        or ""
                    ),
                    message=dict(message_record.get("message") or {}),
                )
            )
        )

    if action == "revoke_message":
        if message_record is None:
            return _error("message_id or appinfo must resolve to a cached Juhe message")
        return json.dumps(
            _run_async(
                adapter.revoke_message(
                    str(args.get("conversation_id") or message_record.get("conversation_id") or ""),
                    str(args.get("msgid") or message_record.get("message_id") or ""),
                )
            )
        )

    if action == "mark_read":
        conversation_id = str(
            args.get("conversation_id") or (message_record or {}).get("conversation_id") or ""
        ).strip()
        if not conversation_id:
            return _error("conversation_id is required")
        return json.dumps(_run_async(adapter.mark_read(conversation_id)))

    if action == "confirm_internal_read":
        base = message_record or {}
        msgid = str(args.get("msgid") or base.get("message_id") or "").strip()
        sender = str(args.get("sender") or base.get("sender") or "").strip()
        receiver = str(args.get("receiver") or base.get("receiver") or "").strip()
        roomid = str(args.get("roomid") or base.get("roomid") or "").strip() or None
        message_type = args.get("message_type")
        if message_type is None:
            message_type = base.get("content_type")
        if message_type is None:
            return _error("message_type is required")
        return json.dumps(
            _run_async(
                adapter.confirm_internal_read(
                    message_type=int(message_type),
                    sender=sender,
                    receiver=receiver,
                    roomid=roomid,
                    msgid=msgid,
                )
            )
        )

    return _error(f"Unknown action: {action}")


registry.register(
    name="juhe_tool",
    toolset="messaging",
    schema=JUHE_TOOL_SCHEMA,
    handler=juhe_tool,
    check_fn=_check_juhe_tool,
    emoji="🧩",
)
