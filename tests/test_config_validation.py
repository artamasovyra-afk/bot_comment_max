from __future__ import annotations

from config_validation import format_validation_report, validate_bot_comment_max_config


def test_missing_required_config_reports_field_names_only() -> None:
    result = validate_bot_comment_max_config({})
    report = format_validation_report(result)

    assert result.ok is False
    assert "MAX_BOT_TOKEN" in report
    assert "SUPER_ADMIN_LOGIN" in report
    assert "missing_required" in report
    assert "<missing>" in report


def test_placeholder_values_are_detected_without_raw_value_disclosure() -> None:
    result = validate_bot_comment_max_config(
        {
            "MAX_BOT_TOKEN": "placeholder-token",
            "SUPER_ADMIN_LOGIN": "bot_owner",
            "SUPER_ADMIN_PASSWORD": "change_me_strong_password",
            "ADMIN_SESSION_SECRET": "<secret>",
        }
    )
    report = format_validation_report(result)

    assert result.ok is False
    assert "placeholder_value" in report
    assert "placeholder-token" not in report
    assert "change_me_strong_password" not in report
    assert "<secret>" not in report


def test_secret_and_sensitive_values_are_redacted() -> None:
    result = validate_bot_comment_max_config(
        {
            "MAX_BOT_TOKEN": "placeholder-token",
            "SUPER_ADMIN_LOGIN": "bot_owner",
            "SUPER_ADMIN_PASSWORD": "placeholder-password",
            "ADMIN_SESSION_SECRET": "placeholder-secret",
            "MAX_DATABASE_PATH": "/tmp/example.sqlite3",
        }
    )

    assert result.redacted_values["MAX_BOT_TOKEN"] == "<redacted>"
    assert result.redacted_values["SUPER_ADMIN_PASSWORD"] == "<redacted>"
    assert result.redacted_values["ADMIN_SESSION_SECRET"] == "<redacted>"
    assert result.redacted_values["SUPER_ADMIN_LOGIN"] == "<set>"
    assert result.redacted_values["MAX_DATABASE_PATH"] == "<set>"


def test_webhook_mode_reports_missing_preflight_settings_without_blocking() -> None:
    result = validate_bot_comment_max_config(
        {
            "MAX_BOT_TOKEN": "placeholder-token",
            "SUPER_ADMIN_LOGIN": "bot_owner",
            "SUPER_ADMIN_PASSWORD": "placeholder-password",
            "ADMIN_SESSION_SECRET": "placeholder-secret",
            "MAX_DELIVERY_MODE": "webhook",
        }
    )
    report = format_validation_report(result)

    assert "missing_webhook_url" in report
    assert "missing_webhook_secret" in report
    assert "MAX_WEBHOOK_PUBLIC_URL" in report
    assert "MAX_WEBHOOK_SECRET" in report


def test_report_never_echoes_supplied_values() -> None:
    result = validate_bot_comment_max_config(
        {
            "MAX_BOT_TOKEN": "sample-token-value",
            "SUPER_ADMIN_LOGIN": "sample-owner",
            "SUPER_ADMIN_PASSWORD": "sample-password-value",
            "ADMIN_SESSION_SECRET": "sample-session-secret",
            "MAX_DELIVERY_MODE": "polling",
            "MAX_WEB_APP_PUBLIC_URL": "https://example.invalid/app",
        }
    )
    report = format_validation_report(result)

    assert "sample-token-value" not in report
    assert "sample-owner" not in report
    assert "sample-password-value" not in report
    assert "sample-session-secret" not in report
    assert "https://example.invalid/app" not in report
    assert "status: ok" in report
