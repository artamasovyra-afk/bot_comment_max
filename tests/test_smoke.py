from __future__ import annotations

import os

os.environ.setdefault("MAX_BOT_TOKEN", "test-token")
os.environ.setdefault("SUPER_ADMIN_LOGIN", "owner")
os.environ.setdefault("SUPER_ADMIN_PASSWORD", "owner-password")
os.environ.setdefault("ADMIN_SESSION_SECRET", "test-secret")
os.environ.setdefault("MAX_DATABASE_PATH", "/tmp/max-comments-tests.sqlite3")

import pytest

import bot


def test_normalize_moderation_text_normalizes_case_spacing_and_repeated_chars() -> None:
    assert bot.normalize_moderation_text("  ПРииивЕЕЕт   ЁЖ   ") == "приивеет еж"
    assert bot.normalize_moderation_text("coooool") == "cool"
    assert bot.normalize_moderation_text("`Test`") == "'test'"


def test_check_comment_text_for_taboo_blocks_exact_word(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bot,
        "TABOO_RULES_CACHE",
        {
            "exact_words": {"insult": {"badword"}},
            "phrases": {},
            "fragments": {},
            "allowlist": set(),
        },
    )

    assert bot.check_comment_text_for_taboo("This BADWORD should be blocked") == {
        "blocked": True,
        "category": "insult",
    }


def test_check_comment_text_for_taboo_honors_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bot,
        "TABOO_RULES_CACHE",
        {
            "exact_words": {"insult": {"allowme"}},
            "phrases": {},
            "fragments": {},
            "allowlist": {"allowme"},
        },
    )

    assert bot.check_comment_text_for_taboo("allowme is explicitly allowed") == {"blocked": False}


def test_ensure_comment_text_has_no_taboo_raises_blocked_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bot,
        "TABOO_RULES_CACHE",
        {
            "exact_words": {},
            "phrases": {"abuse": {"very bad phrase"}},
            "fragments": {},
            "allowlist": set(),
        },
    )

    with pytest.raises(bot.CommentBlockedError) as error:
        bot.ensure_comment_text_has_no_taboo("This is a very bad phrase inside the comment")

    assert error.value.category == "abuse"


def test_reaction_payload_marks_selected_and_filters_invalid_my_reaction() -> None:
    store = object.__new__(bot.CommentStore)

    payload = bot.CommentStore.build_comment_reaction_payload(
        store,
        counts_by_emoji={"👍": 3, "❤️": 1, "🤖": 9},
        my_reaction="❤️",
    )
    assert payload["myReaction"] == "❤️"
    assert payload["reactions"] == [
        {"emoji": "👍", "count": 3, "selected": False},
        {"emoji": "❤️", "count": 1, "selected": True},
    ]

    invalid_payload = bot.CommentStore.build_comment_reaction_payload(
        store,
        counts_by_emoji={"😂": 2},
        my_reaction="🤖",
    )
    assert invalid_payload["myReaction"] is None
    assert invalid_payload["reactions"] == [
        {"emoji": "😂", "count": 2, "selected": False},
    ]


def test_password_hash_and_verify_roundtrip() -> None:
    password_hash = bot.hash_admin_password("secret-password", salt="known-salt")

    assert password_hash.startswith("pbkdf2_sha256$")
    assert bot.verify_admin_password("secret-password", password_hash) is True
    assert bot.verify_admin_password("wrong-password", password_hash) is False
    assert bot.verify_admin_password("secret-password", "not-a-valid-hash") is False


def test_admin_session_token_roundtrip_and_tamper(monkeypatch: pytest.MonkeyPatch) -> None:
    app = object.__new__(bot.MaxCommentsBot)

    monkeypatch.setattr(bot, "ADMIN_SESSION_SECRET", "session-secret")
    monkeypatch.setattr(bot, "ADMIN_SESSION_MAX_AGE_SECONDS", 3600)
    monkeypatch.setattr(bot.time, "time", lambda: 1_700_000_000)

    app.admin_context_for_user = lambda user_id: {"user_id": user_id, "role": "channel_admin"}

    token = app.create_admin_session_token("42")
    assert app.admin_context_from_session_token(token) == {
        "user_id": 42,
        "role": "channel_admin",
    }

    tampered_token = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert app.admin_context_from_session_token(tampered_token) is None

    monkeypatch.setattr(bot.time, "time", lambda: 1_700_000_000 + 3601)
    assert app.admin_context_from_session_token(token) is None
