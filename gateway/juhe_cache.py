"""Local runtime cache for Juhe contacts, rooms, tags, sync state, and messages."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from hermes_constants import get_hermes_home
from utils import atomic_json_write


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return json.loads(json.dumps(default))


def _clean_dict(value: dict[str, Any] | None) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


class JuheCacheStore:
    """File-backed cache for Juhe runtime state."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or (get_hermes_home() / "juhe")
        self.contacts_path = self.base_dir / "contacts.json"
        self.rooms_path = self.base_dir / "rooms.json"
        self.tags_path = self.base_dir / "tags.json"
        self.messages_path = self.base_dir / "messages.json"
        self.sync_state_path = self.base_dir / "sync_state.json"

    # ------------------------------------------------------------------
    # Generic state helpers
    # ------------------------------------------------------------------

    def load_sync_state(self) -> dict[str, Any]:
        return _load_json(
            self.sync_state_path,
            {
                "updated_at": None,
                "contacts_last_seq": "",
                "tags_seq": "",
                "message_sync_key": "",
                "room_versions": {},
            },
        )

    def update_sync_state(self, updates: dict[str, Any]) -> dict[str, Any]:
        state = self.load_sync_state()
        state.update(dict(updates))
        state["updated_at"] = _utcnow_iso()
        atomic_json_write(self.sync_state_path, state)
        return state

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    def load_contacts(self) -> dict[str, Any]:
        return _load_json(
            self.contacts_path,
            {"updated_at": None, "contacts": {}},
        )

    def upsert_contact(self, contact: dict[str, Any]) -> dict[str, Any] | None:
        user_id = str(contact.get("user_id") or contact.get("userid") or contact.get("username") or "").strip()
        if not user_id:
            return None

        contacts_doc = self.load_contacts()
        contacts = _clean_dict(contacts_doc.get("contacts"))
        existing = _clean_dict(contacts.get(user_id))
        extend_info = _clean_dict(existing.get("extend_info"))
        extend_info.update(_clean_dict(contact.get("extend_info")))

        merged = {**existing, **dict(contact)}
        merged["user_id"] = user_id
        if extend_info:
            merged["extend_info"] = extend_info
        contacts[user_id] = merged
        contacts_doc["contacts"] = contacts
        contacts_doc["updated_at"] = _utcnow_iso()
        atomic_json_write(self.contacts_path, contacts_doc)
        return merged

    def upsert_contacts(self, contacts: Iterable[dict[str, Any]], *, last_seq: str | None = None) -> None:
        for contact in contacts:
            if isinstance(contact, dict):
                self.upsert_contact(contact)
        if last_seq is not None:
            self.update_sync_state({"contacts_last_seq": str(last_seq)})

    def list_contacts(self) -> list[dict[str, Any]]:
        contacts = _clean_dict(self.load_contacts().get("contacts"))
        return sorted(
            contacts.values(),
            key=lambda item: (
                str(item.get("name") or item.get("extend_info", {}).get("remark") or item.get("user_id") or "").lower(),
                str(item.get("user_id") or ""),
            ),
        )

    def search_contacts(self, query: str) -> list[dict[str, Any]]:
        normalized = str(query or "").strip().lower()
        if not normalized:
            return self.list_contacts()

        matches: list[dict[str, Any]] = []
        for contact in self.list_contacts():
            extend_info = _clean_dict(contact.get("extend_info"))
            haystack = " ".join(
                [
                    str(contact.get("name") or ""),
                    str(contact.get("user_id") or ""),
                    str(extend_info.get("remark") or ""),
                    str(extend_info.get("company_remark") or ""),
                ]
            ).lower()
            if normalized in haystack:
                matches.append(contact)
        return matches

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------

    def load_tags(self) -> dict[str, Any]:
        return _load_json(
            self.tags_path,
            {"updated_at": None, "tags": {}},
        )

    def upsert_tag(self, tag: dict[str, Any]) -> dict[str, Any] | None:
        tag_id = str(tag.get("id") or tag.get("label_id") or "").strip()
        if not tag_id:
            return None

        tags_doc = self.load_tags()
        tags = _clean_dict(tags_doc.get("tags"))
        existing = _clean_dict(tags.get(tag_id))
        merged = {**existing, **dict(tag), "id": tag_id}
        tags[tag_id] = merged
        tags_doc["tags"] = tags
        tags_doc["updated_at"] = _utcnow_iso()
        atomic_json_write(self.tags_path, tags_doc)
        return merged

    def apply_label_items(
        self,
        label_items: Iterable[dict[str, Any]],
        *,
        seq: str | None = None,
    ) -> None:
        for item in label_items:
            if not isinstance(item, dict):
                continue
            label = _clean_dict(item.get("label"))
            tag_id = str(label.get("id") or "").strip()
            if not tag_id:
                continue
            if int(label.get("bDeleted") or 0) != 0:
                tags_doc = self.load_tags()
                tags = _clean_dict(tags_doc.get("tags"))
                tags.pop(tag_id, None)
                tags_doc["tags"] = tags
                tags_doc["updated_at"] = _utcnow_iso()
                atomic_json_write(self.tags_path, tags_doc)
                continue
            self.upsert_tag(label)
        if seq is not None:
            self.update_sync_state({"tags_seq": str(seq)})

    def list_tags(self) -> list[dict[str, Any]]:
        tags = _clean_dict(self.load_tags().get("tags"))
        return sorted(tags.values(), key=lambda item: (str(item.get("name") or "").lower(), str(item.get("id") or "")))

    def get_contact_tags(self, user_id: str) -> list[dict[str, Any]]:
        contacts = _clean_dict(self.load_contacts().get("contacts"))
        contact = _clean_dict(contacts.get(str(user_id or "").strip()))
        extend_info = _clean_dict(contact.get("extend_info"))
        label_info_list = extend_info.get("label_info_list")
        if not isinstance(label_info_list, list):
            return []

        tags_by_id = _clean_dict(self.load_tags().get("tags"))
        resolved: list[dict[str, Any]] = []
        for item in label_info_list:
            if not isinstance(item, dict):
                continue
            label_id = str(item.get("label_id") or item.get("id") or "").strip()
            if label_id and label_id in tags_by_id:
                resolved.append(tags_by_id[label_id])
                continue
            label_name = str(item.get("label_name") or item.get("name") or "").strip()
            if label_id or label_name:
                resolved.append({"id": label_id, "name": label_name})
        return resolved

    # ------------------------------------------------------------------
    # Rooms
    # ------------------------------------------------------------------

    def load_rooms(self) -> dict[str, Any]:
        return _load_json(
            self.rooms_path,
            {"updated_at": None, "rooms": {}, "room_members": {}},
        )

    def upsert_room(self, room: dict[str, Any]) -> dict[str, Any] | None:
        room_id = str(room.get("room_id") or room.get("roomid") or room.get("id") or "").strip()
        if not room_id:
            return None

        rooms_doc = self.load_rooms()
        rooms = _clean_dict(rooms_doc.get("rooms"))
        existing = _clean_dict(rooms.get(room_id))
        merged = {**existing, **dict(room)}
        merged["room_id"] = room_id
        if "roomname" not in merged and merged.get("name"):
            merged["roomname"] = merged["name"]
        rooms[room_id] = merged
        rooms_doc["rooms"] = rooms
        rooms_doc["updated_at"] = _utcnow_iso()
        atomic_json_write(self.rooms_path, rooms_doc)
        return merged

    def upsert_room_members(self, room_id: str, members: Iterable[dict[str, Any]]) -> None:
        normalized_room_id = str(room_id or "").strip()
        if not normalized_room_id:
            return

        rooms_doc = self.load_rooms()
        room_members = _clean_dict(rooms_doc.get("room_members"))
        room_members[normalized_room_id] = [dict(member) for member in members if isinstance(member, dict)]
        rooms_doc["room_members"] = room_members
        rooms_doc["updated_at"] = _utcnow_iso()
        atomic_json_write(self.rooms_path, rooms_doc)

    def list_rooms(self) -> list[dict[str, Any]]:
        rooms = _clean_dict(self.load_rooms().get("rooms"))
        return sorted(
            rooms.values(),
            key=lambda item: (str(item.get("roomname") or item.get("name") or item.get("room_id") or "").lower(), str(item.get("room_id") or "")),
        )

    def get_room(self, room_id: str) -> dict[str, Any] | None:
        rooms = _clean_dict(self.load_rooms().get("rooms"))
        normalized_room_id = str(room_id or "").strip()
        room = rooms.get(normalized_room_id)
        return dict(room) if isinstance(room, dict) else None

    def list_room_members(self, room_id: str) -> list[dict[str, Any]]:
        room_members = _clean_dict(self.load_rooms().get("room_members"))
        members = room_members.get(str(room_id or "").strip(), [])
        return [dict(member) for member in members if isinstance(member, dict)]

    # ------------------------------------------------------------------
    # Message index
    # ------------------------------------------------------------------

    def load_messages(self) -> dict[str, Any]:
        return _load_json(
            self.messages_path,
            {"updated_at": None, "messages": {}, "appinfo_index": {}},
        )

    def record_message(self, message: dict[str, Any], *, limit: int = 500) -> dict[str, Any] | None:
        message_id = str(message.get("message_id") or "").strip()
        appinfo = str(message.get("appinfo") or "").strip()
        if not message_id and not appinfo:
            return None

        cache_key = message_id or f"appinfo:{appinfo}"
        messages_doc = self.load_messages()
        messages = _clean_dict(messages_doc.get("messages"))
        appinfo_index = _clean_dict(messages_doc.get("appinfo_index"))
        existing = _clean_dict(messages.get(cache_key))
        merged = {**existing, **dict(message)}
        merged["message_id"] = message_id or str(existing.get("message_id") or cache_key)
        merged["recorded_at"] = _utcnow_iso()
        messages[cache_key] = merged
        if appinfo:
            appinfo_index[appinfo] = cache_key

        if len(messages) > limit:
            ordered = sorted(
                messages.items(),
                key=lambda item: str(item[1].get("recorded_at") or ""),
                reverse=True,
            )
            keep = dict(ordered[:limit])
            messages = keep
            appinfo_index = {
                key: value
                for key, value in appinfo_index.items()
                if value in keep
            }

        messages_doc["messages"] = messages
        messages_doc["appinfo_index"] = appinfo_index
        messages_doc["updated_at"] = _utcnow_iso()
        atomic_json_write(self.messages_path, messages_doc)
        return merged

    def get_message(
        self,
        *,
        message_id: str | None = None,
        appinfo: str | None = None,
    ) -> dict[str, Any] | None:
        messages_doc = self.load_messages()
        messages = _clean_dict(messages_doc.get("messages"))
        appinfo_index = _clean_dict(messages_doc.get("appinfo_index"))

        normalized_message_id = str(message_id or "").strip()
        if normalized_message_id:
            message = messages.get(normalized_message_id)
            if isinstance(message, dict):
                return dict(message)
            for item in messages.values():
                if isinstance(item, dict) and str(item.get("message_id") or "").strip() == normalized_message_id:
                    return dict(item)

        normalized_appinfo = str(appinfo or "").strip()
        if normalized_appinfo:
            cache_key = str(appinfo_index.get(normalized_appinfo) or "").strip()
            if cache_key:
                message = messages.get(cache_key)
                if isinstance(message, dict):
                    return dict(message)
            for item in messages.values():
                if isinstance(item, dict) and str(item.get("appinfo") or "").strip() == normalized_appinfo:
                    return dict(item)

        return None

    def was_message_delivered(self, message_id: str) -> bool:
        message = self.get_message(message_id=message_id)
        return bool(message and message.get("delivered"))
