from __future__ import annotations

import importlib
import os
import sys

import pytest


@pytest.fixture
def bot_module(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-token")
    monkeypatch.setenv("SUPER_ADMIN_LOGIN", "owner")
    monkeypatch.setenv("SUPER_ADMIN_PASSWORD", "owner-password")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("MAX_DATABASE_PATH", str(tmp_path / "max-comments-tests.sqlite3"))
    sys.modules.pop("config", None)
    sys.modules.pop("bot", None)
    return importlib.import_module("bot")


def test_normalize_moderation_text_normalizes_case_spacing_and_repeated_chars(bot_module) -> None:
    assert bot_module.normalize_moderation_text("  ПРииивЕЕЕт   ЁЖ   ") == "приивеет еж"
    assert bot_module.normalize_moderation_text("coooool") == "cool"
    assert bot_module.normalize_moderation_text("`Test`") == "'test'"


def test_check_comment_text_for_taboo_blocks_exact_word(
    bot_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        bot_module,
        "TABOO_RULES_CACHE",
        {
            "exact_words": {"insult": {"badword"}},
            "phrases": {},
            "fragments": {},
            "allowlist": set(),
        },
    )

    assert bot_module.check_comment_text_for_taboo("This BADWORD should be blocked") == {
        "blocked": True,
        "category": "insult",
    }


def test_check_comment_text_for_taboo_honors_allowlist(
    bot_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        bot_module,
        "TABOO_RULES_CACHE",
        {
            "exact_words": {"insult": {"allowme"}},
            "phrases": {},
            "fragments": {},
            "allowlist": {"allowme"},
        },
    )

    assert bot_module.check_comment_text_for_taboo("allowme is explicitly allowed") == {
        "blocked": False
    }


def test_ensure_comment_text_has_no_taboo_raises_blocked_error(
    bot_module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bot_module,
        "TABOO_RULES_CACHE",
        {
            "exact_words": {},
            "phrases": {"abuse": {"very bad phrase"}},
            "fragments": {},
            "allowlist": set(),
        },
    )

    with pytest.raises(bot_module.CommentBlockedError) as error:
        bot_module.ensure_comment_text_has_no_taboo("This is a very bad phrase inside the comment")

    assert error.value.category == "abuse"


def test_reaction_payload_marks_selected_and_filters_invalid_my_reaction(bot_module) -> None:
    store = object.__new__(bot_module.CommentStore)

    payload = bot_module.CommentStore.build_comment_reaction_payload(
        store,
        counts_by_emoji={"👍": 3, "❤️": 1, "🤖": 9},
        my_reaction="❤️",
    )
    assert payload["myReaction"] == "❤️"
    assert payload["reactions"] == [
        {"emoji": "👍", "count": 3, "selected": False},
        {"emoji": "❤️", "count": 1, "selected": True},
    ]

    invalid_payload = bot_module.CommentStore.build_comment_reaction_payload(
        store,
        counts_by_emoji={"😂": 2},
        my_reaction="🤖",
    )
    assert invalid_payload["myReaction"] is None
    assert invalid_payload["reactions"] == [
        {"emoji": "😂", "count": 2, "selected": False},
    ]


def test_password_hash_and_verify_roundtrip(bot_module) -> None:
    password_hash = bot_module.hash_admin_password("secret-password", salt="known-salt")

    assert password_hash.startswith("pbkdf2_sha256$")
    assert bot_module.verify_admin_password("secret-password", password_hash) is True
    assert bot_module.verify_admin_password("wrong-password", password_hash) is False
    assert bot_module.verify_admin_password("secret-password", "not-a-valid-hash") is False


def test_admin_session_token_roundtrip_and_tamper(
    bot_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = object.__new__(bot_module.MaxCommentsBot)

    monkeypatch.setattr(bot_module, "ADMIN_SESSION_SECRET", "session-secret")
    monkeypatch.setattr(bot_module, "ADMIN_SESSION_MAX_AGE_SECONDS", 3600)
    monkeypatch.setattr(bot_module.time, "time", lambda: 1_700_000_000)

    app.admin_context_for_user = lambda user_id: {"user_id": user_id, "role": "channel_admin"}

    token = app.create_admin_session_token("42")
    assert app.admin_context_from_session_token(token) == {
        "user_id": 42,
        "role": "channel_admin",
    }

    tampered_token = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert app.admin_context_from_session_token(tampered_token) is None

    monkeypatch.setattr(bot_module.time, "time", lambda: 1_700_000_000 + 3601)
    assert app.admin_context_from_session_token(token) is None


def test_resolve_message_user_id_prefers_sender_without_chat_lookup(bot_module) -> None:
    app = object.__new__(bot_module.MaxCommentsBot)
    app.bot_info = {"user_id": 999}
    app.dialog_user_cache = {}

    class FakeApi:
        def get_chat(self, chat_id: int) -> dict[str, object]:
            raise AssertionError("get_chat should not be called when sender.user_id is present")

    app.api = FakeApi()

    message = {
        "sender": {"user_id": 42},
        "recipient": {"chat_type": "dialog", "chat_id": 555, "user_id": 999},
    }

    assert app.resolve_message_user_id(message) == 42


def test_resolve_message_user_id_falls_back_to_dialog_with_user(bot_module) -> None:
    app = object.__new__(bot_module.MaxCommentsBot)
    app.bot_info = {"user_id": 999}
    app.dialog_user_cache = {}

    class FakeApi:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def get_chat(self, chat_id: int) -> dict[str, object]:
            self.calls.append(chat_id)
            return {"dialog_with_user": {"user_id": 4242}}

    fake_api = FakeApi()
    app.api = fake_api

    message = {
        "recipient": {"chat_type": "dialog", "chat_id": 555, "user_id": 999},
        "link": {"type": "forward", "chat_id": -74631532033454},
    }

    assert app.resolve_message_user_id(message) == 4242
    assert app.resolve_message_user_id(message) == 4242
    assert fake_api.calls == [555]


def test_handle_new_message_uses_resolved_dialog_user_for_forwarded_channel_request(
    bot_module,
) -> None:
    app = object.__new__(bot_module.MaxCommentsBot)
    app.bot_info = {"user_id": 999}
    app.dialog_user_cache = {}

    class FakeApi:
        def get_chat(self, chat_id: int) -> dict[str, object]:
            return {"dialog_with_user": {"user_id": 4242}}

    app.api = FakeApi()
    app.is_own_message = lambda sender: False
    app.maybe_auto_attach_channel_post = lambda message: False
    app.message_text_or_payload = lambda message: ""
    app.handle_senderless_channel_command = lambda message, text: False
    app.forward_payload_has_channel_hint = lambda message: True

    captured: dict[str, int] = {}

    def fake_handle_forwarded(message: dict[str, object], user_id: int) -> bool:
        captured["user_id"] = user_id
        return True

    app.handle_forwarded_channel_post_request = fake_handle_forwarded

    message = {
        "recipient": {"chat_type": "dialog", "chat_id": 555, "user_id": 999},
        "link": {"type": "forward", "chat_id": -74631532033454},
    }

    app.handle_new_message(message)

    assert captured == {"user_id": 4242}


def test_handle_update_bot_started_sends_terms_for_new_user(bot_module) -> None:
    app = object.__new__(bot_module.MaxCommentsBot)
    app.store = object()

    captured: list[int] = []

    class FakeStore:
        def has_accepted_terms(self, *, max_user_id: int, version: str) -> bool:
            return False

    app.store = FakeStore()
    app.send_terms_welcome = lambda user_id: captured.append(user_id)
    app.send_connection_instruction = lambda user_id: (_ for _ in ()).throw(
        AssertionError("instructions should not be sent before terms are accepted")
    )

    app.handle_update({"update_type": "bot_started", "user": {"user_id": 101}})

    assert captured == [101]


def test_admin_delete_user_soft_deletes_and_clears_channel_links(bot_module, tmp_path) -> None:
    store = bot_module.CommentStore(str(tmp_path / "admin-delete.sqlite3"))
    admin_user = store.ensure_admin_user(
        max_user_id="424242",
        role=bot_module.ROLE_CHANNEL_ADMIN,
        password="password",
        must_change_password=False,
        is_active=True,
    )
    store.add_channel_admin(user_id=424242, channel_id=-1001)
    store.add_channel_admin(user_id=424242, channel_id=-1002)

    app = object.__new__(bot_module.MaxCommentsBot)
    app.store = store

    context = bot_module.AdminContext(
        user_id=9001,
        role=bot_module.ROLE_SUPER_ADMIN,
        channel_ids=set(),
        admin_user_id=9001,
    )

    result = app.admin_delete_user(context, admin_user_id=int(admin_user["id"]))

    assert result == {
        "ok": True,
        "deletedAdminUserId": int(admin_user["id"]),
        "deletedMaxUserId": "424242",
        "removedChannelLinks": 2,
        "removedSessions": 0,
        "softDelete": True,
    }
    deleted_row = store.get_admin_user_by_id(int(admin_user["id"]))
    assert deleted_row is not None
    assert int(deleted_row["is_active"]) == 0
    assert int(deleted_row["must_change_password"]) == 1
    assert store.list_channel_admin_channel_ids(424242) == set()
    assert app.admin_context_for_user(424242) is None

    audit_row = store.conn.execute(
        """
        SELECT action, entity_type, entity_id, payload
        FROM admin_audit_log
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert audit_row is not None
    assert audit_row["action"] == "delete_admin_user"
    assert audit_row["entity_type"] == "admin_user"
    assert audit_row["entity_id"] == str(int(admin_user["id"]))
    assert '"removedChannelLinks": 2' in audit_row["payload"]


def test_admin_delete_user_rejects_last_active_super_admin(bot_module, tmp_path) -> None:
    store = bot_module.CommentStore(str(tmp_path / "last-super-admin.sqlite3"))
    super_admin = store.ensure_admin_user(
        max_user_id="777",
        role=bot_module.ROLE_SUPER_ADMIN,
        password="password",
        must_change_password=False,
        is_active=True,
    )

    app = object.__new__(bot_module.MaxCommentsBot)
    app.store = store

    context = bot_module.AdminContext(
        user_id=9001,
        role=bot_module.ROLE_SUPER_ADMIN,
        channel_ids=set(),
        admin_user_id=9001,
    )

    with pytest.raises(bot_module.MaxApiError, match="LAST_SUPER_ADMIN"):
        app.admin_delete_user(context, admin_user_id=int(super_admin["id"]))

    reloaded = store.get_admin_user_by_id(int(super_admin["id"]))
    assert reloaded is not None
    assert int(reloaded["is_active"]) == 1


def test_admin_list_users_enriches_missing_profile_from_channel_member_lookup(
    bot_module, tmp_path
) -> None:
    store = bot_module.CommentStore(str(tmp_path / "admin-profiles.sqlite3"))
    store.ensure_admin_user(
        max_user_id="424242",
        role=bot_module.ROLE_CHANNEL_ADMIN,
        password="password",
        must_change_password=False,
        is_active=True,
    )
    store.add_channel_admin(user_id=424242, channel_id=-1001)

    app = object.__new__(bot_module.MaxCommentsBot)
    app.store = store

    class FakeApi:
        def get_chat_members(
            self, chat_id: int, *, user_ids: list[int] | None = None
        ) -> list[dict[str, object]]:
            assert chat_id == -1001
            assert user_ids == [424242]
            return [
                {
                    "user_id": 424242,
                    "first_name": "Иван",
                    "last_name": "Петров",
                    "username": "ivan_petrov",
                }
            ]

    app.api = FakeApi()

    context = bot_module.AdminContext(
        user_id=9001,
        role=bot_module.ROLE_SUPER_ADMIN,
        channel_ids=set(),
        admin_user_id=9001,
    )

    payload = app.admin_list_users(context)

    assert payload["ok"] is True
    assert payload["users"][0]["first_name"] == "Иван"
    assert payload["users"][0]["last_name"] == "Петров"
    assert payload["users"][0]["display_name"] == "Иван Петров"
    assert payload["users"][0]["username"] == "ivan_petrov"
    assert payload["users"][0]["resolved_name"] == "Иван Петров"

    reloaded = store.get_admin_user_by_max_user_id("424242")
    assert reloaded is not None
    assert reloaded["first_name"] == "Иван"
    assert reloaded["last_name"] == "Петров"
    assert reloaded["display_name"] == "Иван Петров"
    assert reloaded["username"] == "ivan_petrov"


def test_requester_channel_admin_status_returns_unknown_when_max_api_cannot_verify(
    bot_module,
) -> None:
    app = object.__new__(bot_module.MaxCommentsBot)

    class FakeApi:
        def get_chat(self, chat_id: int) -> dict[str, object]:
            raise bot_module.MaxApiError("chat lookup failed")

        def get_chat_admins(self, chat_id: int) -> list[dict[str, object]]:
            raise bot_module.MaxApiError("admins lookup failed")

    app.api = FakeApi()

    status, note = app.requester_channel_admin_status(-74631532033454, 4242)

    assert status is None
    assert "Не удалось автоматически проверить" in note
