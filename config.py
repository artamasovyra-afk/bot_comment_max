from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def optional_env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def optional_int_env(name: str) -> int | None:
    value = optional_env(name)
    if value is None:
        return None
    return int(value)


def parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if item:
            ids.add(int(item))
    return ids


def parse_optional_admin_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    return parse_admin_ids(raw)


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = require_env("MAX_BOT_TOKEN")
MAX_API_BASE_URL = os.getenv("MAX_API_BASE_URL", "https://platform-api.max.ru").rstrip("/")

TARGET_CHANNEL_CHAT_ID = optional_int_env("MAX_CHANNEL_CHAT_ID")
COMMENTS_CHAT_ID = optional_int_env("MAX_COMMENTS_CHAT_ID")
COMMENTS_CHAT_URL = os.getenv("MAX_COMMENTS_CHAT_URL", "").strip()

ADMIN_USER_IDS = parse_optional_admin_ids(optional_env("MAX_ADMIN_USER_IDS"))
ADMIN_PANEL_TOKEN = optional_env("MAX_ADMIN_PANEL_TOKEN")

DATABASE_PATH = os.getenv("MAX_DATABASE_PATH", str(BASE_DIR / "data" / "max_comments.sqlite3"))
POLL_TIMEOUT_SECONDS = int(os.getenv("MAX_POLL_TIMEOUT_SECONDS", "30"))
POLL_LIMIT = int(os.getenv("MAX_POLL_LIMIT", "100"))

WEB_SERVER_ENABLED = os.getenv("MAX_WEB_SERVER_ENABLED", "1").strip() != "0"
WEB_SERVER_HOST = os.getenv("MAX_WEB_SERVER_HOST", "127.0.0.1").strip()
WEB_SERVER_PORT = int(os.getenv("MAX_WEB_SERVER_PORT", "8080"))
WEB_APP_PUBLIC_URL = os.getenv("MAX_WEB_APP_PUBLIC_URL", "").strip().rstrip("/")
WEB_APP_AUTH_MAX_AGE_SECONDS = int(os.getenv("MAX_WEB_APP_AUTH_MAX_AGE_SECONDS", "3600"))
CHANNEL_SYNC_INTERVAL_SECONDS = int(os.getenv("MAX_CHANNEL_SYNC_INTERVAL_SECONDS", "5"))

DELIVERY_MODE = os.getenv("MAX_DELIVERY_MODE", "polling").strip().lower() or "polling"
WEBHOOK_PATH = os.getenv("MAX_WEBHOOK_PATH", "/webhook").strip() or "/webhook"
if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = f"/{WEBHOOK_PATH}"
WEBHOOK_SECRET = optional_env("MAX_WEBHOOK_SECRET")
WEBHOOK_PUBLIC_URL = (
    optional_env("MAX_WEBHOOK_PUBLIC_URL")
    or (f"{WEB_APP_PUBLIC_URL}{WEBHOOK_PATH}" if WEB_APP_PUBLIC_URL else "")
)
