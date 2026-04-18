"""Juhe-specific directory and conversation control tool."""

from __future__ import annotations

import json
from typing import Any

import gateway.config as gateway_config
from gateway.config import Platform
from gateway.juhe_cache import JuheCacheStore
from gateway.platforms.juhe import JuheAdapter
from tools.registry import registry


JUHE_TOOL_SCHEMA = {
    "name": "juhe_tool",
    "description": (
        "Juhe-specific enterprise WeChat operations. Use this for Juhe directory lookups "
        "(contacts, rooms, tags) and Juhe-only conversation controls like group @, quote, revoke, and read state."
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
                    "send_room_at",
                    "send_quote",
                    "revoke_message",
                    "mark_read",
                    "confirm_internal_read",
                ],
            },
            "query": {"type": "string"},
            "contact_id": {"type": "string"},
            "room_id": {"type": "string"},
            "conversation_id": {"type": "string"},
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
        from gateway.session_context import get_session_env

        if get_session_env("HERMES_SESSION_PLATFORM", "") == "juhe":
            return True
        from gateway.status import is_gateway_running

        config = gateway_config.load_gateway_config()
        platform_config = config.platforms.get(Platform.JUHE)
        return bool(platform_config and platform_config.enabled and is_gateway_running())
    except Exception:
        return False


def juhe_tool(args: dict[str, Any], **_kw: Any) -> str:
    action = str(args.get("action") or "").strip()
    if not action:
        return _error("action is required")

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
