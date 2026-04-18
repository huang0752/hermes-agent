"""Tests for Juhe local cache persistence and lookup helpers."""

from gateway.juhe_cache import JuheCacheStore


class TestJuheCacheStore:
    def test_persists_contacts_rooms_tags_messages_and_sync_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        store = JuheCacheStore()
        store.upsert_contact(
            {
                "user_id": "1001",
                "name": "Alice",
                "corp_id": "corp-1",
                "extend_info": {
                    "remark": "老板",
                    "label_info_list": [{"label_id": "tag-1", "label_name": "VIP"}],
                },
            }
        )
        store.upsert_room(
            {
                "room_id": "2001",
                "roomname": "Dev Group",
                "member_count": 2,
            }
        )
        store.upsert_room_members(
            "2001",
            [
                {"uin": "1001", "nickname": "Alice"},
                {"uin": "1002", "nickname": "Bob"},
            ],
        )
        store.upsert_tag({"id": "tag-1", "name": "VIP"})
        store.record_message(
            {
                "message_id": "msg-1",
                "appinfo": "appinfo-1",
                "conversation_id": "R:2001",
                "sender": "1001",
                "sender_name": "Alice",
                "content_type": 2,
                "message": {"msg_type": 2, "content": "hello"},
                "delivered": True,
            }
        )
        store.update_sync_state({"message_sync_key": "7272648", "contacts_last_seq": "42"})

        reloaded = JuheCacheStore()
        assert reloaded.search_contacts("ali")[0]["user_id"] == "1001"
        assert reloaded.list_rooms()[0]["room_id"] == "2001"
        assert reloaded.get_room("2001")["roomname"] == "Dev Group"
        assert reloaded.list_room_members("2001")[0]["uin"] == "1001"
        assert reloaded.get_contact_tags("1001")[0]["name"] == "VIP"
        assert reloaded.get_message(message_id="msg-1")["appinfo"] == "appinfo-1"
        assert reloaded.get_message(appinfo="appinfo-1")["message_id"] == "msg-1"
        assert reloaded.load_sync_state()["message_sync_key"] == "7272648"
