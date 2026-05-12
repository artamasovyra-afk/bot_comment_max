from __future__ import annotations

import importlib
import os
import sys

import pytest


@pytest.fixture
def bot_module(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-token")
    monkeypatch.setenv("SUPER_ADMIN_LOGIN", "owner")
    monkeypatch.setenv("SUPER_ADMIN_PASSWORD", "owner-password")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("MAX_DATABASE_PATH", "/tmp/max-comments-tests.sqlite3")
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
