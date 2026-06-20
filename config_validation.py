from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


DEFAULT_PLACEHOLDERS = {
    "...",
    "<token>",
    "<secret>",
    "<password>",
    "<url>",
    "change-me",
    "change_me",
    "change_me_strong_password",
    "placeholder",
    "placeholder-token",
    "placeholder-secret",
    "your-token-here",
    "your-secret-here",
}


@dataclass(frozen=True)
class ConfigField:
    name: str
    required: bool = False
    secret: bool = False
    sensitive: bool = False
    allowed_values: tuple[str, ...] = ()
    placeholders: frozenset[str] = frozenset(DEFAULT_PLACEHOLDERS)


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    code: str
    severity: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    issues: tuple[ValidationIssue, ...]
    redacted_values: dict[str, str]


BOT_COMMENT_MAX_CONFIG_FIELDS: tuple[ConfigField, ...] = (
    ConfigField("MAX_BOT_TOKEN", required=True, secret=True),
    ConfigField("SUPER_ADMIN_LOGIN", required=True, sensitive=True),
    ConfigField("SUPER_ADMIN_PASSWORD", required=True, secret=True),
    ConfigField("ADMIN_SESSION_SECRET", required=True, secret=True),
    ConfigField("MAX_DATABASE_PATH", sensitive=True),
    ConfigField("MAX_DELIVERY_MODE", allowed_values=("polling", "webhook")),
    ConfigField("MAX_WEB_APP_PUBLIC_URL", sensitive=True),
    ConfigField("MAX_WEBHOOK_PUBLIC_URL", sensitive=True),
    ConfigField("MAX_WEBHOOK_SECRET", secret=True),
)


def is_placeholder_value(
    value: str,
    placeholders: frozenset[str] = frozenset(DEFAULT_PLACEHOLDERS),
) -> bool:
    normalized = value.strip().lower()
    return normalized in placeholders or (normalized.startswith("<") and normalized.endswith(">"))


def redact_value(value: str | None, *, secret: bool = False, sensitive: bool = False) -> str:
    if value is None:
        return "<missing>"
    if not value.strip():
        return "<empty>"
    if secret:
        return "<redacted>"
    if sensitive:
        return "<set>"
    return "<set>"


def validate_bot_comment_max_config(
    values: Mapping[str, str | None],
    fields: tuple[ConfigField, ...] = BOT_COMMENT_MAX_CONFIG_FIELDS,
) -> ValidationResult:
    issues: list[ValidationIssue] = []
    redacted_values: dict[str, str] = {}

    for field in fields:
        raw_value = values.get(field.name)
        redacted_values[field.name] = redact_value(
            raw_value,
            secret=field.secret,
            sensitive=field.sensitive,
        )

        if raw_value is None or not raw_value.strip():
            if field.required:
                issues.append(
                    ValidationIssue(
                        field=field.name,
                        code="missing_required",
                        severity="error",
                        message="Required configuration value is missing.",
                    )
                )
            continue

        normalized = raw_value.strip().lower()
        if is_placeholder_value(raw_value, field.placeholders):
            issues.append(
                ValidationIssue(
                    field=field.name,
                    code="placeholder_value",
                    severity="error" if field.required else "warning",
                    message="Configuration value still looks like a placeholder.",
                )
            )

        if field.allowed_values and normalized not in field.allowed_values:
            issues.append(
                ValidationIssue(
                    field=field.name,
                    code="invalid_choice",
                    severity="error",
                    message="Configuration value is outside the allowed set.",
                )
            )

    delivery_mode = (values.get("MAX_DELIVERY_MODE") or "").strip().lower() or "polling"
    webhook_public_url = (values.get("MAX_WEBHOOK_PUBLIC_URL") or "").strip()
    web_app_public_url = (values.get("MAX_WEB_APP_PUBLIC_URL") or "").strip()
    webhook_secret = (values.get("MAX_WEBHOOK_SECRET") or "").strip()

    if delivery_mode == "webhook" and not webhook_public_url and not web_app_public_url:
        issues.append(
            ValidationIssue(
                field="MAX_WEBHOOK_PUBLIC_URL",
                code="missing_webhook_url",
                severity="error",
                message="Webhook mode needs a public webhook URL or WebApp public URL.",
            )
        )

    if delivery_mode == "webhook" and not webhook_secret:
        issues.append(
            ValidationIssue(
                field="MAX_WEBHOOK_SECRET",
                code="missing_webhook_secret",
                severity="warning",
                message="Webhook mode is not secret-protected.",
            )
        )

    return ValidationResult(
        ok=not any(issue.severity == "error" for issue in issues),
        issues=tuple(issues),
        redacted_values=redacted_values,
    )


def format_validation_report(result: ValidationResult) -> str:
    lines = [
        "bot-comment-max configuration validation report",
        f"status: {'ok' if result.ok else 'needs-review'}",
        "values:",
    ]
    for name in sorted(result.redacted_values):
        lines.append(f"- {name}: {result.redacted_values[name]}")

    lines.append("issues:")
    if not result.issues:
        lines.append("- none")
    for issue in result.issues:
        lines.append(f"- {issue.severity} {issue.field} {issue.code}: {issue.message}")

    return "\n".join(lines)
