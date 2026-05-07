from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import queue
import re
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib import error, parse, request

from config import (
    ADMIN_SESSION_SECRET,
    BOT_TOKEN,
    CHANNEL_SYNC_INTERVAL_SECONDS,
    COMMENTS_CHAT_ID,
    COMMENTS_CHAT_URL,
    DATABASE_PATH,
    DELIVERY_MODE,
    MAX_API_BASE_URL,
    POLL_LIMIT,
    POLL_TIMEOUT_SECONDS,
    SUPER_ADMIN_IDS,
    SUPER_ADMIN_LOGIN,
    SUPER_ADMIN_PASSWORD,
    TARGET_CHANNEL_CHAT_ID,
    WEB_APP_AUTH_MAX_AGE_SECONDS,
    WEB_APP_PUBLIC_URL,
    WEB_SERVER_ENABLED,
    WEB_SERVER_HOST,
    WEB_SERVER_PORT,
    WEBHOOK_PATH,
    WEBHOOK_PUBLIC_URL,
    WEBHOOK_SECRET,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("max-comments-bot")

APP_ROOT_DIR = Path(__file__).resolve().parent
VERSION_FILE = APP_ROOT_DIR / "VERSION"
DATA_DIR = APP_ROOT_DIR / "data"
TABOO_WORDS_FILE = DATA_DIR / "taboo_words_ru_en_uk.json"
WEBAPP_DIR = APP_ROOT_DIR / "webapp"
ADMIN_DIR = APP_ROOT_DIR / "admin"
SUPER_ADMIN_DIR = APP_ROOT_DIR / "super-admin"
COMMENT_MEDIA_DIR = Path(DATABASE_PATH).resolve().parent / "comment_media"
COMMENT_IMAGE_MAX_BYTES = 8 * 1024 * 1024
COMMENT_IMAGE_DATA_URL_MAX_LENGTH = 16 * 1024 * 1024
COMMENT_LINK_RE = re.compile(
    r"(?i)(?:\b(?:https?://|ftp://|www\.)\S+|(?<!@)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}(?::\d{2,5})?(?:/[^\s]*)?)"
)
BIND_CHANNEL_CODE_TTL_SECONDS = 30 * 60
BIND_CHANNEL_CLEANUP_INTERVAL_SECONDS = 10 * 60
SUPPORTED_DELIVERY_MODES = {"polling", "webhook"}
WEBHOOK_UPDATE_TYPES = ["message_created"]
ADMIN_SESSION_COOKIE = "max_comments_admin"
SUPER_ADMIN_SESSION_COOKIE = "max_comments_super_admin"
ADMIN_SESSION_MAX_AGE_SECONDS = 24 * 60 * 60
PASSWORD_HASH_ITERATIONS = 260_000
COMMENT_BLOCKED_CODE = "COMMENT_BLOCKED"
COMMENT_BLOCKED_MESSAGE = "Комментарий содержит запрещённые выражения. Исправьте текст и попробуйте снова."
REPORT_CREATED_MESSAGE = "Жалоба отправлена. Администратор канала проверит комментарий."
REPORT_ALREADY_EXISTS_MESSAGE = "Вы уже отправляли жалобу на этот комментарий."
COMMENT_NOT_FOUND_MESSAGE = "Комментарий не найден."
INVALID_REACTION_MESSAGE = "Недопустимая реакция."
ACCESS_DENIED_MESSAGE = "У вас нет прав для управления этим каналом."
ADMIN_LOGIN_ACCESS_DENIED_MESSAGE = "У вас нет прав для входа в админку."
INVALID_CREDENTIALS_MESSAGE = "Неверный логин или пароль."
FORBIDDEN_MESSAGE = "Недостаточно прав для выполнения действия."
SUPER_ADMIN_NOT_CONFIGURED_MESSAGE = "Панель супер-администратора не настроена."
ROLE_USER = "user"
ROLE_CHANNEL_ADMIN = "channel_admin"
ROLE_SUPER_ADMIN = "super_admin"
ADMIN_ROLES = {ROLE_SUPER_ADMIN, ROLE_CHANNEL_ADMIN}
COMMENT_STATUS_ACTIVE = "active"
COMMENT_STATUS_DELETED = "deleted"
COMMENT_STATUS_HIDDEN = "hidden"
COMMENT_STATUSES = {COMMENT_STATUS_ACTIVE, COMMENT_STATUS_DELETED, COMMENT_STATUS_HIDDEN}
ALLOWED_COMMENT_REACTIONS = ["👍", "❤️", "😂", "🔥", "😮", "😢"]
ALLOWED_COMMENT_REACTION_SET = frozenset(ALLOWED_COMMENT_REACTIONS)
REPORT_REASONS = {"insult", "profanity", "threat", "spam", "hate", "other"}
REPORT_STATUSES = {"new", "in_review", "accepted", "rejected"}
CHANNEL_REQUEST_STATUS_PENDING = "pending"
CHANNEL_REQUEST_STATUS_APPROVED = "approved"
CHANNEL_REQUEST_STATUS_REJECTED = "rejected"
CHANNEL_REQUEST_STATUS_CANCELLED = "cancelled"
CHANNEL_REQUEST_STATUS_DUPLICATE = "duplicate"
CHANNEL_REQUEST_STATUSES = {
    CHANNEL_REQUEST_STATUS_PENDING,
    CHANNEL_REQUEST_STATUS_APPROVED,
    CHANNEL_REQUEST_STATUS_REJECTED,
    CHANNEL_REQUEST_STATUS_CANCELLED,
    CHANNEL_REQUEST_STATUS_DUPLICATE,
}
TERMS_VERSION = "2026-05-06"
WEBAPP_ONLY_COMMENTS_CHAT_ID = 0
MODERATION_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁёІіЇїЄєҐґ']+", re.UNICODE)
MODERATION_COMPACT_RE = re.compile(r"[^0-9A-Za-zА-Яа-яЁёІіЇїЄєҐґ']+", re.UNICODE)
MODERATION_REPEATED_CHAR_RE = re.compile(r"(.)\1{2,}", re.UNICODE)
TABOO_RULES_CACHE: dict[str, Any] | None = None


def read_app_version() -> str:
    try:
        version = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0-dev"
    return version or "0.0.0-dev"


APP_VERSION = read_app_version()


def is_configured_for_publishing() -> bool:
    return (
        TARGET_CHANNEL_CHAT_ID is not None
        and COMMENTS_CHAT_ID is not None
        and bool(SUPER_ADMIN_IDS)
    )


def uses_same_chat_for_posts_and_comments() -> bool:
    return (
        TARGET_CHANNEL_CHAT_ID is not None
        and COMMENTS_CHAT_ID is not None
        and TARGET_CHANNEL_CHAT_ID == COMMENTS_CHAT_ID
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def hash_admin_password(password: str, *, salt: str | None = None) -> str:
    normalized_password = safe_text(password)
    salt_value = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        normalized_password.encode("utf-8"),
        salt_value.encode("utf-8"),
        PASSWORD_HASH_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${salt_value}${digest}"


def verify_admin_password(password: str, stored_hash: str) -> bool:
    parts = safe_text(stored_hash).split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    _algorithm, raw_iterations, salt, expected_digest = parts
    try:
        iterations = int(raw_iterations)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        safe_text(password).encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return hmac.compare_digest(digest, expected_digest)


def snippet(text: str, limit: int = 120) -> str:
    text = " ".join(safe_text(text).split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def format_duration_compact(total_seconds: int) -> str:
    seconds = max(int(total_seconds), 0)
    if seconds < 60:
        return "< 1 мин"
    total_minutes = (seconds + 59) // 60
    hours, minutes = divmod(total_minutes, 60)
    if hours <= 0:
        return f"{total_minutes} мин"
    if minutes <= 0:
        return f"{hours} ч"
    return f"{hours} ч {minutes} мин"


def format_utc_timestamp(iso_value: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_value)
    except ValueError:
        return iso_value
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def contains_link_like_text(text: str) -> bool:
    return bool(COMMENT_LINK_RE.search(safe_text(text)))


def ensure_comment_text_has_no_links(text: str) -> None:
    if contains_link_like_text(text):
        raise MaxApiError("Links are not allowed in comments")


def normalize_moderation_text(value: Any) -> str:
    text = safe_text(value).lower()
    text = text.replace("ё", "е").replace("’", "'").replace("`", "'")
    text = MODERATION_REPEATED_CHAR_RE.sub(r"\1\1", text)
    return " ".join(text.split())


def compact_moderation_text(value: Any) -> str:
    return MODERATION_COMPACT_RE.sub("", normalize_moderation_text(value))


def moderation_terms_from_language_map(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    terms: list[str] = []
    for language_terms in value.values():
        if not isinstance(language_terms, list):
            continue
        terms.extend(safe_text(item) for item in language_terms if safe_text(item))
    return terms


def load_taboo_rules() -> dict[str, Any]:
    global TABOO_RULES_CACHE
    if TABOO_RULES_CACHE is not None:
        return TABOO_RULES_CACHE

    empty_rules: dict[str, Any] = {
        "exact_words": {},
        "phrases": {},
        "fragments": {},
        "allowlist": set(),
    }
    try:
        payload = json.loads(TABOO_WORDS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to load taboo words file: %s", TABOO_WORDS_FILE)
        TABOO_RULES_CACHE = empty_rules
        return TABOO_RULES_CACHE

    rules: dict[str, Any] = {
        "exact_words": {},
        "phrases": {},
        "fragments": {},
        "allowlist": set(),
    }
    categories = payload.get("categories") if isinstance(payload, dict) else {}
    if isinstance(categories, dict):
        for category, language_map in categories.items():
            category_key = safe_text(category) or "taboo"
            for raw_term in moderation_terms_from_language_map(language_map):
                normalized = normalize_moderation_text(raw_term)
                compact = compact_moderation_text(raw_term)
                if not compact:
                    continue
                if " " in normalized:
                    rules["phrases"].setdefault(category_key, set()).add(normalized)
                else:
                    rules["exact_words"].setdefault(category_key, set()).add(compact)

    fragments = payload.get("contains_fragments") if isinstance(payload, dict) else {}
    if isinstance(fragments, dict):
        for language, raw_fragments in fragments.items():
            if not isinstance(raw_fragments, list):
                continue
            category_key = f"fragment:{safe_text(language) or 'unknown'}"
            for raw_fragment in raw_fragments:
                compact = compact_moderation_text(raw_fragment)
                if compact:
                    rules["fragments"].setdefault(category_key, set()).add(compact)

    allowlist = payload.get("allowlist") if isinstance(payload, dict) else {}
    for raw_term in moderation_terms_from_language_map(allowlist):
        compact = compact_moderation_text(raw_term)
        if compact:
            rules["allowlist"].add(compact)

    TABOO_RULES_CACHE = rules
    return TABOO_RULES_CACHE


def check_comment_text_for_taboo(text: str) -> dict[str, Any]:
    normalized = normalize_moderation_text(text)
    if not normalized:
        return {"blocked": False}

    rules = load_taboo_rules()
    allowlist = rules.get("allowlist") or set()
    tokens = {
        compact_moderation_text(token)
        for token in MODERATION_TOKEN_RE.findall(normalized)
    }
    tokens = {token for token in tokens if token and token not in allowlist}

    for category, words in (rules.get("exact_words") or {}).items():
        if tokens.intersection(words):
            return {"blocked": True, "category": category}

    phrase_text = f" {normalized} "
    for category, phrases in (rules.get("phrases") or {}).items():
        for phrase in phrases:
            if f" {phrase} " in phrase_text:
                return {"blocked": True, "category": category}

    compact_chunks = [
        compact_moderation_text(chunk)
        for chunk in normalized.split()
    ]
    for category, fragments in (rules.get("fragments") or {}).items():
        for chunk in compact_chunks:
            if any(fragment in chunk for fragment in fragments):
                return {"blocked": True, "category": category}

    return {"blocked": False}


def ensure_comment_text_has_no_taboo(text: str) -> None:
    result = check_comment_text_for_taboo(text)
    if result.get("blocked"):
        raise CommentBlockedError(category=safe_text(result.get("category")) or "taboo")


def humanize_comment_error_message(message: str) -> str:
    normalized = safe_text(message)
    if not normalized:
        return "Не удалось обработать комментарий."
    return {
        "Post not found": "Пост для комментария больше не найден. Попробуйте открыть обсуждение заново.",
        "Parent comment not found": "Комментарий, на который вы отвечаете, больше не найден.",
        "Comment not found": "Комментарий не найден.",
        "Comment is deleted": "Комментарий удалён.",
        "Comment is empty": "Комментарий пустой. Напишите текст или прикрепите фото.",
        "Comment is too long": "Комментарий слишком длинный. Максимум 4000 символов.",
        "Links are not allowed in comments": "Ссылки запрещены правилами сервиса.",
        COMMENT_BLOCKED_CODE: COMMENT_BLOCKED_MESSAGE,
        "INVALID_REACTION": INVALID_REACTION_MESSAGE,
        "Channel is not connected": "Этот канал не подключён к боту. Добавьте его через `/channel_add CHANNEL_ID COMMENTS_CHAT_ID`.",
        "Comments chat is not configured for this post": "Для этого поста не найден чат комментариев.",
        "You can edit only your own comments": "Можно редактировать только свои комментарии.",
        "You can delete only your own comments": "Можно удалять только свои комментарии.",
        "Access denied": ACCESS_DENIED_MESSAGE,
        "ACCESS_DENIED": ADMIN_LOGIN_ACCESS_DENIED_MESSAGE,
        "INVALID_CREDENTIALS": INVALID_CREDENTIALS_MESSAGE,
        "FORBIDDEN": FORBIDDEN_MESSAGE,
        "Report already exists": REPORT_ALREADY_EXISTS_MESSAGE,
        "Invalid report reason": "Выберите причину жалобы.",
    }.get(normalized, normalized)


def comment_reply_preview_text(comment: sqlite3.Row | dict[str, Any]) -> str:
    text = safe_text(comment["text"] if isinstance(comment, sqlite3.Row) else comment.get("text"))
    if text:
        return snippet(text, 120)
    media_items = (
        deserialize_comment_media(comment["media_json"])
        if isinstance(comment, sqlite3.Row)
        else [item for item in (comment.get("media") or []) if isinstance(item, dict)]
    )
    if media_items:
        return "Фото"
    return "Комментарий"


def post_token_for_message_id(post_message_id: str) -> str:
    token = base64.urlsafe_b64encode(post_message_id.encode("utf-8")).decode("ascii")
    return token.rstrip("=")


def message_id_from_post_token(post_token: str) -> str | None:
    padding = "=" * (-len(post_token) % 4)
    try:
        raw = base64.urlsafe_b64decode(f"{post_token}{padding}".encode("ascii"))
        return raw.decode("utf-8")
    except Exception:
        return None


def extract_message_id(message_payload: dict[str, Any] | None) -> str | None:
    if not isinstance(message_payload, dict):
        return None
    message = message_payload.get("message")
    if isinstance(message, dict):
        body = message.get("body") or {}
        return safe_text(body.get("mid") or message.get("mid")) or None
    body = message_payload.get("body") or {}
    return safe_text(body.get("mid") or message_payload.get("mid")) or None


def post_ref_for_message_id(post_message_id: str) -> str:
    return f"post_{post_token_for_message_id(post_message_id)}"


def comment_code_for_post(post_message_id: str) -> str:
    digest = hashlib.sha1(post_message_id.encode("utf-8")).hexdigest()
    return f"c{digest[:8]}"


def comment_count_label(comment_count: int) -> str:
    value = max(int(comment_count), 0)
    mod10 = value % 10
    mod100 = value % 100
    if mod10 == 1 and mod100 != 11:
        noun = "комментарий"
    elif 2 <= mod10 <= 4 and not 12 <= mod100 <= 14:
        noun = "комментария"
    else:
        noun = "комментариев"
    return f"{value} {noun}"


def comment_button_text(comment_count: int) -> str:
    return comment_count_label(comment_count)


def max_share_url(text: str) -> str:
    return f"https://max.ru/:share?text={parse.quote(safe_text(text))}"


CHANNEL_POST_FOOTER = "Комментарии к этому посту открываются в мини-приложении по кнопке ниже."
SETUP_SERVICE_MESSAGE_PREFIXES = (
    "Канал создан в админке",
    "Готовые кнопки для завершения привязки",
    "Привязка завершена.",
    "Не удалось выполнить настройку:",
)
COMMENTS_PAGE_SIZE_DEFAULT = 40
COMMENTS_PAGE_SIZE_MAX = 100


def strip_managed_channel_footer(post_text: str) -> str:
    clean_text = safe_text(post_text)
    footer_marker = f"\n\n{CHANNEL_POST_FOOTER}"
    if footer_marker in clean_text:
        return clean_text.split(footer_marker, 1)[0].rstrip()
    return clean_text


def is_setup_service_message_text(post_text: str) -> bool:
    clean_text = safe_text(post_text)
    if any(clean_text.startswith(prefix) for prefix in SETUP_SERVICE_MESSAGE_PREFIXES):
        return True
    return "Код привязки:" in clean_text and "/bind_comments" in clean_text


def serialize_message_attachments(attachments: list[dict[str, Any]] | None) -> str:
    return json.dumps(attachments or [], ensure_ascii=False, separators=(",", ":"))


def deserialize_message_attachments(raw_value: str | None) -> list[dict[str, Any]]:
    if not raw_value:
        return []
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def looks_like_image_url(url: str) -> bool:
    path = parse.urlparse(safe_text(url)).path.lower()
    return path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"))


def extract_post_media_item(node: dict[str, Any]) -> dict[str, Any] | None:
    url = safe_text(
        node.get("url")
        or node.get("image_url")
        or node.get("src")
        or node.get("download_url")
        or node.get("original_url")
    )
    preview_url = safe_text(
        node.get("preview_url")
        or node.get("previewUrl")
        or node.get("thumbnail_url")
        or node.get("thumb_url")
        or url
    )
    mime_type = safe_text(node.get("mime_type") or node.get("mimeType")).lower()
    type_hint = safe_text(node.get("type") or node.get("kind") or node.get("media_type")).lower()
    width = validate_positive_int(node.get("width"), minimum=1, maximum=20_000)
    height = validate_positive_int(node.get("height"), minimum=1, maximum=20_000)

    if not url:
        return None
    if not (
        type_hint == "image"
        or mime_type.startswith("image/")
        or looks_like_image_url(url)
        or looks_like_image_url(preview_url)
        or width is not None
        or height is not None
    ):
        return None

    return {
        "kind": "image",
        "url": url,
        "preview_url": preview_url or url,
        "width": width,
        "height": height,
    }


def extract_post_media_from_attachments(attachments: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    media_items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    def collect(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                collect(item)
            return
        if not isinstance(node, dict):
            return

        attachment_type = safe_text(node.get("type")).lower()
        payload = node.get("payload")
        candidates = [node]
        if isinstance(payload, dict):
            merged_payload = dict(payload)
            if attachment_type and "type" not in merged_payload:
                merged_payload["type"] = attachment_type
            candidates.insert(0, merged_payload)

        for candidate in candidates:
            media_item = extract_post_media_item(candidate)
            if media_item is None:
                continue
            dedupe_key = safe_text(media_item.get("url") or media_item.get("preview_url"))
            if not dedupe_key or dedupe_key in seen_urls:
                continue
            seen_urls.add(dedupe_key)
            media_items.append(media_item)

        if attachment_type == "inline_keyboard":
            return
        for child_key in ("payload", "attachments", "items", "media", "images", "photos"):
            child = node.get(child_key)
            if isinstance(child, (list, dict)):
                collect(child)

    collect(attachments or [])
    return media_items


def serialize_comment_media(media_items: list[dict[str, Any]] | None) -> str:
    return json.dumps(media_items or [], ensure_ascii=False, separators=(",", ":"))


def deserialize_comment_media(raw_value: str | None) -> list[dict[str, Any]]:
    if not raw_value:
        return []
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def safe_file_name(file_name: Any, *, fallback: str) -> str:
    clean = "".join(
        ch for ch in safe_text(file_name).replace("\\", "/").split("/")[-1] if ch.isalnum() or ch in "._-"
    )
    return clean or fallback


def comment_media_public_path(storage_path: str) -> str:
    encoded = parse.quote(storage_path.strip("/"), safe="/")
    return f"/media/comments/{encoded}"


def comment_media_public_url(storage_path: str) -> str:
    public_path = comment_media_public_path(storage_path)
    if WEB_APP_PUBLIC_URL:
        return f"{WEB_APP_PUBLIC_URL.rstrip('/')}{public_path}"
    return public_path


def public_app_url(path: str = "") -> str:
    base_url = WEB_APP_PUBLIC_URL.rstrip("/")
    clean_path = safe_text(path)
    if not base_url:
        return clean_path or "/"
    if not clean_path:
        return base_url
    if not clean_path.startswith("/"):
        clean_path = f"/{clean_path}"
    return f"{base_url}{clean_path}"


def detect_image_format(binary: bytes) -> tuple[str, str]:
    if binary.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if binary.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    raise MaxApiError("Unsupported image format. Use JPG or PNG.")


def validate_positive_int(raw_value: Any, *, minimum: int = 1, maximum: int = 10000) -> int | None:
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    if value < minimum or value > maximum:
        return None
    return value


def normalize_comments_page_limit(raw_value: Any) -> int:
    try:
        limit = int(raw_value)
    except (TypeError, ValueError):
        return COMMENTS_PAGE_SIZE_DEFAULT
    return max(1, min(limit, COMMENTS_PAGE_SIZE_MAX))


def chat_title_from_payload(payload: dict[str, Any]) -> str:
    title = safe_text(
        payload.get("title")
        or payload.get("name")
        or payload.get("chat_title")
        or payload.get("chat_name")
    )
    if title:
        return title
    nested_chat = payload.get("chat")
    if isinstance(nested_chat, dict):
        return chat_title_from_payload(nested_chat)
    return ""


def chat_link_from_payload(payload: dict[str, Any]) -> str:
    link = safe_text(payload.get("link") or payload.get("url") or payload.get("chat_link"))
    if link:
        return link
    nested_chat = payload.get("chat")
    if isinstance(nested_chat, dict):
        return chat_link_from_payload(nested_chat)
    return ""


@dataclass
class PendingComment:
    user_id: int
    post_message_id: str


@dataclass
class PendingChannelBinding:
    bind_code: str
    requested_by_user_id: int
    channel_chat_id: int
    channel_title: str | None
    created_at: str


@dataclass
class ForwardedChannelPost:
    channel_id: int
    channel_title: str
    post_message_id: str | None
    post_text: str
    post_url: str | None
    post_attachments: list[dict[str, Any]]
    raw_payload: dict[str, Any]


@dataclass
class AuthenticatedWebAppUser:
    user_id: int
    display_name: str
    username: str | None
    platform: str
    chat_id: int | None
    chat_type: str | None


@dataclass
class AdminContext:
    user_id: int
    role: str
    channel_ids: set[int]
    admin_user_id: int | None = None
    must_change_password: bool = False
    login: str = ""

    @property
    def is_super_admin(self) -> bool:
        return self.role == ROLE_SUPER_ADMIN


class MaxApiError(RuntimeError):
    pass


class CommentBlockedError(MaxApiError):
    def __init__(self, *, category: str = "taboo") -> None:
        super().__init__(COMMENT_BLOCKED_CODE)
        self.category = category


class WebAppAuthError(RuntimeError):
    pass


class MaxApiClient:
    def __init__(self, token: str, base_url: str) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")

    def _request(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            query_string = parse.urlencode(
                [(key, value) for key, value in query.items() if value is not None],
                doseq=True,
            )
            url = f"{url}?{query_string}"

        body = None
        headers = {
            "Authorization": self.token,
            "Accept": "application/json",
        }
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = request.Request(url, data=body, method=method, headers=headers)
        try:
            with request.urlopen(req, timeout=90) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise MaxApiError(f"{method} {url} failed: {exc.code} {raw}") from exc
        except error.URLError as exc:
            raise MaxApiError(f"{method} {url} failed: {exc.reason}") from exc

        if not raw:
            return {}
        return json.loads(raw)

    def _request_absolute(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request_headers = {
            "Authorization": self.token,
            "Accept": "application/json",
        }
        if headers:
            request_headers.update(headers)
        req = request.Request(url, data=body, method=method, headers=request_headers)
        try:
            with request.urlopen(req, timeout=90) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise MaxApiError(f"{method} {url} failed: {exc.code} {raw}") from exc
        except error.URLError as exc:
            raise MaxApiError(f"{method} {url} failed: {exc.reason}") from exc

        if not raw:
            return {}
        return json.loads(raw)

    def get_me(self) -> dict[str, Any]:
        return self._request("GET", "/me")

    def get_updates(self, marker: int | None) -> dict[str, Any]:
        query = {
            "limit": POLL_LIMIT,
            "timeout": POLL_TIMEOUT_SECONDS,
            "marker": marker,
            "types": WEBHOOK_UPDATE_TYPES,
        }
        return self._request("GET", "/updates", query=query)

    def get_subscriptions(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/subscriptions")
        subscriptions = payload.get("subscriptions") or []
        if not isinstance(subscriptions, list):
            return []
        return [item for item in subscriptions if isinstance(item, dict)]

    def get_chat(self, chat_id: int) -> dict[str, Any]:
        return self._request("GET", f"/chats/{int(chat_id)}")

    def get_chat_admins(self, chat_id: int) -> list[dict[str, Any]]:
        payload = self._request("GET", f"/chats/{int(chat_id)}/members/admins")
        members = payload.get("members") or payload.get("admins") or []
        if not isinstance(members, list):
            return []
        return [item for item in members if isinstance(item, dict)]

    def get_chat_member_me(self, chat_id: int) -> dict[str, Any]:
        return self._request("GET", f"/chats/{int(chat_id)}/members/me")

    def create_subscription(
        self,
        *,
        url: str,
        update_types: list[str] | None = None,
        secret: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"url": url}
        if update_types:
            payload["update_types"] = update_types
        if safe_text(secret):
            payload["secret"] = safe_text(secret)
        return self._request("POST", "/subscriptions", payload=payload)

    def delete_subscription(self, *, url: str) -> dict[str, Any]:
        return self._request("DELETE", "/subscriptions", query={"url": url})

    def create_upload(self, upload_type: str) -> dict[str, Any]:
        return self._request("POST", "/uploads", query={"type": upload_type})

    def upload_image(
        self,
        *,
        file_name: str,
        mime_type: str,
        binary: bytes,
    ) -> dict[str, Any]:
        upload_info = self.create_upload("image")
        upload_url = safe_text(upload_info.get("url"))
        if not upload_url:
            raise MaxApiError("Image upload URL is missing")

        boundary = f"----MaxComments{uuid.uuid4().hex}"
        safe_name = safe_file_name(file_name, fallback="image.jpg")
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="data"; filename="{safe_name}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8") + binary + f"\r\n--{boundary}--\r\n".encode("utf-8")
        payload = self._request_absolute(
            "POST",
            upload_url,
            body=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        if not isinstance(payload, dict):
            raise MaxApiError("Image upload response is invalid")
        return {"type": "image", "payload": payload}

    def send_message(
        self,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        link: dict[str, Any] | None = None,
        notify: bool = True,
        fmt: str = "markdown",
    ) -> dict[str, Any]:
        query = {"user_id": user_id, "chat_id": chat_id}
        payload: dict[str, Any] = {
            "text": text,
            "notify": notify,
            "format": fmt,
        }
        if attachments is not None:
            payload["attachments"] = attachments
        if link is not None:
            payload["link"] = link
        return self._request("POST", "/messages", query=query, payload=payload)

    def edit_message(
        self,
        message_id: str,
        *,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        fmt: str = "markdown",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": text, "format": fmt}
        if attachments is not None:
            payload["attachments"] = attachments
        return self._request(
            "PUT",
            "/messages",
            query={"message_id": message_id},
            payload=payload,
        )

    def get_message(self, message_id: str) -> dict[str, Any]:
        return self._request("GET", f"/messages/{message_id}")

    def delete_message(self, message_id: str) -> dict[str, Any]:
        return self._request("DELETE", "/messages", query={"message_id": message_id})

    def get_chat_messages(
        self,
        chat_id: int,
        *,
        count: int = 50,
    ) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/messages",
            query={"chat_id": chat_id, "count": count},
        )
        messages = payload.get("messages") or []
        if not isinstance(messages, list):
            return []
        return [item for item in messages if isinstance(item, dict)]


class CommentStore:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS channel_bindings (
                    channel_chat_id INTEGER PRIMARY KEY,
                    comments_chat_id INTEGER NOT NULL,
                    comments_chat_url TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS posts (
                    post_message_id TEXT PRIMARY KEY,
                    channel_chat_id INTEGER NOT NULL,
                    comments_chat_id INTEGER,
                    post_title TEXT,
                    post_url TEXT,
                    post_text TEXT,
                    post_attachments_json TEXT NOT NULL DEFAULT '[]',
                    discussion_message_id TEXT,
                    status TEXT NOT NULL DEFAULT 'published',
                    deleted_at TEXT,
                    deleted_by_user_id INTEGER,
                    delete_reason TEXT,
                    comment_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_message_id TEXT NOT NULL,
                    parent_comment_id INTEGER,
                    user_id INTEGER NOT NULL,
                    display_name TEXT NOT NULL,
                    username TEXT,
                    text TEXT NOT NULL,
                    media_json TEXT NOT NULL DEFAULT '[]',
                    source_message_id TEXT,
                    discussion_copy_message_id TEXT,
                    source_kind TEXT NOT NULL DEFAULT 'bot',
                    status TEXT NOT NULL DEFAULT 'active',
                    deleted_at TEXT,
                    deleted_by_user_id INTEGER,
                    delete_reason TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(post_message_id) REFERENCES posts(post_message_id)
                );

                CREATE TABLE IF NOT EXISTS pending_comments (
                    user_id INTEGER PRIMARY KEY,
                    post_message_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(post_message_id) REFERENCES posts(post_message_id)
                );

                CREATE TABLE IF NOT EXISTS comment_read_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    post_id TEXT NOT NULL,
                    last_read_comment_id INTEGER,
                    last_read_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, post_id),
                    FOREIGN KEY(post_id) REFERENCES posts(post_message_id),
                    FOREIGN KEY(last_read_comment_id) REFERENCES comments(id)
                );

                CREATE TABLE IF NOT EXISTS comment_reactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    comment_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    emoji TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(comment_id, user_id),
                    FOREIGN KEY(comment_id) REFERENCES comments(id)
                );

                CREATE TABLE IF NOT EXISTS pending_channel_bindings (
                    bind_code TEXT PRIMARY KEY,
                    requested_by_user_id INTEGER NOT NULL,
                    channel_chat_id INTEGER NOT NULL,
                    channel_title TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_terms_acceptance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    max_user_id TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    version TEXT NOT NULL,
                    UNIQUE(max_user_id, version)
                );

                CREATE TABLE IF NOT EXISTS channel_connection_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT,
                    channel_title TEXT,
                    forwarded_post_id TEXT,
                    requester_user_id INTEGER,
                    requester_max_user_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    admin_comment TEXT,
                    reviewed_by_admin_id INTEGER,
                    reviewed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    raw_payload TEXT
                );

                CREATE TABLE IF NOT EXISTS admin_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    max_user_id TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    must_change_password INTEGER NOT NULL DEFAULT 1,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS channel_admins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, channel_id)
                );

                CREATE TABLE IF NOT EXISTS comment_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    comment_id INTEGER NOT NULL,
                    post_id TEXT NOT NULL,
                    channel_id INTEGER NOT NULL,
                    reporter_user_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    details TEXT,
                    status TEXT NOT NULL DEFAULT 'new',
                    admin_comment TEXT,
                    resolved_by_user_id INTEGER,
                    resolved_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(comment_id, reporter_user_id),
                    FOREIGN KEY(comment_id) REFERENCES comments(id),
                    FOREIGN KEY(post_id) REFERENCES posts(post_message_id)
                );

                CREATE TABLE IF NOT EXISTS admin_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_user_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT,
                    payload TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._migrate_schema()
            self.conn.commit()

    def _migrate_schema(self) -> None:
        comment_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(comments)").fetchall()
        }
        if "source_kind" not in comment_columns:
            self.conn.execute(
                "ALTER TABLE comments ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'bot'"
            )
        if "parent_comment_id" not in comment_columns:
            self.conn.execute(
                "ALTER TABLE comments ADD COLUMN parent_comment_id INTEGER"
            )
        if "media_json" not in comment_columns:
            self.conn.execute(
                "ALTER TABLE comments ADD COLUMN media_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "discussion_copy_message_id" not in comment_columns:
            self.conn.execute(
                "ALTER TABLE comments ADD COLUMN discussion_copy_message_id TEXT"
            )
        if "status" not in comment_columns:
            self.conn.execute(
                "ALTER TABLE comments ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
            )
        if "deleted_at" not in comment_columns:
            self.conn.execute(
                "ALTER TABLE comments ADD COLUMN deleted_at TEXT"
            )
        if "deleted_by_user_id" not in comment_columns:
            self.conn.execute(
                "ALTER TABLE comments ADD COLUMN deleted_by_user_id INTEGER"
            )
        if "delete_reason" not in comment_columns:
            self.conn.execute(
                "ALTER TABLE comments ADD COLUMN delete_reason TEXT"
            )
        post_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(posts)").fetchall()
        }
        if "post_title" not in post_columns:
            self.conn.execute(
                "ALTER TABLE posts ADD COLUMN post_title TEXT"
            )
        if "comments_chat_id" not in post_columns:
            self.conn.execute(
                "ALTER TABLE posts ADD COLUMN comments_chat_id INTEGER"
            )
            if COMMENTS_CHAT_ID is not None:
                self.conn.execute(
                    "UPDATE posts SET comments_chat_id = ? WHERE comments_chat_id IS NULL",
                    (COMMENTS_CHAT_ID,),
                )
        if "post_attachments_json" not in post_columns:
            self.conn.execute(
                "ALTER TABLE posts ADD COLUMN post_attachments_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "status" not in post_columns:
            self.conn.execute(
                "ALTER TABLE posts ADD COLUMN status TEXT NOT NULL DEFAULT 'published'"
            )
        if "deleted_at" not in post_columns:
            self.conn.execute(
                "ALTER TABLE posts ADD COLUMN deleted_at TEXT"
            )
        if "deleted_by_user_id" not in post_columns:
            self.conn.execute(
                "ALTER TABLE posts ADD COLUMN deleted_by_user_id INTEGER"
            )
        if "delete_reason" not in post_columns:
            self.conn.execute(
                "ALTER TABLE posts ADD COLUMN delete_reason TEXT"
            )
        self.conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_admin_users_role
                ON admin_users(role, is_active);
            CREATE INDEX IF NOT EXISTS idx_channel_admins_user_id
                ON channel_admins(user_id);
            CREATE INDEX IF NOT EXISTS idx_channel_admins_channel_id
                ON channel_admins(channel_id);
            CREATE INDEX IF NOT EXISTS idx_posts_channel_status
                ON posts(channel_chat_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_comments_post_status
                ON comments(post_message_id, status, id);
            CREATE INDEX IF NOT EXISTS idx_comment_read_state_user_post
                ON comment_read_state(user_id, post_id);
            CREATE INDEX IF NOT EXISTS idx_comment_read_state_post
                ON comment_read_state(post_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_comment_reactions_comment_user
                ON comment_reactions(comment_id, user_id);
            CREATE INDEX IF NOT EXISTS idx_comment_reactions_comment_id
                ON comment_reactions(comment_id);
            CREATE INDEX IF NOT EXISTS idx_comment_reactions_user_id
                ON comment_reactions(user_id);
            CREATE INDEX IF NOT EXISTS idx_comment_reactions_emoji
                ON comment_reactions(emoji);
            CREATE INDEX IF NOT EXISTS idx_comment_reports_channel_status
                ON comment_reports(channel_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_comment_reports_comment_id
                ON comment_reports(comment_id);
            CREATE INDEX IF NOT EXISTS idx_admin_audit_channel
                ON admin_audit_log(channel_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_terms_acceptance_user
                ON user_terms_acceptance(max_user_id, version);
            CREATE INDEX IF NOT EXISTS idx_channel_requests_channel_id
                ON channel_connection_requests(channel_id);
            CREATE INDEX IF NOT EXISTS idx_channel_requests_requester
                ON channel_connection_requests(requester_max_user_id);
            CREATE INDEX IF NOT EXISTS idx_channel_requests_status
                ON channel_connection_requests(status, created_at);
            """
        )

    def upsert_channel_binding(
        self,
        *,
        channel_chat_id: int,
        comments_chat_id: int,
        comments_chat_url: str | None = None,
    ) -> None:
        now = utc_now()
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO channel_bindings (
                    channel_chat_id,
                    comments_chat_id,
                    comments_chat_url,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel_chat_id) DO UPDATE SET
                    comments_chat_id = excluded.comments_chat_id,
                    comments_chat_url = COALESCE(excluded.comments_chat_url, channel_bindings.comments_chat_url),
                    updated_at = excluded.updated_at
                """,
                (
                    int(channel_chat_id),
                    int(comments_chat_id),
                    safe_text(comments_chat_url) or None,
                    now,
                    now,
                ),
            )
            self.conn.commit()

    def get_channel_binding(self, channel_chat_id: int) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(
                """
                SELECT *
                FROM channel_bindings
                WHERE channel_chat_id = ?
                """,
                (int(channel_chat_id),),
            ).fetchone()

    def list_channel_bindings(self) -> list[sqlite3.Row]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT *
                FROM channel_bindings
                ORDER BY channel_chat_id ASC
                """
            ).fetchall()
        return list(rows)

    def delete_channel_binding(self, channel_chat_id: int) -> bool:
        with self.lock:
            cursor = self.conn.execute(
                "DELETE FROM channel_bindings WHERE channel_chat_id = ?",
                (int(channel_chat_id),),
            )
            self.conn.commit()
        return cursor.rowcount > 0

    def add_channel_admin(self, *, user_id: int, channel_id: int) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO channel_admins (user_id, channel_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, channel_id) DO NOTHING
                """,
                (int(user_id), int(channel_id), utc_now()),
            )
            self.conn.commit()

    def list_channel_admin_channel_ids(self, user_id: int) -> set[int]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT channel_id
                FROM channel_admins
                WHERE user_id = ?
                ORDER BY channel_id ASC
                """,
                (int(user_id),),
            ).fetchall()
        return {int(row["channel_id"]) for row in rows}

    def list_channel_bindings_for_channels(self, channel_ids: set[int]) -> list[sqlite3.Row]:
        normalized_ids = sorted({int(channel_id) for channel_id in channel_ids})
        if not normalized_ids:
            return []
        placeholders = ", ".join("?" for _ in normalized_ids)
        with self.lock:
            rows = self.conn.execute(
                f"""
                SELECT *
                FROM channel_bindings
                WHERE channel_chat_id IN ({placeholders})
                ORDER BY channel_chat_id ASC
                """,
                tuple(normalized_ids),
            ).fetchall()
        return list(rows)

    def get_admin_user_by_max_user_id(self, max_user_id: str | int) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(
                """
                SELECT *
                FROM admin_users
                WHERE max_user_id = ?
                """,
                (safe_text(max_user_id),),
            ).fetchone()

    def get_admin_user_by_id(self, admin_user_id: int) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(
                """
                SELECT *
                FROM admin_users
                WHERE id = ?
                """,
                (int(admin_user_id),),
            ).fetchone()

    def ensure_admin_user(
        self,
        *,
        max_user_id: str | int,
        role: str,
        password: str | None = None,
        must_change_password: bool = True,
        is_active: bool = True,
    ) -> sqlite3.Row:
        normalized_user_id = safe_text(max_user_id)
        normalized_role = safe_text(role)
        if normalized_role not in ADMIN_ROLES:
            raise ValueError("Invalid admin role")
        if not normalized_user_id:
            raise ValueError("max_user_id is required")
        existing = self.get_admin_user_by_max_user_id(normalized_user_id)
        now = utc_now()
        with self.lock:
            if existing is None:
                self.conn.execute(
                    """
                    INSERT INTO admin_users (
                        max_user_id,
                        password_hash,
                        role,
                        must_change_password,
                        is_active,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_user_id,
                        hash_admin_password(password or normalized_user_id),
                        normalized_role,
                        1 if must_change_password else 0,
                        1 if is_active else 0,
                        now,
                        now,
                    ),
                )
            else:
                self.conn.execute(
                    """
                    UPDATE admin_users
                    SET role = ?,
                        is_active = ?,
                        updated_at = ?
                    WHERE max_user_id = ?
                    """,
                    (
                        normalized_role,
                        1 if is_active else 0,
                        now,
                        normalized_user_id,
                    ),
                )
            self.conn.commit()
        row = self.get_admin_user_by_max_user_id(normalized_user_id)
        if row is None:
            raise RuntimeError("admin user was not saved")
        return row

    def list_admin_users(self) -> list[sqlite3.Row]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT
                    admin_users.*,
                    GROUP_CONCAT(channel_admins.channel_id) AS channel_ids
                FROM admin_users
                LEFT JOIN channel_admins
                    ON channel_admins.user_id = CAST(admin_users.max_user_id AS INTEGER)
                GROUP BY admin_users.id
                ORDER BY admin_users.role DESC, admin_users.max_user_id ASC
                """
            ).fetchall()
        return list(rows)

    def replace_admin_channels(self, *, max_user_id: str | int, channel_ids: set[int]) -> None:
        normalized_user_id = int(safe_text(max_user_id))
        now = utc_now()
        with self.lock:
            self.conn.execute(
                "DELETE FROM channel_admins WHERE user_id = ?",
                (normalized_user_id,),
            )
            for channel_id in sorted({int(channel_id) for channel_id in channel_ids}):
                self.conn.execute(
                    """
                    INSERT INTO channel_admins (user_id, channel_id, created_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id, channel_id) DO NOTHING
                    """,
                    (normalized_user_id, int(channel_id), now),
                )
            self.conn.commit()

    def delete_admin_channel(self, *, max_user_id: str | int, channel_id: int) -> bool:
        with self.lock:
            cursor = self.conn.execute(
                """
                DELETE FROM channel_admins
                WHERE user_id = ? AND channel_id = ?
                """,
                (int(safe_text(max_user_id)), int(channel_id)),
            )
            self.conn.commit()
        return cursor.rowcount > 0

    def update_admin_user(
        self,
        *,
        admin_user_id: int,
        role: str | None = None,
        is_active: bool | None = None,
    ) -> sqlite3.Row | None:
        row = self.get_admin_user_by_id(admin_user_id)
        if row is None:
            return None
        normalized_role = safe_text(role) or safe_text(row["role"])
        if normalized_role not in ADMIN_ROLES:
            raise ValueError("Invalid admin role")
        active_value = int(row["is_active"]) if is_active is None else (1 if is_active else 0)
        with self.lock:
            self.conn.execute(
                """
                UPDATE admin_users
                SET role = ?,
                    is_active = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (normalized_role, active_value, utc_now(), int(admin_user_id)),
            )
            self.conn.commit()
        return self.get_admin_user_by_id(admin_user_id)

    def set_admin_password(
        self,
        *,
        admin_user_id: int,
        password: str,
        must_change_password: bool,
    ) -> sqlite3.Row | None:
        if self.get_admin_user_by_id(admin_user_id) is None:
            return None
        with self.lock:
            self.conn.execute(
                """
                UPDATE admin_users
                SET password_hash = ?,
                    must_change_password = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    hash_admin_password(password),
                    1 if must_change_password else 0,
                    utc_now(),
                    int(admin_user_id),
                ),
            )
            self.conn.commit()
        return self.get_admin_user_by_id(admin_user_id)

    def migrate_channel_admins_to_admin_users(self) -> int:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT DISTINCT user_id
                FROM channel_admins
                """
            ).fetchall()
        created = 0
        for row in rows:
            max_user_id = safe_text(row["user_id"])
            if not max_user_id:
                continue
            if self.get_admin_user_by_max_user_id(max_user_id) is None:
                self.ensure_admin_user(
                    max_user_id=max_user_id,
                    role=ROLE_CHANNEL_ADMIN,
                    password=max_user_id,
                    must_change_password=True,
                    is_active=True,
                )
                created += 1
        return created

    def upsert_post(
        self,
        *,
        post_message_id: str,
        channel_chat_id: int,
        comments_chat_id: int | None,
        post_url: str | None,
        post_text: str,
        post_attachments: list[dict[str, Any]] | None = None,
        post_title: str | None = None,
        discussion_message_id: str | None = None,
    ) -> None:
        now = utc_now()
        post_attachments_json = serialize_message_attachments(post_attachments)
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO posts (
                    post_message_id,
                    channel_chat_id,
                    comments_chat_id,
                    post_title,
                    post_url,
                    post_text,
                    post_attachments_json,
                    discussion_message_id,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_message_id) DO UPDATE SET
                    channel_chat_id = excluded.channel_chat_id,
                    comments_chat_id = COALESCE(posts.comments_chat_id, excluded.comments_chat_id),
                    post_title = COALESCE(excluded.post_title, posts.post_title),
                    post_url = excluded.post_url,
                    post_text = excluded.post_text,
                    post_attachments_json = excluded.post_attachments_json,
                    discussion_message_id = COALESCE(excluded.discussion_message_id, posts.discussion_message_id),
                    updated_at = excluded.updated_at
                """,
                (
                    post_message_id,
                    channel_chat_id,
                    comments_chat_id,
                    safe_text(post_title) or None,
                    post_url,
                    post_text,
                    post_attachments_json,
                    discussion_message_id,
                    now,
                    now,
                ),
            )
            self.conn.commit()

    def set_discussion_message_id(self, post_message_id: str, discussion_message_id: str) -> None:
        with self.lock:
            self.conn.execute(
                """
                UPDATE posts
                SET discussion_message_id = ?, updated_at = ?
                WHERE post_message_id = ?
                """,
                (discussion_message_id, utc_now(), post_message_id),
            )
            self.conn.commit()

    def get_post(self, post_message_id: str) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM posts WHERE post_message_id = ?",
                (post_message_id,),
            ).fetchone()

    def list_posts(
        self,
        limit: int = 10,
        *,
        channel_ids: set[int] | None = None,
        status: str | None = None,
    ) -> list[sqlite3.Row]:
        query = ["SELECT * FROM posts"]
        params: list[Any] = []
        where: list[str] = []
        if channel_ids is not None:
            normalized_ids = sorted({int(channel_id) for channel_id in channel_ids})
            if not normalized_ids:
                return []
            placeholders = ", ".join("?" for _ in normalized_ids)
            where.append(f"channel_chat_id IN ({placeholders})")
            params.extend(normalized_ids)
        if status:
            where.append("status = ?")
            params.append(safe_text(status))
        if where:
            query.append("WHERE " + " AND ".join(where))
        query.append("ORDER BY created_at DESC")
        query.append("LIMIT ?")
        params.append(max(1, min(int(limit), 500)))
        with self.lock:
            rows = self.conn.execute(
                "\n".join(query),
                tuple(params),
            ).fetchall()
        return list(rows)

    def soft_delete_post(
        self,
        *,
        post_message_id: str,
        deleted_by_user_id: int,
        reason: str | None = None,
    ) -> sqlite3.Row | None:
        now = utc_now()
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM posts WHERE post_message_id = ?",
                (post_message_id,),
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                """
                UPDATE posts
                SET status = 'deleted',
                    deleted_at = COALESCE(deleted_at, ?),
                    deleted_by_user_id = ?,
                    delete_reason = ?,
                    updated_at = ?
                WHERE post_message_id = ?
                """,
                (
                    now,
                    int(deleted_by_user_id),
                    safe_text(reason) or None,
                    now,
                    post_message_id,
                ),
            )
            self.conn.commit()
            return self.conn.execute(
                "SELECT * FROM posts WHERE post_message_id = ?",
                (post_message_id,),
            ).fetchone()

    def find_post_by_comment_code(self, comment_code: str) -> sqlite3.Row | None:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT * FROM posts
                ORDER BY created_at DESC
                """
            ).fetchall()
        for row in rows:
            if comment_code_for_post(row["post_message_id"]) == comment_code:
                return row
        return None

    def set_pending_comment(self, user_id: int, post_message_id: str) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO pending_comments (user_id, post_message_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    post_message_id = excluded.post_message_id,
                    created_at = excluded.created_at
                """,
                (user_id, post_message_id, utc_now()),
            )
            self.conn.commit()

    def pop_pending_comment(self, user_id: int) -> PendingComment | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT user_id, post_message_id FROM pending_comments WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            self.conn.execute("DELETE FROM pending_comments WHERE user_id = ?", (user_id,))
            self.conn.commit()
        return PendingComment(user_id=row["user_id"], post_message_id=row["post_message_id"])

    def set_pending_channel_binding(
        self,
        *,
        bind_code: str,
        requested_by_user_id: int,
        channel_chat_id: int,
        channel_title: str | None,
    ) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO pending_channel_bindings (
                    bind_code,
                    requested_by_user_id,
                    channel_chat_id,
                    channel_title,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(bind_code) DO UPDATE SET
                    requested_by_user_id = excluded.requested_by_user_id,
                    channel_chat_id = excluded.channel_chat_id,
                    channel_title = excluded.channel_title,
                    created_at = excluded.created_at
                """,
                (
                    safe_text(bind_code).upper(),
                    int(requested_by_user_id),
                    int(channel_chat_id),
                    safe_text(channel_title) or None,
                    utc_now(),
                ),
            )
            self.conn.commit()

    def get_pending_channel_binding(self, bind_code: str) -> PendingChannelBinding | None:
        with self.lock:
            row = self.conn.execute(
                """
                SELECT bind_code, requested_by_user_id, channel_chat_id, channel_title, created_at
                FROM pending_channel_bindings
                WHERE bind_code = ?
                """,
                (safe_text(bind_code).upper(),),
            ).fetchone()
        if row is None:
            return None
        return PendingChannelBinding(
            bind_code=safe_text(row["bind_code"]).upper(),
            requested_by_user_id=int(row["requested_by_user_id"]),
            channel_chat_id=int(row["channel_chat_id"]),
            channel_title=safe_text(row["channel_title"]) or None,
            created_at=safe_text(row["created_at"]),
        )

    def list_pending_channel_bindings(
        self,
        *,
        requested_by_user_id: int | None = None,
    ) -> list[PendingChannelBinding]:
        query = """
            SELECT bind_code, requested_by_user_id, channel_chat_id, channel_title, created_at
            FROM pending_channel_bindings
        """
        params: list[Any] = []
        if requested_by_user_id is not None:
            query += " WHERE requested_by_user_id = ?"
            params.append(int(requested_by_user_id))
        query += " ORDER BY created_at DESC"
        with self.lock:
            rows = self.conn.execute(query, tuple(params)).fetchall()
        return [
            PendingChannelBinding(
                bind_code=safe_text(row["bind_code"]).upper(),
                requested_by_user_id=int(row["requested_by_user_id"]),
                channel_chat_id=int(row["channel_chat_id"]),
                channel_title=safe_text(row["channel_title"]) or None,
                created_at=safe_text(row["created_at"]),
            )
            for row in rows
        ]

    def delete_pending_channel_binding(self, bind_code: str) -> None:
        with self.lock:
            self.conn.execute(
                "DELETE FROM pending_channel_bindings WHERE bind_code = ?",
                (safe_text(bind_code).upper(),),
            )
            self.conn.commit()

    def clear_expired_pending_channel_bindings(self, *, older_than_iso: str) -> int:
        with self.lock:
            cursor = self.conn.execute(
                "DELETE FROM pending_channel_bindings WHERE created_at < ?",
                (older_than_iso,),
            )
            self.conn.commit()
        return int(cursor.rowcount)

    def has_accepted_terms(self, *, max_user_id: str | int, version: str = TERMS_VERSION) -> bool:
        with self.lock:
            row = self.conn.execute(
                """
                SELECT id
                FROM user_terms_acceptance
                WHERE max_user_id = ? AND version = ?
                """,
                (safe_text(max_user_id), safe_text(version)),
            ).fetchone()
        return row is not None

    def accept_terms(self, *, max_user_id: str | int, version: str = TERMS_VERSION) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO user_terms_acceptance (max_user_id, accepted_at, version)
                VALUES (?, ?, ?)
                ON CONFLICT(max_user_id, version) DO UPDATE SET
                    accepted_at = excluded.accepted_at
                """,
                (safe_text(max_user_id), utc_now(), safe_text(version)),
            )
            self.conn.commit()

    def create_channel_connection_request(
        self,
        *,
        channel_id: int,
        channel_title: str | None,
        forwarded_post_id: str | None,
        requester_user_id: int,
        requester_max_user_id: str | int,
        raw_payload: dict[str, Any] | None,
    ) -> sqlite3.Row:
        raw_payload_json = json.dumps(raw_payload or {}, ensure_ascii=False)[:20000]
        now = utc_now()
        with self.lock:
            cursor = self.conn.execute(
                """
                INSERT INTO channel_connection_requests (
                    channel_id,
                    channel_title,
                    forwarded_post_id,
                    requester_user_id,
                    requester_max_user_id,
                    status,
                    created_at,
                    updated_at,
                    raw_payload
                )
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    safe_text(channel_id),
                    safe_text(channel_title) or None,
                    safe_text(forwarded_post_id) or None,
                    int(requester_user_id),
                    safe_text(requester_max_user_id),
                    now,
                    now,
                    raw_payload_json,
                ),
            )
            request_id = int(cursor.lastrowid)
            self.conn.commit()
        row = self.get_channel_connection_request(request_id)
        if row is None:
            raise RuntimeError("channel connection request was not saved")
        return row

    def get_pending_channel_connection_request(self, *, channel_id: int) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(
                """
                SELECT *
                FROM channel_connection_requests
                WHERE channel_id = ? AND status = 'pending'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (safe_text(channel_id),),
            ).fetchone()

    def get_latest_channel_connection_request_for_requester(
        self,
        *,
        channel_id: int,
        requester_max_user_id: str | int,
    ) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(
                """
                SELECT *
                FROM channel_connection_requests
                WHERE channel_id = ? AND requester_max_user_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (safe_text(channel_id), safe_text(requester_max_user_id)),
            ).fetchone()

    def list_channel_connection_requests(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        query = ["SELECT * FROM channel_connection_requests"]
        params: list[Any] = []
        clean_status = safe_text(status)
        if clean_status:
            query.append("WHERE status = ?")
            params.append(clean_status)
        query.append("ORDER BY created_at DESC")
        query.append("LIMIT ? OFFSET ?")
        params.extend([max(1, min(int(limit), 200)), max(0, int(offset))])
        with self.lock:
            rows = self.conn.execute("\n".join(query), tuple(params)).fetchall()
        return list(rows)

    def get_channel_connection_request(self, request_id: int) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(
                """
                SELECT *
                FROM channel_connection_requests
                WHERE id = ?
                """,
                (int(request_id),),
            ).fetchone()

    def update_channel_connection_request_status(
        self,
        *,
        request_id: int,
        status: str,
        admin_comment: str | None,
        reviewed_by_admin_id: int | None,
    ) -> sqlite3.Row | None:
        clean_status = safe_text(status)
        if clean_status not in CHANNEL_REQUEST_STATUSES:
            raise ValueError("Invalid channel request status")
        now = utc_now()
        reviewed_at = now if clean_status in {CHANNEL_REQUEST_STATUS_APPROVED, CHANNEL_REQUEST_STATUS_REJECTED} else None
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM channel_connection_requests WHERE id = ?",
                (int(request_id),),
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                """
                UPDATE channel_connection_requests
                SET status = ?,
                    admin_comment = COALESCE(?, admin_comment),
                    reviewed_by_admin_id = ?,
                    reviewed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    clean_status,
                    safe_text(admin_comment) or None,
                    int(reviewed_by_admin_id) if reviewed_by_admin_id is not None else None,
                    reviewed_at,
                    now,
                    int(request_id),
                ),
            )
            self.conn.commit()
        return self.get_channel_connection_request(request_id)

    def count_channel_connection_requests(self, *, status: str | None = None) -> int:
        clean_status = safe_text(status)
        with self.lock:
            if clean_status:
                row = self.conn.execute(
                    "SELECT COUNT(*) AS count FROM channel_connection_requests WHERE status = ?",
                    (clean_status,),
                ).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT COUNT(*) AS count FROM channel_connection_requests",
                ).fetchone()
        return int(row["count"] or 0)

    def add_comment(
        self,
        *,
        post_message_id: str,
        parent_comment_id: int | None,
        user_id: int,
        display_name: str,
        username: str | None,
        text: str,
        media: list[dict[str, Any]] | None,
        source_message_id: str | None,
        discussion_copy_message_id: str | None,
        source_kind: str,
    ) -> int:
        now = utc_now()
        media_json = serialize_comment_media(media)
        with self.lock:
            cursor = self.conn.execute(
                """
                INSERT INTO comments (
                    post_message_id,
                    parent_comment_id,
                    user_id,
                    display_name,
                    username,
                    text,
                    media_json,
                    source_message_id,
                    discussion_copy_message_id,
                    source_kind,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post_message_id,
                    parent_comment_id,
                    user_id,
                    display_name,
                    username,
                    text,
                    media_json,
                    source_message_id,
                    discussion_copy_message_id,
                    source_kind,
                    now,
                ),
            )
            self.conn.execute(
                """
                UPDATE posts
                SET comment_count = comment_count + 1, updated_at = ?
                WHERE post_message_id = ?
                """,
                (now, post_message_id),
            )
            self.conn.commit()
            return int(cursor.lastrowid)

    def get_comment_count(self, post_message_id: str) -> int:
        with self.lock:
            row = self.conn.execute(
                "SELECT comment_count FROM posts WHERE post_message_id = ?",
                (post_message_id,),
            ).fetchone()
        if row is None:
            return 0
        return int(row["comment_count"])

    def list_comments(self, post_message_id: str, limit: int = 200) -> list[sqlite3.Row]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT *
                FROM comments
                WHERE post_message_id = ? AND status = 'active'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (post_message_id, limit),
            ).fetchall()
        return list(rows)

    def list_comment_texts(self, post_message_id: str, limit: int = 100) -> list[str]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT text
                FROM comments
                WHERE post_message_id = ? AND status = 'active'
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (post_message_id, limit),
            ).fetchall()
        return [safe_text(row["text"]) for row in rows if safe_text(row["text"])]

    def list_comments_page(
        self,
        post_message_id: str,
        *,
        limit: int,
        before_comment_id: int | None = None,
    ) -> tuple[list[sqlite3.Row], bool]:
        query = [
            "SELECT *",
            "FROM comments",
            "WHERE post_message_id = ? AND status = 'active'",
        ]
        params: list[Any] = [post_message_id]
        if before_comment_id is not None:
            query.append("AND id < ?")
            params.append(before_comment_id)
        query.append("ORDER BY id DESC")
        query.append("LIMIT ?")
        params.append(limit + 1)

        with self.lock:
            rows = self.conn.execute("\n".join(query), tuple(params)).fetchall()

        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]
        rows = list(reversed(rows))
        return rows, has_more

    def get_latest_active_comment_id(self, post_message_id: str) -> int | None:
        with self.lock:
            row = self.conn.execute(
                """
                SELECT id
                FROM comments
                WHERE post_message_id = ? AND status = 'active'
                ORDER BY id DESC
                LIMIT 1
                """,
                (post_message_id,),
            ).fetchone()
        if row is None:
            return None
        return int(row["id"])

    def get_comment_read_state(self, *, user_id: int, post_message_id: str) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(
                """
                SELECT *
                FROM comment_read_state
                WHERE user_id = ? AND post_id = ?
                """,
                (int(user_id), post_message_id),
            ).fetchone()

    def upsert_comment_read_state(
        self,
        *,
        user_id: int,
        post_message_id: str,
        last_read_comment_id: int,
    ) -> sqlite3.Row:
        now = utc_now()
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO comment_read_state (
                    user_id,
                    post_id,
                    last_read_comment_id,
                    last_read_at,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, post_id) DO UPDATE SET
                    last_read_comment_id = CASE
                        WHEN comment_read_state.last_read_comment_id IS NULL
                          OR excluded.last_read_comment_id > comment_read_state.last_read_comment_id
                        THEN excluded.last_read_comment_id
                        ELSE comment_read_state.last_read_comment_id
                    END,
                    last_read_at = excluded.last_read_at,
                    updated_at = excluded.updated_at
                """,
                (
                    int(user_id),
                    post_message_id,
                    int(last_read_comment_id),
                    now,
                    now,
                    now,
                ),
            )
            self.conn.commit()
            row = self.get_comment_read_state(user_id=user_id, post_message_id=post_message_id)
        if row is None:
            raise RuntimeError("comment read state was not saved")
        return row

    def get_comment(self, comment_id: int) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM comments WHERE id = ?",
                (comment_id,),
            ).fetchone()

    def build_comment_reaction_payload(
        self,
        *,
        counts_by_emoji: dict[str, int] | None = None,
        my_reaction: str | None = None,
    ) -> dict[str, Any]:
        normalized_my_reaction = safe_text(my_reaction) or None
        if normalized_my_reaction not in ALLOWED_COMMENT_REACTION_SET:
            normalized_my_reaction = None
        reaction_counts = counts_by_emoji or {}
        reactions = []
        for emoji in ALLOWED_COMMENT_REACTIONS:
            count = int(reaction_counts.get(emoji) or 0)
            if count <= 0:
                continue
            reactions.append(
                {
                    "emoji": emoji,
                    "count": count,
                    "selected": emoji == normalized_my_reaction,
                }
            )
        return {
            "myReaction": normalized_my_reaction,
            "my_reaction": normalized_my_reaction,
            "reactions": reactions,
        }

    def list_comment_reaction_summaries(
        self,
        comment_ids: list[int],
        *,
        viewer_user_id: int | None = None,
    ) -> dict[int, dict[str, Any]]:
        normalized_ids = sorted({int(comment_id) for comment_id in comment_ids if comment_id is not None})
        if not normalized_ids:
            return {}

        placeholders = ", ".join("?" for _ in normalized_ids)
        with self.lock:
            count_rows = self.conn.execute(
                f"""
                SELECT comment_id, emoji, COUNT(*) AS count
                FROM comment_reactions
                WHERE comment_id IN ({placeholders})
                GROUP BY comment_id, emoji
                """,
                tuple(normalized_ids),
            ).fetchall()

            viewer_rows: list[sqlite3.Row] = []
            if viewer_user_id is not None:
                viewer_rows = self.conn.execute(
                    f"""
                    SELECT comment_id, emoji
                    FROM comment_reactions
                    WHERE user_id = ? AND comment_id IN ({placeholders})
                    """,
                    tuple([int(viewer_user_id)] + normalized_ids),
                ).fetchall()

        counts_map: dict[int, dict[str, int]] = {
            int(comment_id): {} for comment_id in normalized_ids
        }
        for row in count_rows:
            comment_id = int(row["comment_id"])
            emoji = safe_text(row["emoji"])
            if emoji not in ALLOWED_COMMENT_REACTION_SET:
                continue
            counts_map.setdefault(comment_id, {})[emoji] = int(row["count"] or 0)

        my_reactions = {
            int(row["comment_id"]): (
                safe_text(row["emoji"]) if safe_text(row["emoji"]) in ALLOWED_COMMENT_REACTION_SET else None
            )
            for row in viewer_rows
        }

        return {
            comment_id: self.build_comment_reaction_payload(
                counts_by_emoji=counts_map.get(comment_id),
                my_reaction=my_reactions.get(comment_id),
            )
            for comment_id in normalized_ids
        }

    def toggle_comment_reaction(
        self,
        *,
        comment_id: int,
        user_id: int,
        emoji: str,
    ) -> dict[str, Any]:
        normalized_emoji = safe_text(emoji)
        now = utc_now()
        with self.lock:
            existing = self.conn.execute(
                """
                SELECT id, emoji
                FROM comment_reactions
                WHERE comment_id = ? AND user_id = ?
                """,
                (int(comment_id), int(user_id)),
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    """
                    INSERT INTO comment_reactions (
                        comment_id,
                        user_id,
                        emoji,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        int(comment_id),
                        int(user_id),
                        normalized_emoji,
                        now,
                        now,
                    ),
                )
            elif safe_text(existing["emoji"]) == normalized_emoji:
                self.conn.execute(
                    "DELETE FROM comment_reactions WHERE id = ?",
                    (int(existing["id"]),),
                )
            else:
                self.conn.execute(
                    """
                    UPDATE comment_reactions
                    SET emoji = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        normalized_emoji,
                        now,
                        int(existing["id"]),
                    ),
                )
            self.conn.commit()
        return self.list_comment_reaction_summaries(
            [int(comment_id)],
            viewer_user_id=int(user_id),
        ).get(int(comment_id), self.build_comment_reaction_payload())

    def set_comment_discussion_copy_message_id(
        self,
        *,
        comment_id: int,
        post_message_id: str,
        discussion_copy_message_id: str,
    ) -> None:
        with self.lock:
            self.conn.execute(
                """
                UPDATE comments
                SET discussion_copy_message_id = ?
                WHERE id = ? AND post_message_id = ?
                """,
                (discussion_copy_message_id, comment_id, post_message_id),
            )
            self.conn.commit()

    def list_comments_by_ids(self, comment_ids: list[int]) -> dict[int, sqlite3.Row]:
        normalized_ids = [int(comment_id) for comment_id in comment_ids if comment_id is not None]
        if not normalized_ids:
            return {}
        placeholders = ", ".join("?" for _ in normalized_ids)
        with self.lock:
            rows = self.conn.execute(
                f"SELECT * FROM comments WHERE id IN ({placeholders})",
                tuple(normalized_ids),
            ).fetchall()
        return {int(row["id"]): row for row in rows}

    def delete_comment(
        self,
        *,
        comment_id: int,
        post_message_id: str,
        deleted_by_user_id: int | None = None,
        reason: str | None = None,
    ) -> sqlite3.Row | None:
        now = utc_now()
        with self.lock:
            row = self.conn.execute(
                """
                SELECT *
                FROM comments
                WHERE id = ? AND post_message_id = ?
                """,
                (comment_id, post_message_id),
            ).fetchone()
            if row is None:
                return None
            was_active = safe_text(row["status"] if "status" in row.keys() else COMMENT_STATUS_ACTIVE) == COMMENT_STATUS_ACTIVE
            self.conn.execute(
                """
                UPDATE comments
                SET status = 'deleted',
                    deleted_at = COALESCE(deleted_at, ?),
                    deleted_by_user_id = COALESCE(?, deleted_by_user_id),
                    delete_reason = COALESCE(?, delete_reason)
                WHERE id = ?
                """,
                (
                    now,
                    int(deleted_by_user_id) if deleted_by_user_id is not None else None,
                    safe_text(reason) or None,
                    comment_id,
                ),
            )
            if was_active:
                self.conn.execute(
                    """
                    UPDATE posts
                    SET comment_count = CASE
                        WHEN comment_count > 0 THEN comment_count - 1
                        ELSE 0
                    END,
                    updated_at = ?
                    WHERE post_message_id = ?
                    """,
                    (now, post_message_id),
                )
            self.conn.commit()
        return row

    def set_comment_status(
        self,
        *,
        comment_id: int,
        status: str,
        moderator_user_id: int,
        reason: str | None = None,
    ) -> sqlite3.Row | None:
        normalized_status = safe_text(status)
        if normalized_status not in COMMENT_STATUSES:
            raise ValueError("Invalid comment status")
        now = utc_now()
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM comments WHERE id = ?",
                (int(comment_id),),
            ).fetchone()
            if row is None:
                return None
            old_status = safe_text(row["status"] if "status" in row.keys() else COMMENT_STATUS_ACTIVE)
            if old_status == normalized_status:
                return row
            deleted_at = now if normalized_status in {COMMENT_STATUS_DELETED, COMMENT_STATUS_HIDDEN} else None
            deleted_by_user_id = int(moderator_user_id) if normalized_status in {COMMENT_STATUS_DELETED, COMMENT_STATUS_HIDDEN} else None
            self.conn.execute(
                """
                UPDATE comments
                SET status = ?,
                    deleted_at = ?,
                    deleted_by_user_id = ?,
                    delete_reason = ?
                WHERE id = ?
                """,
                (
                    normalized_status,
                    deleted_at,
                    deleted_by_user_id,
                    safe_text(reason) or None,
                    int(comment_id),
                ),
            )
            if old_status == COMMENT_STATUS_ACTIVE and normalized_status != COMMENT_STATUS_ACTIVE:
                self.conn.execute(
                    """
                    UPDATE posts
                    SET comment_count = CASE WHEN comment_count > 0 THEN comment_count - 1 ELSE 0 END,
                        updated_at = ?
                    WHERE post_message_id = ?
                    """,
                    (now, safe_text(row["post_message_id"])),
                )
            elif old_status != COMMENT_STATUS_ACTIVE and normalized_status == COMMENT_STATUS_ACTIVE:
                self.conn.execute(
                    """
                    UPDATE posts
                    SET comment_count = comment_count + 1,
                        updated_at = ?
                    WHERE post_message_id = ?
                    """,
                    (now, safe_text(row["post_message_id"])),
                )
            self.conn.commit()
            return self.conn.execute(
                "SELECT * FROM comments WHERE id = ?",
                (int(comment_id),),
            ).fetchone()

    def update_comment_text(
        self,
        *,
        comment_id: int,
        post_message_id: str,
        text: str,
    ) -> sqlite3.Row | None:
        now = utc_now()
        with self.lock:
            row = self.conn.execute(
                """
                SELECT *
                FROM comments
                WHERE id = ? AND post_message_id = ?
                """,
                (comment_id, post_message_id),
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                """
                UPDATE comments
                SET text = ?
                WHERE id = ?
                """,
                (text, comment_id),
            )
            self.conn.execute(
                """
                UPDATE posts
                SET updated_at = ?
                WHERE post_message_id = ?
                """,
                (now, post_message_id),
            )
            self.conn.commit()
            return self.conn.execute(
                "SELECT * FROM comments WHERE id = ?",
                (comment_id,),
            ).fetchone()

    def count_reports_for_comment(self, comment_id: int) -> int:
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS count FROM comment_reports WHERE comment_id = ?",
                (int(comment_id),),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def list_admin_comments(
        self,
        *,
        channel_ids: set[int],
        post_message_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        normalized_ids = sorted({int(channel_id) for channel_id in channel_ids})
        if not normalized_ids:
            return []
        placeholders = ", ".join("?" for _ in normalized_ids)
        query = [
            """
            SELECT
                comments.*,
                posts.channel_chat_id AS channel_chat_id,
                posts.post_text AS post_text,
                posts.post_title AS post_title,
                COALESCE(report_counts.report_count, 0) AS report_count
            FROM comments
            JOIN posts ON posts.post_message_id = comments.post_message_id
            LEFT JOIN (
                SELECT comment_id, COUNT(*) AS report_count
                FROM comment_reports
                GROUP BY comment_id
            ) AS report_counts ON report_counts.comment_id = comments.id
            """,
            f"WHERE posts.channel_chat_id IN ({placeholders})",
        ]
        params: list[Any] = list(normalized_ids)
        if post_message_id:
            query.append("AND comments.post_message_id = ?")
            params.append(safe_text(post_message_id))
        if status:
            query.append("AND comments.status = ?")
            params.append(safe_text(status))
        query.append("ORDER BY comments.created_at DESC")
        query.append("LIMIT ?")
        params.append(max(1, min(int(limit), 500)))
        with self.lock:
            rows = self.conn.execute("\n".join(query), tuple(params)).fetchall()
        return list(rows)

    def create_comment_report(
        self,
        *,
        comment_id: int,
        post_id: str,
        channel_id: int,
        reporter_user_id: int,
        reason: str,
        details: str | None,
    ) -> int:
        now = utc_now()
        with self.lock:
            cursor = self.conn.execute(
                """
                INSERT INTO comment_reports (
                    comment_id,
                    post_id,
                    channel_id,
                    reporter_user_id,
                    reason,
                    details,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?)
                """,
                (
                    int(comment_id),
                    safe_text(post_id),
                    int(channel_id),
                    int(reporter_user_id),
                    safe_text(reason),
                    safe_text(details) or None,
                    now,
                    now,
                ),
            )
            self.conn.commit()
            return int(cursor.lastrowid)

    def list_reports(
        self,
        *,
        channel_ids: set[int],
        status: str | None = None,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        normalized_ids = sorted({int(channel_id) for channel_id in channel_ids})
        if not normalized_ids:
            return []
        placeholders = ", ".join("?" for _ in normalized_ids)
        query = [
            """
            SELECT
                comment_reports.*,
                comments.text AS comment_text,
                comments.display_name AS comment_author,
                comments.username AS comment_username,
                comments.created_at AS comment_created_at,
                comments.status AS comment_status,
                posts.post_text AS post_text,
                posts.post_title AS post_title,
                COALESCE(report_counts.report_count, 0) AS report_count
            FROM comment_reports
            JOIN comments ON comments.id = comment_reports.comment_id
            JOIN posts ON posts.post_message_id = comment_reports.post_id
            LEFT JOIN (
                SELECT comment_id, COUNT(*) AS report_count
                FROM comment_reports
                GROUP BY comment_id
            ) AS report_counts ON report_counts.comment_id = comment_reports.comment_id
            """,
            f"WHERE comment_reports.channel_id IN ({placeholders})",
        ]
        params: list[Any] = list(normalized_ids)
        if status:
            query.append("AND comment_reports.status = ?")
            params.append(safe_text(status))
        query.append("ORDER BY comment_reports.created_at DESC")
        query.append("LIMIT ?")
        params.append(max(1, min(int(limit), 500)))
        with self.lock:
            rows = self.conn.execute("\n".join(query), tuple(params)).fetchall()
        return list(rows)

    def get_report(self, report_id: int) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(
                """
                SELECT
                    comment_reports.*,
                    comments.text AS comment_text,
                    comments.display_name AS comment_author,
                    comments.username AS comment_username,
                    comments.created_at AS comment_created_at,
                    comments.status AS comment_status,
                    comments.media_json AS comment_media_json,
                    posts.post_text AS post_text,
                    posts.post_title AS post_title,
                    COALESCE(report_counts.report_count, 0) AS report_count
                FROM comment_reports
                JOIN comments ON comments.id = comment_reports.comment_id
                JOIN posts ON posts.post_message_id = comment_reports.post_id
                LEFT JOIN (
                    SELECT comment_id, COUNT(*) AS report_count
                    FROM comment_reports
                    GROUP BY comment_id
                ) AS report_counts ON report_counts.comment_id = comment_reports.comment_id
                WHERE comment_reports.id = ?
                """,
                (int(report_id),),
            ).fetchone()

    def update_report(
        self,
        *,
        report_id: int,
        status: str,
        admin_comment: str | None,
        resolved_by_user_id: int | None,
    ) -> sqlite3.Row | None:
        now = utc_now()
        resolved_at = now if status in {"accepted", "rejected"} else None
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM comment_reports WHERE id = ?",
                (int(report_id),),
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                """
                UPDATE comment_reports
                SET status = ?,
                    admin_comment = COALESCE(?, admin_comment),
                    resolved_by_user_id = ?,
                    resolved_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    safe_text(status),
                    safe_text(admin_comment) or None,
                    int(resolved_by_user_id) if resolved_by_user_id is not None and status in {"accepted", "rejected"} else None,
                    resolved_at,
                    now,
                    int(report_id),
                ),
            )
            self.conn.commit()
        return self.get_report(report_id)

    def dashboard_stats(self, *, channel_ids: set[int]) -> dict[str, int]:
        normalized_ids = sorted({int(channel_id) for channel_id in channel_ids})
        if not normalized_ids:
            return {
                "usersCount": 0,
                "postsCount": 0,
                "commentsCount": 0,
                "reportsCount": 0,
                "newReportsCount": 0,
                "deletedCommentsCount": 0,
            }
        placeholders = ", ".join("?" for _ in normalized_ids)
        with self.lock:
            users_count = self.conn.execute(
                f"""
                SELECT COUNT(DISTINCT comments.user_id) AS count
                FROM comments
                JOIN posts ON posts.post_message_id = comments.post_message_id
                WHERE posts.channel_chat_id IN ({placeholders})
                  AND comments.status = 'active'
                """,
                tuple(normalized_ids),
            ).fetchone()["count"]
            posts_count = self.conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM posts
                WHERE channel_chat_id IN ({placeholders})
                  AND status = 'published'
                """,
                tuple(normalized_ids),
            ).fetchone()["count"]
            comments_count = self.conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM comments
                JOIN posts ON posts.post_message_id = comments.post_message_id
                WHERE posts.channel_chat_id IN ({placeholders})
                  AND comments.status = 'active'
                """,
                tuple(normalized_ids),
            ).fetchone()["count"]
            reports_count = self.conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM comment_reports
                WHERE channel_id IN ({placeholders})
                """,
                tuple(normalized_ids),
            ).fetchone()["count"]
            new_reports_count = self.conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM comment_reports
                WHERE channel_id IN ({placeholders})
                  AND status = 'new'
                """,
                tuple(normalized_ids),
            ).fetchone()["count"]
            deleted_comments_count = self.conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM comments
                JOIN posts ON posts.post_message_id = comments.post_message_id
                WHERE posts.channel_chat_id IN ({placeholders})
                  AND comments.status IN ('deleted', 'hidden')
                """,
                tuple(normalized_ids),
            ).fetchone()["count"]
        return {
            "usersCount": int(users_count or 0),
            "postsCount": int(posts_count or 0),
            "commentsCount": int(comments_count or 0),
            "reportsCount": int(reports_count or 0),
            "newReportsCount": int(new_reports_count or 0),
            "deletedCommentsCount": int(deleted_comments_count or 0),
        }

    def add_admin_audit_log(
        self,
        *,
        admin_user_id: int,
        channel_id: int,
        action: str,
        entity_type: str,
        entity_id: str | int | None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO admin_audit_log (
                    admin_user_id,
                    channel_id,
                    action,
                    entity_type,
                    entity_id,
                    payload,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(admin_user_id),
                    int(channel_id),
                    safe_text(action),
                    safe_text(entity_type),
                    safe_text(entity_id) if entity_id is not None else None,
                    json.dumps(payload or {}, ensure_ascii=False),
                    utc_now(),
                ),
            )
            self.conn.commit()


def validate_webapp_init_data(
    raw_init_data: str,
    *,
    bot_token: str,
    max_age_seconds: int,
) -> AuthenticatedWebAppUser:
    init_data = safe_text(raw_init_data)
    if not init_data:
        raise WebAppAuthError("initData is required")

    if "#" in init_data:
        fragment = init_data.split("#", 1)[1]
        top_level = parse.parse_qsl(fragment, keep_blank_values=True)
        top_level_map = {key: value for key, value in top_level}
        init_data = top_level_map.get("WebAppData", "")
    elif init_data.startswith("WebAppData="):
        top_level = parse.parse_qsl(init_data, keep_blank_values=True)
        top_level_map = {key: value for key, value in top_level}
        init_data = top_level_map.get("WebAppData", "")

    params = parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    if not params:
        raise WebAppAuthError("initData has no parameters")

    seen_keys: dict[str, int] = {}
    for key, _ in params:
        seen_keys[key] = seen_keys.get(key, 0) + 1
    duplicates = [key for key, count in seen_keys.items() if count != 1]
    if duplicates:
        raise WebAppAuthError("initData contains duplicate keys")

    raw_map = dict(params)
    original_hash = raw_map.get("hash")
    if not original_hash:
        raise WebAppAuthError("initData hash is missing")

    launch_params = "\n".join(
        f"{key}={value}"
        for key, value in sorted((key, value) for key, value in params if key != "hash")
    )
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(
        secret_key,
        launch_params.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(calculated_hash, original_hash):
        raise WebAppAuthError("initData signature mismatch")

    auth_date = int(raw_map.get("auth_date", "0"))
    now_ts = int(time.time())
    if auth_date <= 0 or now_ts - auth_date > max_age_seconds:
        raise WebAppAuthError("initData is expired")

    user_payload = json.loads(raw_map.get("user", "{}"))
    if not user_payload or "id" not in user_payload:
        raise WebAppAuthError("initData user payload is missing")

    chat_payload: dict[str, Any] = {}
    if raw_map.get("chat"):
        try:
            chat_payload = json.loads(raw_map["chat"])
        except json.JSONDecodeError:
            chat_payload = {}

    display_name = MaxCommentsBot.user_display_name(user_payload)
    return AuthenticatedWebAppUser(
        user_id=int(user_payload["id"]),
        display_name=display_name,
        username=safe_text(user_payload.get("username")) or None,
        platform=safe_text(raw_map.get("platform")) or "max",
        chat_id=int(chat_payload["id"]) if "id" in chat_payload else None,
        chat_type=safe_text(chat_payload.get("type")) or None,
    )


class MaxCommentsBot:
    def __init__(self) -> None:
        self.api = MaxApiClient(BOT_TOKEN, MAX_API_BASE_URL)
        self.store = CommentStore(DATABASE_PATH)
        self.bootstrap_admin_users()
        self.bootstrap_legacy_channel_binding()
        self.delivery_mode = self.resolve_delivery_mode()
        self.marker: int | None = None
        self.bot_info: dict[str, Any] | None = None
        self.web_server: CommentWebServer | None = None
        self.channel_sync_thread: threading.Thread | None = None
        self.bind_cleanup_thread: threading.Thread | None = None
        self.update_queue: queue.Queue = queue.Queue()
        self.update_worker_thread: threading.Thread | None = None
        self.chat_info_cache: dict[int, tuple[dict[str, str], float]] = {}

    def run(self) -> None:
        logger.info("Starting MAX Comments bot version %s", APP_VERSION)
        logger.info("Delivery mode: %s", self.delivery_mode)
        self.bot_info = self.api.get_me()
        logger.info(
            "Connected as %s (@%s)",
            self.bot_info.get("first_name") or self.bot_info.get("name"),
            self.bot_info.get("username"),
        )
        self.purge_expired_bind_codes()
        self.start_update_worker()
        self.start_web_server()
        self.configure_delivery_mode()
        self.start_channel_sync()
        self.start_bind_cleanup()
        if self.delivery_mode == "webhook":
            self.run_webhook_loop()
            return
        self.run_polling_loop()

    def resolve_delivery_mode(self) -> str:
        mode = safe_text(DELIVERY_MODE).lower() or "polling"
        if mode not in SUPPORTED_DELIVERY_MODES:
            raise RuntimeError(
                f"Unsupported MAX_DELIVERY_MODE={DELIVERY_MODE!r}. "
                f"Use one of: {', '.join(sorted(SUPPORTED_DELIVERY_MODES))}"
            )
        return mode

    def webhook_url(self) -> str:
        return safe_text(WEBHOOK_PUBLIC_URL)

    def webhook_secret(self) -> str:
        return safe_text(WEBHOOK_SECRET)

    def start_update_worker(self) -> None:
        if self.update_worker_thread is not None:
            return
        self.update_worker_thread = threading.Thread(
            target=self.update_worker_loop,
            name="update-worker",
            daemon=True,
        )
        self.update_worker_thread.start()

    def update_worker_loop(self) -> None:
        while True:
            update = self.update_queue.get()
            try:
                self.handle_update(update)
            except Exception:
                logger.exception("Update worker failed")
            finally:
                self.update_queue.task_done()

    def enqueue_update(self, update: dict[str, Any]) -> None:
        if not isinstance(update, dict):
            return
        self.update_queue.put(update)

    def configure_delivery_mode(self) -> None:
        if self.delivery_mode == "webhook":
            self.ensure_webhook_ready()
            self.ensure_webhook_subscription()
            return
        self.disable_matching_webhook_subscription_if_present()

    def run_webhook_loop(self) -> None:
        logger.info("Webhook delivery is active on %s", self.webhook_url())
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            logger.info("Shutting down")

    def run_polling_loop(self) -> None:
        while True:
            try:
                payload = self.api.get_updates(self.marker)
                self.marker = payload.get("marker", self.marker)
                for update in payload.get("updates", []):
                    self.enqueue_update(update)
            except KeyboardInterrupt:
                logger.info("Shutting down")
                break
            except Exception:
                logger.exception("Polling loop failed, retrying in 3 seconds")
                time.sleep(3)

    def ensure_webhook_ready(self) -> None:
        webhook_url = self.webhook_url()
        if not webhook_url:
            raise RuntimeError(
                "MAX_WEBHOOK_PUBLIC_URL or MAX_WEB_APP_PUBLIC_URL must be configured for webhook mode"
            )
        if not webhook_url.startswith("https://"):
            raise RuntimeError("Webhook mode requires an HTTPS webhook URL")
        if not WEB_SERVER_ENABLED:
            raise RuntimeError("Webhook mode requires MAX_WEB_SERVER_ENABLED=1")
        if WEBHOOK_PATH and not self.webhook_url().endswith(WEBHOOK_PATH):
            logger.warning(
                "Webhook URL %s does not end with configured path %s",
                webhook_url,
                WEBHOOK_PATH,
            )
        if not self.webhook_secret():
            logger.warning("MAX_WEBHOOK_SECRET is empty. Webhook requests will not be secret-protected")

    def ensure_webhook_subscription(self) -> None:
        webhook_url = self.webhook_url()
        subscriptions = self.api.get_subscriptions()
        found_current = False
        for subscription in subscriptions:
            url = safe_text(subscription.get("url"))
            if not url:
                continue
            if url == webhook_url:
                found_current = True
            try:
                self.api.delete_subscription(url=url)
                if url == webhook_url:
                    logger.info("Removed existing MAX webhook subscription for refresh: %s", url)
                else:
                    logger.info("Removed stale MAX webhook subscription: %s", url)
            except MaxApiError:
                if url == webhook_url:
                    logger.exception("Failed to refresh existing MAX webhook subscription: %s", url)
                else:
                    logger.exception("Failed to remove stale MAX webhook subscription: %s", url)
        if found_current:
            logger.info("Refreshing MAX webhook subscription for %s", webhook_url)
        result = self.api.create_subscription(
            url=webhook_url,
            update_types=WEBHOOK_UPDATE_TYPES,
            secret=self.webhook_secret() or None,
        )
        logger.info("Created MAX webhook subscription for %s: %s", webhook_url, result)

    def disable_matching_webhook_subscription_if_present(self) -> None:
        webhook_url = self.webhook_url()
        subscriptions = self.api.get_subscriptions()
        other_urls: list[str] = []
        for subscription in subscriptions:
            url = safe_text(subscription.get("url"))
            if not url:
                continue
            if not webhook_url or url != webhook_url:
                other_urls.append(url)
                continue
            try:
                self.api.delete_subscription(url=url)
                logger.info("Disabled MAX webhook subscription for polling mode: %s", url)
            except MaxApiError:
                logger.exception("Failed to disable MAX webhook subscription for polling mode: %s", url)
        if other_urls:
            logger.warning(
                "MAX still has webhook subscriptions configured while polling mode is active: %s",
                ", ".join(other_urls),
            )

    def start_web_server(self) -> None:
        if not WEB_SERVER_ENABLED:
            logger.info("Built-in web server is disabled")
            return
        self.web_server = CommentWebServer(self, WEB_SERVER_HOST, WEB_SERVER_PORT)
        self.web_server.start()
        logger.info("Web app server started on http://%s:%s", WEB_SERVER_HOST, WEB_SERVER_PORT)
        if WEB_APP_PUBLIC_URL:
            logger.info("Expected public WebApp URL: %s", WEB_APP_PUBLIC_URL)

    def bootstrap_legacy_channel_binding(self) -> None:
        if self.store.list_channel_bindings():
            return
        if TARGET_CHANNEL_CHAT_ID is None or COMMENTS_CHAT_ID is None:
            return
        self.store.upsert_channel_binding(
            channel_chat_id=int(TARGET_CHANNEL_CHAT_ID),
            comments_chat_id=int(COMMENTS_CHAT_ID),
            comments_chat_url=COMMENTS_CHAT_URL or None,
        )
        logger.info(
            "Bootstrapped channel binding from legacy env: %s -> %s",
            TARGET_CHANNEL_CHAT_ID,
            COMMENTS_CHAT_ID,
        )

    def bootstrap_admin_users(self) -> None:
        created_super_admins = 0
        for user_id in sorted({int(user_id) for user_id in SUPER_ADMIN_IDS}):
            existing = self.store.get_admin_user_by_max_user_id(user_id)
            self.store.ensure_admin_user(
                max_user_id=user_id,
                role=ROLE_SUPER_ADMIN,
                password=str(user_id),
                must_change_password=True,
                is_active=True,
            )
            if existing is None:
                created_super_admins += 1
        migrated_channel_admins = self.store.migrate_channel_admins_to_admin_users()
        if created_super_admins:
            logger.info("Created %s bootstrap super admin account(s)", created_super_admins)
        if migrated_channel_admins:
            logger.info("Created %s channel admin account(s) from channel bindings", migrated_channel_admins)

    def admin_notice_user_ids(self) -> list[int]:
        user_ids: list[int] = []
        for row in self.store.list_admin_users():
            if int(row["is_active"]) != 1:
                continue
            if safe_text(row["role"]) == ROLE_SUPER_ADMIN:
                try:
                    user_ids.append(int(row["max_user_id"]))
                except ValueError:
                    continue
        return sorted(set(user_ids))

    def has_channel_bindings(self) -> bool:
        return bool(self.store.list_channel_bindings())

    def get_channel_binding(self, channel_chat_id: int | None) -> sqlite3.Row | None:
        if channel_chat_id is None:
            return None
        try:
            return self.store.get_channel_binding(int(channel_chat_id))
        except (TypeError, ValueError):
            return None

    def list_channel_bindings(self) -> list[sqlite3.Row]:
        return self.store.list_channel_bindings()

    def current_chat_context(self, message: dict[str, Any]) -> tuple[int | None, str, str, str | None]:
        recipient = message.get("recipient") or {}
        chat_id = recipient.get("chat_id")
        title = safe_text(recipient.get("title") or recipient.get("name"))
        chat_type = safe_text(recipient.get("chat_type") or recipient.get("type"))
        chat_link = safe_text(recipient.get("link")) or None
        try:
            normalized_chat_id = int(chat_id) if chat_id is not None else None
        except (TypeError, ValueError):
            normalized_chat_id = None
        return (
            normalized_chat_id,
            title,
            chat_type,
            chat_link,
        )

    def message_text_or_payload(self, message: dict[str, Any]) -> str:
        body = message.get("body") or {}
        return safe_text(body.get("payload") or body.get("text"))

    def terms_links_text(self) -> str:
        return "\n".join(
            [
                "1) Типовое пользовательское соглашение для приложений на платформе «МАКС» "
                "https://dev.max.ru/docs/legal/agreement",
                "2) Пользовательское соглашение и политика конфиденциальности сервиса «МАКС» "
                "https://legal.max.ru/ps",
                "3) Публичная оферта бота ЦИТ",
                "4) Политика Конфиденциальности И Политика Cookie бота ЦИТ",
            ]
        )

    def send_terms_welcome(self, user_id: int) -> None:
        self.api.send_message(
            user_id=user_id,
            text=(
                "Это бот ЦИТ 😉\n"
                "Он поможет вам сделать комментарии под постами!\n\n"
                "Мы с уважением относимся к правилам МАКС. Перед использованием бота, пожалуйста, "
                "ознакомьтесь с материалами ниже и нажмите \"Принять\" если согласны с ними:\n"
                f"{self.terms_links_text()}\n\n"
                "Большое спасибо ❤️"
            ),
            attachments=self.build_link_keyboard(
                [
                    {
                        "type": "message",
                        "text": "Принять",
                        "payload": "/accept_terms",
                    }
                ]
            ),
        )

    def connection_instruction_text(self) -> str:
        return (
            "Для подключения комментариев к каналу:\n\n"
            "1. Добавьте этого бота в ваш канал.\n"
            "2. Назначьте бота администратором канала.\n"
            "3. Перешлите этому боту любой пост из подключаемого канала.\n"
            "4. Дождитесь одобрения заявки супер-администратором.\n"
            "5. После одобрения кнопка “Комментарии” будет автоматически добавляться к новым постам канала.\n\n"
            "Важно:\n"
            "Бот должен быть администратором канала, иначе он не сможет добавлять кнопку комментариев к постам."
        )

    def send_connection_instruction(self, user_id: int) -> None:
        self.api.send_message(user_id=user_id, text=self.connection_instruction_text())

    def accept_terms_for_user(self, user_id: int) -> None:
        self.store.accept_terms(max_user_id=user_id, version=TERMS_VERSION)
        self.api.send_message(user_id=user_id, text=self.connection_instruction_text())

    def ensure_user_accepted_terms(self, user_id: int) -> bool:
        if self.store.has_accepted_terms(max_user_id=user_id, version=TERMS_VERSION):
            return True
        self.send_terms_welcome(user_id)
        return False

    def nested_payload_value(self, payload: Any, *keys: str) -> Any:
        current = payload
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    def first_payload_value(self, payload: dict[str, Any], paths: list[tuple[str, ...]]) -> Any:
        for path in paths:
            value = self.nested_payload_value(payload, *path)
            if value is not None:
                return value
        return None

    def iter_nested_dicts(self, payload: Any) -> Iterable[dict[str, Any]]:
        stack: list[Any] = [payload]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                yield current
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)

    def forwarded_payload_candidates(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        body = message.get("body") if isinstance(message.get("body"), dict) else {}
        candidates: list[dict[str, Any]] = []
        for value in (
            message.get("link"),
            body.get("link"),
            body.get("forward"),
            body.get("forwarded_message"),
            body.get("shared_message"),
        ):
            if isinstance(value, dict):
                candidates.append(value)

        attachments = body.get("attachments")
        if isinstance(attachments, list):
            for attachment in attachments:
                candidates.extend(self.iter_nested_dicts(attachment))

        seen: set[int] = set()
        unique_candidates: list[dict[str, Any]] = []
        for candidate in candidates:
            marker = id(candidate)
            if marker in seen:
                continue
            seen.add(marker)
            unique_candidates.append(candidate)
        return unique_candidates

    def extract_channel_id_from_forward_payload(self, payload: dict[str, Any]) -> int | None:
        direct_value = self.first_payload_value(
            payload,
            [
                ("chat_id",),
                ("chatId",),
                ("channel_id",),
                ("channelId",),
                ("from_chat_id",),
                ("fromChatId",),
                ("source_chat_id",),
                ("sourceChatId",),
                ("sender_chat_id",),
                ("senderChatId",),
                ("chat", "chat_id"),
                ("chat", "chatId"),
                ("chat", "id"),
                ("channel", "chat_id"),
                ("channel", "chatId"),
                ("channel", "id"),
                ("sender_chat", "chat_id"),
                ("sender_chat", "chatId"),
                ("senderChat", "chatId"),
                ("senderChat", "id"),
                ("message", "recipient", "chat_id"),
                ("message", "recipient", "chatId"),
                ("message", "recipient", "id"),
                ("message", "chat_id"),
                ("message", "chatId"),
            ],
        )
        channel_id = validate_positive_int(direct_value, minimum=-10**18, maximum=10**18)
        if channel_id is not None and channel_id != 0:
            return channel_id

        id_keys = ("chat_id", "chatId", "channel_id", "channelId")
        for node in self.iter_nested_dicts(payload):
            node_value = self.first_payload_value(node, [(key,) for key in id_keys])
            node_channel_id = validate_positive_int(node_value, minimum=-10**18, maximum=10**18)
            if node_channel_id is None or node_channel_id == 0:
                continue
            node_type = safe_text(node.get("type") or node.get("chat_type") or node.get("chatType")).lower()
            has_chat_hint = bool(
                safe_text(node.get("title") or node.get("name") or node.get("chat_title") or node.get("channel_title"))
                or "channel" in node_type
                or "chat" in node_type
            )
            if node_channel_id < 0 or has_chat_hint:
                return node_channel_id
        return None

    def extract_channel_title_from_forward_payload(self, payload: dict[str, Any], channel_id: int) -> str:
        title = safe_text(
            self.first_payload_value(
                payload,
                [
                    ("chat", "title"),
                    ("chat", "name"),
                    ("channel", "title"),
                    ("channel", "name"),
                    ("sender_chat", "title"),
                    ("sender_chat", "name"),
                    ("senderChat", "title"),
                    ("senderChat", "name"),
                    ("channel_title",),
                    ("channelTitle",),
                    ("chat_title",),
                    ("chatTitle",),
                    ("message", "recipient", "title"),
                    ("message", "recipient", "name"),
                ],
            )
        )
        if title:
            return title

        for node in self.iter_nested_dicts(payload):
            node_channel_id = self.extract_channel_id_from_forward_payload(node)
            if node_channel_id != channel_id:
                continue
            title = safe_text(
                node.get("title")
                or node.get("name")
                or node.get("chat_title")
                or node.get("chatTitle")
                or node.get("channel_title")
                or node.get("channelTitle")
            )
            if title:
                return title
        return ""

    def first_nested_text_value(self, payload: dict[str, Any], keys: set[str]) -> str:
        for node in self.iter_nested_dicts(payload):
            for key in keys:
                value = safe_text(node.get(key))
                if value:
                    return value
        return ""

    def forward_payload_has_channel_hint(self, message: dict[str, Any]) -> bool:
        for candidate in self.forwarded_payload_candidates(message):
            if self.extract_channel_id_from_forward_payload(candidate) is not None:
                return True
            candidate_type = safe_text(candidate.get("type") or candidate.get("link_type") or candidate.get("linkType")).lower()
            if any(marker in candidate_type for marker in ("forward", "message", "share", "link")):
                return True
        return False

    def extract_forwarded_channel_post(self, message: dict[str, Any]) -> ForwardedChannelPost | None:
        for link in self.forwarded_payload_candidates(message):
            forwarded = self.extract_forwarded_channel_post_from_payload(message, link)
            if forwarded is not None:
                return forwarded
        return None

    def extract_forwarded_channel_post_from_payload(
        self,
        message: dict[str, Any],
        link: dict[str, Any],
    ) -> ForwardedChannelPost | None:
        channel_id = self.extract_channel_id_from_forward_payload(link)
        if channel_id is None or channel_id == 0:
            return None

        linked_message = link.get("message") if isinstance(link.get("message"), dict) else {}
        linked_body = linked_message.get("body") if isinstance(linked_message.get("body"), dict) else {}
        post_message_id = safe_text(
            linked_body.get("mid")
            or linked_message.get("mid")
            or link.get("mid")
            or linked_body.get("message_id")
            or linked_message.get("message_id")
            or link.get("message_id")
            or linked_body.get("messageId")
            or linked_message.get("messageId")
            or link.get("messageId")
            or self.first_nested_text_value(link, {"mid", "message_id", "messageId"})
        ) or None
        post_text = safe_text(
            linked_body.get("text")
            or linked_message.get("text")
            or link.get("text")
            or self.first_nested_text_value(link, {"text"})
        )
        post_url = safe_text(linked_message.get("url") or link.get("url") or self.first_nested_text_value(link, {"url"})) or None
        raw_attachments = linked_body.get("attachments") or linked_message.get("attachments") or []
        post_attachments = [item for item in raw_attachments if isinstance(item, dict)] if isinstance(raw_attachments, list) else []
        channel_title = self.extract_channel_title_from_forward_payload(link, int(channel_id))
        raw_payload = {
            "link": link,
            "body": message.get("body") or {},
            "recipient": message.get("recipient") or {},
        }
        return ForwardedChannelPost(
            channel_id=int(channel_id),
            channel_title=channel_title,
            post_message_id=post_message_id,
            post_text=post_text,
            post_url=post_url,
            post_attachments=post_attachments,
            raw_payload=raw_payload,
        )

    def chat_member_is_admin(self, member: dict[str, Any]) -> bool:
        role = safe_text(member.get("role") or member.get("status")).lower()
        permissions = member.get("permissions")
        has_permissions = bool(permissions) if isinstance(permissions, (list, tuple, set, dict)) else False
        return bool(
            member.get("is_admin")
            or member.get("isAdmin")
            or member.get("is_owner")
            or member.get("isOwner")
            or has_permissions
            or role in {"admin", "administrator", "owner", "creator"}
        )

    def member_user_id(self, member: dict[str, Any]) -> int | None:
        user = member.get("user") if isinstance(member.get("user"), dict) else {}
        return validate_positive_int(
            member.get("user_id") or member.get("userId") or user.get("user_id") or user.get("userId"),
            minimum=1,
            maximum=10**18,
        )

    def ensure_bot_can_manage_channel(self, channel_id: int) -> tuple[bool, str | None]:
        try:
            member = self.api.get_chat_member_me(channel_id)
        except MaxApiError as exc:
            logger.warning("Failed to check bot permissions in channel %s: %s", channel_id, exc)
            return False, "Проверьте, что бот добавлен в этот канал и назначен администратором."
        if not self.chat_member_is_admin(member):
            return False, "Бот добавлен в канал, но не назначен администратором."
        permissions = {
            safe_text(permission)
            for permission in (member.get("permissions") or [])
            if safe_text(permission)
        }
        if permissions and "write" not in permissions:
            return False, "У бота нет права писать сообщения в канале."
        edit_permissions = {"edit", "edit_message", "post_edit_delete_message", "delete", "delete_message"}
        if permissions and not permissions.intersection(edit_permissions):
            return False, "У бота нет права редактировать посты канала."
        return True, None

    def requester_is_channel_admin(self, channel_id: int, requester_user_id: int) -> bool:
        try:
            chat = self.api.get_chat(channel_id)
            owner_id = validate_positive_int(chat.get("owner_id") or chat.get("ownerId"), minimum=1, maximum=10**18)
            if owner_id is not None and int(owner_id) == int(requester_user_id):
                return True
        except MaxApiError:
            logger.exception("Failed to load channel owner for request validation: %s", channel_id)

        try:
            admins = self.api.get_chat_admins(channel_id)
        except MaxApiError:
            logger.exception("Failed to load channel admins for request validation: %s", channel_id)
            return False
        return any(self.member_user_id(admin) == int(requester_user_id) for admin in admins)

    def issue_bind_code(self) -> str:
        while True:
            code = uuid.uuid4().hex[:6].upper()
            if self.store.get_pending_channel_binding(code) is None:
                return code

    def purge_expired_bind_codes(self) -> None:
        threshold = datetime.fromtimestamp(
            time.time() - BIND_CHANNEL_CODE_TTL_SECONDS,
            tz=timezone.utc,
        ).isoformat()
        deleted_count = self.store.clear_expired_pending_channel_bindings(older_than_iso=threshold)
        if deleted_count:
            logger.info("Purged %s expired pending channel binding code(s)", deleted_count)

    def start_bind_cleanup(self) -> None:
        if self.bind_cleanup_thread is not None:
            return
        self.bind_cleanup_thread = threading.Thread(
            target=self.bind_cleanup_loop,
            name="bind-cleanup",
            daemon=True,
        )
        self.bind_cleanup_thread.start()
        logger.info(
            "Pending bind-code cleanup started every %s seconds",
            BIND_CHANNEL_CLEANUP_INTERVAL_SECONDS,
        )

    def bind_cleanup_loop(self) -> None:
        while True:
            try:
                self.purge_expired_bind_codes()
            except Exception:
                logger.exception("Pending bind-code cleanup failed")
            time.sleep(max(BIND_CHANNEL_CLEANUP_INTERVAL_SECONDS, 60))

    def get_valid_pending_channel_binding(self, bind_code: str) -> PendingChannelBinding | None:
        self.purge_expired_bind_codes()
        pending = self.store.get_pending_channel_binding(bind_code)
        if pending is None:
            return None
        try:
            created_at = datetime.fromisoformat(pending.created_at)
        except ValueError:
            self.store.delete_pending_channel_binding(bind_code)
            return None
        if (datetime.now(timezone.utc) - created_at).total_seconds() > BIND_CHANNEL_CODE_TTL_SECONDS:
            self.store.delete_pending_channel_binding(bind_code)
            return None
        return pending

    def resolve_post_comments_chat_id(self, post: sqlite3.Row) -> int | None:
        raw_comments_chat_id = post["comments_chat_id"] if "comments_chat_id" in post.keys() else None
        if raw_comments_chat_id is not None:
            comments_chat_id = int(raw_comments_chat_id)
            return comments_chat_id if comments_chat_id > 0 else None

        channel_chat_id = int(post["channel_chat_id"])
        binding = self.get_channel_binding(channel_chat_id)
        if binding is not None:
            comments_chat_id = int(binding["comments_chat_id"])
            return comments_chat_id if comments_chat_id > 0 else None
        if COMMENTS_CHAT_ID is not None:
            return int(COMMENTS_CHAT_ID)
        raise MaxApiError("Comments chat is not configured for this post")

    def start_channel_sync(self) -> None:
        if not self.has_channel_bindings():
            logger.info("Channel auto-attach sync is disabled until at least one channel binding is configured")
            return
        if self.channel_sync_thread is not None:
            return
        self.channel_sync_thread = threading.Thread(
            target=self.channel_sync_loop,
            name="channel-sync",
            daemon=True,
        )
        self.channel_sync_thread.start()
        logger.info(
            "Channel auto-attach sync started for %s channel(s) every %s seconds",
            len(self.list_channel_bindings()),
            CHANNEL_SYNC_INTERVAL_SECONDS,
        )

    def channel_sync_loop(self) -> None:
        while True:
            try:
                attached_count = self.sync_recent_channel_posts()
                if attached_count:
                    logger.info("Channel sync attached buttons to %s post(s)", attached_count)
            except Exception:
                logger.exception("Channel auto-attach sync failed")
            time.sleep(max(CHANNEL_SYNC_INTERVAL_SECONDS, 1))

    def handle_update(self, update: dict[str, Any]) -> None:
        update_type = safe_text(update.get("update_type"))
        if update_type != "message_created":
            logger.info("Ignored update type: %s", update_type or "<empty>")
            return
        self.handle_new_message(update.get("message") or {})

    def handle_new_message(self, message: dict[str, Any]) -> None:
        text = self.message_text_or_payload(message)
        sender = message.get("sender") or {}
        user_id = sender.get("user_id")
        if self.is_own_message(sender):
            return
        if self.maybe_auto_attach_channel_post(message):
            return
        if not user_id:
            if text.startswith("/") and self.handle_senderless_channel_command(message, text):
                return
            return

        if text.startswith("/"):
            self.handle_command(message, text)
            return

        if text in {"Принять", "Принять и продолжить", "accept_terms", "/accept_terms"}:
            self.accept_terms_for_user(int(user_id))
            return

        if self.handle_forwarded_channel_post_request(message, int(user_id)):
            return

        pending = self.store.pop_pending_comment(int(user_id))
        if pending is None:
            if self.store.has_accepted_terms(max_user_id=user_id, version=TERMS_VERSION):
                text = (
                    "Для подключения канала перешлите сюда любой пост из канала, "
                    "где бот уже добавлен администратором."
                )
            else:
                text = "Нажмите “Начать” или отправьте `/start`, чтобы принять условия и получить инструкцию."
            self.api.send_message(user_id=int(user_id), text=text)
            return

        self.save_user_comment(message, pending, text)

    def handle_forwarded_channel_post_request(self, message: dict[str, Any], user_id: int) -> bool:
        forwarded = self.extract_forwarded_channel_post(message)
        if forwarded is None:
            if self.forward_payload_has_channel_hint(message):
                body = message.get("body") if isinstance(message.get("body"), dict) else {}
                attachment_types = [
                    safe_text(item.get("type"))
                    for item in (body.get("attachments") or [])
                    if isinstance(item, dict)
                ]
                logger.warning(
                    "Forwarded channel request from user %s was not recognized. body_keys=%s attachment_types=%s",
                    user_id,
                    sorted(body.keys()),
                    attachment_types,
                )
                self.api.send_message(
                    user_id=user_id,
                    text=(
                        "Не удалось определить канал по пересланному сообщению.\n\n"
                        "Проверьте, что:\n"
                        "1. Сообщение переслано именно из канала.\n"
                        "2. Бот добавлен в этот канал.\n"
                        "3. Бот назначен администратором канала.\n\n"
                        "После этого попробуйте переслать пост ещё раз."
                    ),
                )
                return True
            return False
        if not self.ensure_user_accepted_terms(user_id):
            return True

        channel_id = int(forwarded.channel_id)
        if self.get_channel_binding(channel_id) is not None:
            logger.info("Connection request skipped because channel %s is already connected", channel_id)
            self.api.send_message(
                user_id=user_id,
                text=(
                    "Этот канал уже подключён к комментариям.\n\n"
                    f"Канал: `{forwarded.channel_title or channel_id}`\n"
                    f"Панель администратора канала: {public_app_url('/admin')}"
                ),
            )
            return True

        can_manage, manage_error = self.ensure_bot_can_manage_channel(channel_id)
        if not can_manage:
            logger.warning("Connection request for channel %s rejected: bot cannot manage channel: %s", channel_id, manage_error)
            self.api.send_message(
                user_id=user_id,
                text=(
                    "Не удалось определить канал по пересланному сообщению.\n\n"
                    "Проверьте, что:\n"
                    "1. Сообщение переслано именно из канала.\n"
                    "2. Бот добавлен в этот канал.\n"
                    "3. Бот назначен администратором канала.\n\n"
                    f"{manage_error or 'После этого попробуйте переслать пост ещё раз.'}"
                ),
            )
            return True

        if not self.requester_is_channel_admin(channel_id, user_id):
            logger.warning("Connection request for channel %s rejected: user %s is not channel admin", channel_id, user_id)
            self.api.send_message(
                user_id=user_id,
                text=(
                    "Заявку может отправить только администратор подключаемого канала.\n\n"
                    "Проверьте, что вы являетесь администратором канала, и попробуйте переслать пост ещё раз."
                ),
            )
            return True

        pending = self.store.get_pending_channel_connection_request(channel_id=channel_id)
        if pending is not None:
            logger.info("Connection request for channel %s skipped: pending request %s exists", channel_id, pending["id"])
            self.api.send_message(
                user_id=user_id,
                text=(
                    "Заявка по этому каналу уже находится на рассмотрении.\n\n"
                    f"Канал: {safe_text(pending['channel_title']) or channel_id}\n"
                    f"Статус: `{safe_text(pending['status'])}`"
                ),
            )
            return True

        channel_title = forwarded.channel_title
        if not channel_title:
            channel_title = self.get_chat_title_cached(channel_id)
        request_row = self.store.create_channel_connection_request(
            channel_id=channel_id,
            channel_title=channel_title or None,
            forwarded_post_id=forwarded.post_message_id,
            requester_user_id=user_id,
            requester_max_user_id=user_id,
            raw_payload=forwarded.raw_payload,
        )
        logger.info(
            "Created channel connection request %s for channel %s from user %s",
            request_row["id"],
            channel_id,
            user_id,
        )
        self.notify_super_admins_about_channel_request(request_row)
        self.api.send_message(
            user_id=user_id,
            text=(
                "Новая заявка на подключение канала\n"
                f"Канал: {channel_title or channel_id}\n"
                f"ID канала: {channel_id}\n"
                f"Ваш ID MAX: {user_id}\n"
                f"Адрес административной панели канала: {public_app_url('/admin')}"
            ),
        )
        return True

    def handle_senderless_channel_command(self, message: dict[str, Any], text: str) -> bool:
        command, _, raw_args = text.partition(" ")
        if command == "/bind_comments":
            return self.handle_senderless_bind_comments_command(message, raw_args.strip())
        if command not in {"/setup_channel", "/bind_channel"}:
            return False
        recipient = message.get("recipient") or {}
        try:
            channel_chat_id = int(recipient.get("chat_id"))
        except (TypeError, ValueError):
            return True

        try:
            chat = self.api.get_chat(channel_chat_id)
        except MaxApiError as exc:
            logger.exception("Failed to resolve senderless setup command chat: %s", channel_chat_id)
            self.api.send_message(
                chat_id=channel_chat_id,
                text=humanize_comment_error_message(str(exc)),
            )
            return True

        owner_id = validate_positive_int(
            chat.get("owner_id"),
            minimum=1,
            maximum=10**18,
        )
        if owner_id is None:
            self.api.send_message(
                chat_id=channel_chat_id,
                text=(
                    "Не удалось выполнить настройку: MAX не вернул владельца канала."
                ),
            )
            return True

        synthetic_message = dict(message)
        synthetic_message["sender"] = {"user_id": owner_id}
        synthetic_recipient = dict(recipient)
        synthetic_recipient.update(
            {
                "chat_id": channel_chat_id,
                "chat_type": safe_text(chat.get("type")) or safe_text(recipient.get("chat_type")),
                "title": chat_title_from_payload(chat) or safe_text(recipient.get("title")),
                "link": safe_text(chat.get("link")) or safe_text(recipient.get("link")),
            }
        )
        synthetic_message["recipient"] = synthetic_recipient
        self.begin_channel_binding(
            user_id=owner_id,
            message=synthetic_message,
            args=raw_args.strip(),
            response_chat_id=channel_chat_id,
        )
        return True

    def handle_senderless_bind_comments_command(self, message: dict[str, Any], args: str) -> bool:
        recipient = message.get("recipient") or {}
        try:
            comments_chat_id = int(recipient.get("chat_id"))
        except (TypeError, ValueError):
            return True

        bind_code = safe_text(args).upper()
        if not bind_code:
            self.api.send_message(
                chat_id=comments_chat_id,
                text="Формат: `/bind_comments CODE`",
            )
            return True

        pending = self.get_valid_pending_channel_binding(bind_code)
        if pending is None:
            self.api.send_message(
                chat_id=comments_chat_id,
                text="Код привязки не найден или уже истёк. Запустите `/setup_channel` заново в канале.",
            )
            return True

        try:
            chat = self.api.get_chat(comments_chat_id)
        except MaxApiError as exc:
            logger.exception("Failed to resolve senderless comments chat: %s", comments_chat_id)
            self.api.send_message(
                chat_id=comments_chat_id,
                text=humanize_comment_error_message(str(exc)),
            )
            return True

        synthetic_message = dict(message)
        synthetic_message["sender"] = {"user_id": pending.requested_by_user_id}
        synthetic_recipient = dict(recipient)
        synthetic_recipient.update(
            {
                "chat_id": comments_chat_id,
                "chat_type": safe_text(chat.get("type")) or safe_text(recipient.get("chat_type")),
                "title": chat_title_from_payload(chat) or safe_text(recipient.get("title")),
                "link": safe_text(chat.get("link")) or safe_text(recipient.get("link")),
            }
        )
        synthetic_message["recipient"] = synthetic_recipient
        self.complete_channel_binding(
            user_id=pending.requested_by_user_id,
            message=synthetic_message,
            args=bind_code,
            response_chat_id=comments_chat_id,
        )
        return True

    def handle_command(self, message: dict[str, Any], text: str) -> None:
        sender = message.get("sender") or {}
        user_id = int(sender["user_id"])
        command, _, raw_args = text.partition(" ")
        args = raw_args.strip()

        if command == "/start":
            if self.store.has_accepted_terms(max_user_id=user_id, version=TERMS_VERSION):
                self.send_connection_instruction(user_id)
            else:
                self.send_terms_welcome(user_id)
            return

        if command == "/accept_terms":
            self.accept_terms_for_user(user_id)
            return

        if command == "/help":
            self.api.send_message(
                user_id=user_id,
                text=(
                    "Основной способ подключения канала:\n"
                    "1. Отправьте `/start`.\n"
                    "2. Примите условия.\n"
                    "3. Добавьте бота администратором в канал.\n"
                    "4. Перешлите боту любой пост из канала.\n"
                    "5. Дождитесь одобрения заявки.\n\n"
                    "Команды администратора:\n"
                    "`/publish текст поста` - опубликовать пост в единственный подключённый канал или в текущий канал\n"
                    "`/publish CHANNEL_ID текст поста` - опубликовать пост в выбранный подключённый канал\n"
                    "`/attach MESSAGE_ID` - подключить мини-приложение к уже существующему посту\n"
                    "`/channels` - показать подключённые каналы\n"
                    "`/channel_add CHANNEL_ID COMMENTS_CHAT_ID` - подключить канал к чату комментариев\n"
                    "`/channel_remove CHANNEL_ID` - отключить канал\n"
                    "`/posts` - показать последние зарегистрированные посты\n"
                    "`/bind_status` - показать активные коды незавершённых привязок\n"
                    "`/chatinfo` - показать данные текущего чата\n\n"
                    "Быстрая привязка без ручного ввода ID:\n"
                    "`/setup_channel` - создать канал в админке и начать подключение\n"
                    "`/bind_channel` - отправить прямо в канале\n"
                    "`/bind_comments CODE` - отправить в чате комментариев\n"
                    "`/setup_channel same` - если комментарии должны жить в этом же чате\n\n"
                    "Если администратор публикует пост вручную прямо в канале, кнопка комментариев тоже добавится автоматически.\n\n"
                    "Диагностика:\n"
                    "`/me` - показать мой user id"
                ),
            )
            return

        if command == "/me":
            self.api.send_message(
                user_id=user_id,
                text=(
                    "Данные пользователя:\n"
                    f"`user_id`: `{user_id}`\n"
                    f"`display_name`: `{self.user_display_name(sender)}`\n"
                    f"`username`: `{safe_text(sender.get('username')) or '-'}'"
                ),
            )
            return

        if command == "/chatinfo":
            self.send_chat_info(user_id, message)
            return

        if command == "/posts":
            context = self.admin_context_for_user(user_id)
            if context is None:
                self.api.send_message(
                    user_id=user_id,
                    text="Эта команда доступна только администратору канала.",
                )
                return
            self.send_posts_overview(user_id, context=context)
            return

        if command == "/comment":
            if not args:
                self.api.send_message(
                    user_id=user_id,
                    text="Используйте кнопку `Комментарии` под постом или формат `/comment cXXXXXX`.",
                )
                return
            post = self.resolve_post_reference(args)
            if post is None:
                self.api.send_message(
                    user_id=user_id,
                    text="Я не нашёл этот пост в базе.",
                )
                return
            self.store.set_pending_comment(user_id, post["post_message_id"])
            self.api.send_message(
                user_id=user_id,
                text=(
                    f"Напишите следующий сообщением комментарий к посту:\n"
                    f"`{snippet(post['post_text'], 180)}`"
                ),
            )
            return

        if command in {"/bind_channel", "/setup_channel"}:
            self.begin_channel_binding(user_id=user_id, message=message, args=args)
            return

        if command == "/bind_comments":
            self.complete_channel_binding(user_id=user_id, message=message, args=args)
            return

        if command == "/bind_status":
            self.send_pending_channel_bindings_status(user_id)
            return

        if not self.is_super_admin_user(user_id):
            self.api.send_message(
                user_id=user_id,
                text=(
                    "Эта команда доступна только администратору.\n\n"
                    f"{self.setup_status_text()}"
                ),
            )
            return

        if command == "/publish":
            if not self.ensure_publish_ready(user_id):
                return
            binding, publish_text, error_text = self.resolve_publish_command_target(message, args)
            if error_text:
                self.api.send_message(user_id=user_id, text=error_text)
                return
            try:
                self.publish_post(
                    publish_text,
                    admin_user_id=user_id,
                    channel_chat_id=int(binding["channel_chat_id"]),
                )
            except MaxApiError as exc:
                self.api.send_message(user_id=user_id, text=humanize_comment_error_message(str(exc)))
            return

        if command == "/attach":
            if not self.ensure_publish_ready(user_id):
                return
            if not args:
                self.api.send_message(user_id=user_id, text="Формат: `/attach MESSAGE_ID`")
                return
            try:
                self.attach_existing_post(args, admin_user_id=user_id)
            except MaxApiError as exc:
                self.api.send_message(user_id=user_id, text=humanize_comment_error_message(str(exc)))
            return

        if command == "/channels":
            self.send_channel_bindings_overview(user_id)
            return

        if command == "/channel_add":
            if not args:
                self.api.send_message(
                    user_id=user_id,
                    text="Формат: `/channel_add CHANNEL_ID COMMENTS_CHAT_ID`",
                )
                return
            self.add_channel_binding(user_id=user_id, args=args)
            return

        if command == "/channel_remove":
            if not args:
                self.api.send_message(
                    user_id=user_id,
                    text="Формат: `/channel_remove CHANNEL_ID`",
                )
                return
            self.remove_channel_binding(user_id=user_id, args=args)
            return

        self.api.send_message(user_id=user_id, text="Неизвестная команда. Используйте `/help`.")

    def ensure_publish_ready(self, user_id: int) -> bool:
        if self.has_channel_bindings() and self.is_super_admin_user(user_id):
            return True
        self.api.send_message(
            user_id=user_id,
            text=(
                "Публикация пока не настроена.\n\n"
                f"{self.setup_status_text()}"
            ),
        )
        return False

    def setup_status_text(self) -> str:
        bindings = self.list_channel_bindings()
        missing: list[str] = []
        if not bindings:
            missing.append("хотя бы одну привязку канала через `/channel_add CHANNEL_ID COMMENTS_CHAT_ID`")
        if not SUPER_ADMIN_IDS:
            missing.append("`MAX_SUPER_ADMIN_IDS`")

        if missing:
            return (
                "Сейчас доступен диагностический режим.\n"
                "Нужно настроить: "
                f"{', '.join(missing)}\n"
                "Используйте `/me`, чтобы получить свой `user_id`, и `/chatinfo`, чтобы получить `chat_id`."
            )

        same_chat_bindings = [
            row for row in bindings if int(row["channel_chat_id"]) == int(row["comments_chat_id"])
        ]
        status = f"Режим публикации настроен. Подключено каналов: {len(bindings)}."
        if same_chat_bindings:
            status += (
                "\n\n"
                "Внимание: для некоторых привязок канал и чат обсуждения совпадают. "
                "Посты и техническая лента обсуждения будут смешиваться."
            )
        if not WEB_APP_PUBLIC_URL:
            status += (
                "\n\n"
                "Не забудьте настроить публичный HTTPS-URL мини-приложения в кабинете MAX. "
                "Локальный сервер из этого проекта нужен для разработки и бэкенда комментариев."
            )
        return status

    def send_chat_info(self, user_id: int, message: dict[str, Any]) -> None:
        recipient = message.get("recipient") or {}
        chat_id = recipient.get("chat_id")
        chat_type = safe_text(recipient.get("chat_type") or recipient.get("type"))
        title = safe_text(recipient.get("title") or recipient.get("name"))
        text = (
            "Данные текущего чата:\n"
            f"`chat_id`: `{chat_id or '-'}`\n"
            f"`chat_type`: `{chat_type or '-'}`\n"
            f"`title`: `{title or '-'}`"
        )
        self.api.send_message(user_id=user_id, text=text)

    def send_posts_overview(self, user_id: int, *, context: AdminContext | None = None) -> None:
        if context is None:
            context = self.admin_context_for_user(user_id)
        channel_ids = None
        if context is not None and not context.is_super_admin:
            channel_ids = context.channel_ids
        posts = self.store.list_posts(channel_ids=channel_ids)
        if not posts:
            self.api.send_message(user_id=user_id, text="Пока нет зарегистрированных постов.")
            return

        lines = ["Последние посты:"]
        for post in posts:
            ref = post_ref_for_message_id(post["post_message_id"])
            lines.append(
                f"- канал `{post['channel_chat_id']}` | пост `{post['post_message_id']}` | `{ref}` | {post['comment_count']} комм. | {snippet(post['post_text'], 60)}"
            )
        self.api.send_message(user_id=user_id, text="\n".join(lines))

    def send_channel_bindings_overview(self, user_id: int) -> None:
        bindings = self.list_channel_bindings()
        if not bindings:
            self.api.send_message(
                user_id=user_id,
                text=(
                    "Пока нет подключённых каналов.\n"
                    "Добавьте первый через `/channel_add CHANNEL_ID COMMENTS_CHAT_ID`."
                ),
            )
            return

        lines = ["Подключённые каналы:"]
        for binding in bindings:
            extra = ""
            comments_chat_url = safe_text(binding["comments_chat_url"])
            if comments_chat_url:
                extra = f" | {comments_chat_url}"
            lines.append(
                f"- канал `{binding['channel_chat_id']}` -> чат комментариев `{binding['comments_chat_id']}`{extra}"
            )
        self.api.send_message(user_id=user_id, text="\n".join(lines))

    def send_pending_channel_bindings_status(self, user_id: int) -> None:
        self.purge_expired_bind_codes()
        show_all = self.is_super_admin_user(user_id)
        pendings = self.store.list_pending_channel_bindings(
            requested_by_user_id=None if show_all else user_id
        )
        if not pendings:
            message = (
                "Сейчас нет активных незавершённых привязок."
                if show_all
                else "У вас нет активных незавершённых привязок."
            )
            self.api.send_message(user_id=user_id, text=message)
            return

        now = datetime.now(timezone.utc)
        lines = [
            "Активные незавершённые привязки:"
            if show_all
            else "Ваши активные незавершённые привязки:"
        ]
        for pending in pendings:
            try:
                created_at = datetime.fromisoformat(pending.created_at)
            except ValueError:
                created_at = now
            expires_at = created_at.timestamp() + BIND_CHANNEL_CODE_TTL_SECONDS
            remaining_seconds = max(int(expires_at - now.timestamp()), 0)
            channel_label = pending.channel_title or "-"
            line = (
                f"- код `{pending.bind_code}` | канал `{channel_label} ({pending.channel_chat_id})` | "
                f"создан `{format_utc_timestamp(pending.created_at)}` | осталось `{format_duration_compact(remaining_seconds)}`"
            )
            if show_all:
                line += f" | админ `{pending.requested_by_user_id}`"
            lines.append(line)
        self.api.send_message(user_id=user_id, text="\n".join(lines))

    def notify_super_admins_about_channel_request(self, request_row: sqlite3.Row) -> None:
        admin_ids = self.admin_notice_user_ids()
        if not admin_ids:
            return
        text = (
            "Новая заявка на подключение канала.\n\n"
            f"Канал: {safe_text(request_row['channel_title']) or request_row['channel_id']}\n"
            f"ID канала: `{request_row['channel_id']}`\n"
            f"Заявитель: `{request_row['requester_max_user_id']}`\n"
            f"Панель: {public_app_url('/super-admin')}"
        )
        for admin_id in admin_ids:
            try:
                self.api.send_message(user_id=admin_id, text=text, notify=True)
            except Exception:
                logger.exception("Failed to notify super admin %s about channel request", admin_id)

    def get_chat_info_cached(self, chat_id: int) -> dict[str, str]:
        normalized_chat_id = int(chat_id)
        cached = self.chat_info_cache.get(normalized_chat_id)
        now = time.time()
        if cached is not None and now - cached[1] < 10 * 60:
            return cached[0]
        try:
            payload = self.api.get_chat(normalized_chat_id)
        except MaxApiError:
            logger.exception("Failed to load chat info for %s", normalized_chat_id)
            return cached[0] if cached is not None else {"title": "", "link": ""}
        info = {
            "title": chat_title_from_payload(payload),
            "link": chat_link_from_payload(payload),
        }
        self.chat_info_cache[normalized_chat_id] = (info, now)
        return info

    def get_chat_title_cached(self, chat_id: int) -> str:
        return self.get_chat_info_cached(chat_id).get("title", "")

    def serialize_channel_binding(self, binding: sqlite3.Row) -> dict[str, Any]:
        channel_chat_id = int(binding["channel_chat_id"])
        comments_chat_id = int(binding["comments_chat_id"])
        channel_info = self.get_chat_info_cached(channel_chat_id)
        comments_chat_info = self.get_chat_info_cached(comments_chat_id) if comments_chat_id > 0 else {}
        channel_title = channel_info.get("title", "")
        comments_chat_title = comments_chat_info.get("title", "") if comments_chat_id > 0 else "WebApp без отдельного чата"
        comments_chat_url = safe_text(binding["comments_chat_url"]) or comments_chat_info.get("link", "")
        same_chat = comments_chat_id > 0 and int(binding["channel_chat_id"]) == comments_chat_id
        return {
            "channel_chat_id": channel_chat_id,
            "channel_title": channel_title,
            "channel_label": channel_title or str(channel_chat_id),
            "channel_url": channel_info.get("link", ""),
            "comments_chat_id": comments_chat_id,
            "comments_chat_title": comments_chat_title,
            "comments_chat_label": comments_chat_title or str(comments_chat_id),
            "comments_chat_url": comments_chat_url,
            "created_at": safe_text(binding["created_at"]),
            "updated_at": safe_text(binding["updated_at"]),
            "same_chat": same_chat,
            "webapp_only": comments_chat_id <= 0,
        }

    def serialize_post_summary(self, post: sqlite3.Row) -> dict[str, Any]:
        comment_search_text = " ".join(self.store.list_comment_texts(post["post_message_id"]))
        return {
            "post_message_id": safe_text(post["post_message_id"]),
            "post_ref": post_ref_for_message_id(post["post_message_id"]),
            "channel_chat_id": int(post["channel_chat_id"]),
            "comments_chat_id": int(post["comments_chat_id"]) if post["comments_chat_id"] is not None else None,
            "post_url": safe_text(post["post_url"]),
            "post_title": safe_text(post["post_title"] if "post_title" in post.keys() else ""),
            "post_text": safe_text(post["post_text"]),
            "comment_search_text": comment_search_text,
            "comment_count": int(post["comment_count"]),
            "status": safe_text(post["status"] if "status" in post.keys() else "published"),
            "created_at": safe_text(post["created_at"]),
            "updated_at": safe_text(post["updated_at"]),
        }

    def serialize_pending_channel_binding_status(
        self,
        pending: PendingChannelBinding,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current_time = now or datetime.now(timezone.utc)
        try:
            created_at = datetime.fromisoformat(pending.created_at)
        except ValueError:
            created_at = current_time
        expires_at = created_at.timestamp() + BIND_CHANNEL_CODE_TTL_SECONDS
        remaining_seconds = max(int(expires_at - current_time.timestamp()), 0)
        return {
            "status": "pending_comments_chat",
            "bind_code": pending.bind_code,
            "requested_by_user_id": pending.requested_by_user_id,
            "channel_chat_id": pending.channel_chat_id,
            "channel_title": pending.channel_title or "",
            "channel_label": pending.channel_title or str(pending.channel_chat_id),
            "created_at": pending.created_at,
            "created_at_display": format_utc_timestamp(pending.created_at),
            "remaining_seconds": remaining_seconds,
            "remaining_display": format_duration_compact(remaining_seconds),
            "setup_command": f"/bind_comments {pending.bind_code}",
        }

    def get_admin_state(self) -> dict[str, Any]:
        self.purge_expired_bind_codes()
        now = datetime.now(timezone.utc)
        return {
            "app": {
                "version": APP_VERSION,
                "delivery_mode": self.delivery_mode,
                "web_app_public_url": WEB_APP_PUBLIC_URL,
                "webhook_public_url": self.webhook_url(),
                "webhook_path": WEBHOOK_PATH,
                "bot_username": self.get_bot_username(),
                "channel_sync_interval_seconds": CHANNEL_SYNC_INTERVAL_SECONDS,
                "admin_count": len(self.store.list_admin_users()),
            },
            "channels": [
                self.serialize_channel_binding(binding)
                for binding in self.list_channel_bindings()
            ],
            "posts": [
                self.serialize_post_summary(post)
                for post in self.store.list_posts(limit=30)
            ],
            "pending_bindings": [
                self.serialize_pending_channel_binding_status(pending, now=now)
                for pending in self.store.list_pending_channel_bindings()
            ],
        }

    def admin_add_channel_binding(
        self,
        *,
        channel_chat_id: int,
        comments_chat_id: int,
        comments_chat_url: str | None = None,
        sync_now: bool = True,
    ) -> dict[str, Any]:
        self.store.upsert_channel_binding(
            channel_chat_id=channel_chat_id,
            comments_chat_id=comments_chat_id,
            comments_chat_url=comments_chat_url or None,
        )
        self.start_channel_sync()
        attached_count = 0
        if sync_now:
            attached_count = self.sync_recent_channel_posts_for_binding(
                channel_chat_id=channel_chat_id,
                comments_chat_id=comments_chat_id,
            )
        binding = self.get_channel_binding(channel_chat_id)
        if binding is None:
            raise MaxApiError("Channel is not connected")
        return {
            "ok": True,
            "binding": self.serialize_channel_binding(binding),
            "attached_count": attached_count,
        }

    def admin_remove_channel_binding(self, *, channel_chat_id: int) -> dict[str, Any]:
        deleted = self.store.delete_channel_binding(channel_chat_id)
        if not deleted:
            raise MaxApiError("Channel is not connected")
        return {"ok": True, "channel_chat_id": channel_chat_id}

    def admin_sync_recent_channel_posts(self) -> dict[str, Any]:
        attached_count = self.sync_recent_channel_posts()
        return {"ok": True, "attached_count": attached_count}

    def build_link_keyboard(self, buttons: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "type": "inline_keyboard",
                "payload": {
                    "buttons": [
                        buttons
                    ]
                },
            }
        ]

    def send_setup_channel_response(
        self,
        *,
        user_id: int,
        response_chat_id: int | None,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        if response_chat_id is not None:
            self.api.send_message(
                chat_id=response_chat_id,
                text=text,
                attachments=attachments,
            )
            return
        self.api.send_message(
            user_id=user_id,
            text=text,
            attachments=attachments,
        )

    def begin_channel_binding(
        self,
        *,
        user_id: int,
        message: dict[str, Any],
        args: str,
        response_chat_id: int | None = None,
    ) -> None:
        channel_chat_id, title, chat_type, chat_link = self.current_chat_context(message)
        if channel_chat_id is None:
            self.api.send_message(
                user_id=user_id,
                text="Эту команду нужно отправить прямо в канале, который хотите подключить.",
            )
            return

        same_chat_mode = safe_text(args).lower() in {"same", "self", "here", "same_chat"}
        if same_chat_mode:
            self.store.upsert_channel_binding(
                channel_chat_id=channel_chat_id,
                comments_chat_id=channel_chat_id,
                comments_chat_url=chat_link,
            )
            self.store.add_channel_admin(user_id=user_id, channel_id=channel_chat_id)
            self.start_channel_sync()
            attached_count = self.sync_recent_channel_posts_for_binding(
                channel_chat_id=channel_chat_id,
                comments_chat_id=channel_chat_id,
            )
            self.send_setup_channel_response(
                user_id=user_id,
                response_chat_id=response_chat_id,
                text=(
                    "Канал создан в админке и подключён в режиме одного чата.\n"
                    f"Канал: `{title or '-'} ({channel_chat_id})`\n"
                    f"Чат комментариев: `{title or '-'} ({channel_chat_id})`\n"
                    f"Подцеплено постов при первичной синхронизации: `{attached_count}`"
                ),
            )
            return

        bind_code = self.issue_bind_code()
        self.store.set_pending_channel_binding(
            bind_code=bind_code,
            requested_by_user_id=user_id,
            channel_chat_id=channel_chat_id,
            channel_title=title or None,
        )
        lines = [
            "Канал создан в админке со статусом `Ожидает чат комментариев`.",
            f"Канал: `{title or '-'} ({channel_chat_id})`",
            f"Код привязки: `{bind_code}`",
            "",
            "Теперь создайте или откройте нужный чат комментариев и отправьте туда:",
            f"`/bind_comments {bind_code}`",
            "",
            f"Код действует {BIND_CHANNEL_CODE_TTL_SECONDS // 60} минут.",
        ]
        share_url = max_share_url(f"/bind_comments {bind_code}")
        self.send_setup_channel_response(
            user_id=user_id,
            response_chat_id=response_chat_id,
            text="\n".join(lines),
        )
        self.send_setup_channel_response(
            user_id=user_id,
            response_chat_id=response_chat_id,
            text=(
                "Готовые кнопки для завершения привязки.\n"
                "Можно выбрать отдельный чат комментариев или сразу подключить комментарии в этом же чате."
            ),
            attachments=self.build_link_keyboard(
                [
                    {
                        "type": "link",
                        "text": "Выбрать чат комментариев",
                        "url": share_url,
                    },
                    {
                        "type": "message",
                        "text": "Комментарии в этом чате",
                        "payload": "/setup_channel same",
                    },
                ]
            ),
        )

    def complete_channel_binding(
        self,
        *,
        user_id: int,
        message: dict[str, Any],
        args: str,
        response_chat_id: int | None = None,
    ) -> None:
        bind_code = safe_text(args).upper()
        if not bind_code:
            self.send_setup_channel_response(
                user_id=user_id,
                response_chat_id=response_chat_id,
                text="Формат: `/bind_comments CODE`",
            )
            return

        pending = self.get_valid_pending_channel_binding(bind_code)
        if pending is None:
            self.send_setup_channel_response(
                user_id=user_id,
                response_chat_id=response_chat_id,
                text="Код привязки не найден или уже истёк. Запустите `/bind_channel` заново в канале.",
            )
            return
        if int(pending.requested_by_user_id) != int(user_id):
            self.send_setup_channel_response(
                user_id=user_id,
                response_chat_id=response_chat_id,
                text="Этот код привязки создан другим пользователем. Завершить привязку должен тот же администратор.",
            )
            return

        comments_chat_id, comments_title, _chat_type, comments_chat_link = self.current_chat_context(message)
        if comments_chat_id is None:
            self.send_setup_channel_response(
                user_id=user_id,
                response_chat_id=response_chat_id,
                text="Эту команду нужно отправить прямо в чате комментариев.",
            )
            return

        self.store.upsert_channel_binding(
            channel_chat_id=pending.channel_chat_id,
            comments_chat_id=comments_chat_id,
            comments_chat_url=comments_chat_link,
        )
        self.store.add_channel_admin(user_id=user_id, channel_id=pending.channel_chat_id)
        self.store.delete_pending_channel_binding(bind_code)
        self.start_channel_sync()

        attached_count = self.sync_recent_channel_posts_for_binding(
            channel_chat_id=pending.channel_chat_id,
            comments_chat_id=comments_chat_id,
        )
        warning = ""
        if int(pending.channel_chat_id) == int(comments_chat_id):
            warning = (
                "\n\nВнимание: канал и чат комментариев совпадают. "
                "Посты и техническая лента обсуждения будут смешиваться."
            )
        self.send_setup_channel_response(
            user_id=user_id,
            response_chat_id=response_chat_id,
            text=(
                "Привязка завершена.\n"
                f"Канал: `{pending.channel_title or '-'} ({pending.channel_chat_id})`\n"
                f"Чат комментариев: `{comments_title or '-'} ({comments_chat_id})`\n"
                f"Подцеплено постов при первичной синхронизации: `{attached_count}`"
                f"{warning}"
            ),
        )

    def add_channel_binding(self, *, user_id: int, args: str) -> None:
        parts = args.split(maxsplit=2)
        if len(parts) < 2:
            self.api.send_message(
                user_id=user_id,
                text="Формат: `/channel_add CHANNEL_ID COMMENTS_CHAT_ID`",
            )
            return

        try:
            channel_chat_id = int(parts[0])
            comments_chat_id = int(parts[1])
        except ValueError:
            self.api.send_message(
                user_id=user_id,
                text="CHANNEL_ID и COMMENTS_CHAT_ID должны быть числами.",
            )
            return

        comments_chat_url = parts[2].strip() if len(parts) > 2 else None
        self.store.upsert_channel_binding(
            channel_chat_id=channel_chat_id,
            comments_chat_id=comments_chat_id,
            comments_chat_url=comments_chat_url or None,
        )
        self.store.add_channel_admin(user_id=user_id, channel_id=channel_chat_id)
        self.start_channel_sync()

        warning = ""
        if channel_chat_id == comments_chat_id:
            warning = (
                "\n\nВнимание: канал и чат комментариев совпадают. "
                "Посты и техническая лента обсуждения будут смешиваться."
            )
        self.api.send_message(
            user_id=user_id,
            text=(
                "Канал подключён.\n"
                f"Канал: `{channel_chat_id}`\n"
                f"Чат комментариев: `{comments_chat_id}`{warning}"
            ),
        )

    def remove_channel_binding(self, *, user_id: int, args: str) -> None:
        try:
            channel_chat_id = int(args.strip())
        except ValueError:
            self.api.send_message(
                user_id=user_id,
                text="CHANNEL_ID должен быть числом.",
            )
            return

        deleted = self.store.delete_channel_binding(channel_chat_id)
        if not deleted:
            self.api.send_message(
                user_id=user_id,
                text=f"Канал `{channel_chat_id}` не найден в списке подключённых.",
            )
            return
        self.api.send_message(
            user_id=user_id,
            text=(
                f"Канал `{channel_chat_id}` отключён.\n"
                "Старые посты и их комментарии останутся доступны, но новые посты бот больше не будет подцеплять автоматически."
            ),
        )

    def resolve_publish_command_target(
        self,
        message: dict[str, Any],
        args: str,
    ) -> tuple[sqlite3.Row | None, str, str | None]:
        clean_args = args.strip()
        bindings = self.list_channel_bindings()
        if not bindings:
            return None, "", "Сначала подключите хотя бы один канал через `/channel_add CHANNEL_ID COMMENTS_CHAT_ID`."

        recipient = message.get("recipient") or {}
        current_binding = self.get_channel_binding(recipient.get("chat_id"))
        if current_binding is not None:
            if not clean_args:
                return None, "", "Формат: `/publish текст поста`"
            return current_binding, clean_args, None

        if len(bindings) == 1:
            if not clean_args:
                return None, "", "Формат: `/publish текст поста`"
            return bindings[0], clean_args, None

        if not clean_args:
            return (
                None,
                "",
                "Подключено несколько каналов. Используйте формат `/publish CHANNEL_ID текст поста` или отправьте команду прямо в нужном канале.",
            )

        raw_channel_id, _, publish_text = clean_args.partition(" ")
        try:
            channel_chat_id = int(raw_channel_id)
        except ValueError:
            return (
                None,
                "",
                "Подключено несколько каналов. Используйте формат `/publish CHANNEL_ID текст поста` или отправьте команду прямо в нужном канале.",
            )

        binding = self.get_channel_binding(channel_chat_id)
        if binding is None:
            return None, "", f"Канал `{channel_chat_id}` не подключён. Посмотреть список можно через `/channels`."
        if not publish_text.strip():
            return None, "", "После CHANNEL_ID укажите текст поста."
        return binding, publish_text.strip(), None

    def resolve_post_reference(self, reference: str) -> sqlite3.Row | None:
        direct = self.store.get_post(reference)
        if direct is not None:
            return direct

        if reference.startswith("post_"):
            decoded = message_id_from_post_token(reference.removeprefix("post_"))
            if decoded:
                return self.store.get_post(decoded)

        decoded = message_id_from_post_token(reference)
        if decoded:
            post = self.store.get_post(decoded)
            if post is not None:
                return post

        return self.store.find_post_by_comment_code(reference)

    def is_own_message(self, sender: dict[str, Any]) -> bool:
        if self.bot_info is None:
            return False
        sender_user_id = sender.get("user_id")
        bot_user_id = self.bot_info.get("user_id")
        if not sender_user_id or not bot_user_id:
            return False
        return int(sender_user_id) == int(bot_user_id)

    def maybe_auto_attach_channel_post(self, message: dict[str, Any]) -> bool:
        if not self.has_channel_bindings():
            return False

        recipient = message.get("recipient") or {}
        chat_id = recipient.get("chat_id")
        binding = self.get_channel_binding(chat_id)
        if binding is None:
            return False

        sender = message.get("sender") or {}
        if sender and self.is_own_message(sender):
            return False
        sender_user_id = validate_positive_int(sender.get("user_id"), minimum=1, maximum=10**18)

        body = message.get("body") or {}
        post_message_id = safe_text(body.get("mid") or message.get("mid"))
        if not post_message_id:
            logger.warning("Channel post from admin has no message id: %s", message)
            return True

        try:
            self.register_channel_post_for_comments(
                post_message_id=post_message_id,
                channel_chat_id=int(binding["channel_chat_id"]),
                comments_chat_id=int(binding["comments_chat_id"]),
                post_url=message.get("url"),
                post_text=safe_text(body.get("text")),
                source_attachments=self.extract_post_attachments_from_message(message),
            )
            logger.info("Auto-attached comments button to channel post %s", post_message_id)
        except Exception:
            logger.exception("Failed to auto-attach comments to channel post %s", post_message_id)
            if sender_user_id is not None:
                self.api.send_message(
                    user_id=int(sender_user_id),
                    text=(
                        "Не удалось автоматически подключить комментарии к этому посту.\n"
                        f"Попробуйте вручную: `/attach {post_message_id}`"
                    ),
                )
        return True

    def sync_recent_channel_posts(self, *, count: int = 50) -> int:
        attached_count = 0
        for binding in self.list_channel_bindings():
            channel_chat_id = int(binding["channel_chat_id"])
            try:
                attached_count += self.sync_recent_channel_posts_for_binding(
                    channel_chat_id=channel_chat_id,
                    comments_chat_id=int(binding["comments_chat_id"]),
                    count=count,
                )
            except Exception:
                logger.exception("Failed to sync channel %s", channel_chat_id)
        return attached_count

    def sync_recent_channel_posts_for_binding(
        self,
        *,
        channel_chat_id: int,
        comments_chat_id: int,
        count: int = 50,
    ) -> int:
        attached_count = 0
        messages = self.api.get_chat_messages(channel_chat_id, count=count)
        for message in reversed(messages):
            if not self.should_auto_attach_channel_message(message):
                continue
            body = message.get("body") or {}
            post_message_id = safe_text(body.get("mid") or message.get("mid"))
            if not post_message_id:
                continue
            self.register_channel_post_for_comments(
                post_message_id=post_message_id,
                channel_chat_id=channel_chat_id,
                comments_chat_id=comments_chat_id,
                post_url=message.get("url"),
                post_text=safe_text(body.get("text")),
                source_attachments=self.extract_post_attachments_from_message(message),
            )
            attached_count += 1
        return attached_count

    def should_auto_attach_channel_message(self, message: dict[str, Any]) -> bool:
        recipient = message.get("recipient") or {}
        chat_id = recipient.get("chat_id")
        if self.get_channel_binding(chat_id) is None:
            return False

        body = message.get("body") or {}
        post_message_id = safe_text(body.get("mid") or message.get("mid"))
        if not post_message_id:
            return False
        if self.store.get_post(post_message_id) is not None:
            return False

        post_text = safe_text(body.get("text"))
        if post_text.startswith("/"):
            return False
        if CHANNEL_POST_FOOTER in post_text:
            return False
        if is_setup_service_message_text(post_text):
            return False

        raw_attachments = body.get("attachments") or []
        if not post_text and not raw_attachments:
            return False
        if self.has_any_inline_keyboard(raw_attachments):
            return False
        return True

    def has_any_inline_keyboard(self, attachments: Any) -> bool:
        if not isinstance(attachments, list):
            return False
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            if safe_text(attachment.get("type")) == "inline_keyboard":
                return True
        return False

    def extract_post_attachments_from_message(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        raw_attachments = ((message.get("body") or {}).get("attachments")) or []
        if not isinstance(raw_attachments, list):
            return []
        preserved: list[dict[str, Any]] = []
        for attachment in raw_attachments:
            if not isinstance(attachment, dict):
                continue
            if safe_text(attachment.get("type")) == "inline_keyboard":
                continue
            preserved.append(attachment)
        return preserved

    def stored_post_attachments(self, post: sqlite3.Row) -> list[dict[str, Any]]:
        try:
            raw_value = post["post_attachments_json"]
        except (KeyError, IndexError):
            raw_value = None
        return deserialize_message_attachments(raw_value)

    def render_channel_post_attachments(
        self,
        post_message_id: str,
        *,
        post_attachments: list[dict[str, Any]] | None = None,
        comment_count: int | None = None,
    ) -> list[dict[str, Any]]:
        attachments = list(post_attachments or [])
        attachments.extend(
            self.build_comment_button(
                post_message_id,
                comment_count=comment_count,
            )
        )
        return attachments

    def register_channel_post_for_comments(
        self,
        *,
        post_message_id: str,
        channel_chat_id: int,
        comments_chat_id: int,
        post_url: str | None,
        post_text: str,
        post_title: str | None = None,
        source_attachments: list[dict[str, Any]] | None = None,
    ) -> sqlite3.Row:
        clean_post_text = strip_managed_channel_footer(post_text)
        clean_attachments = list(source_attachments or [])
        existing_post = self.store.get_post(post_message_id)
        stored_comments_chat_id = (
            int(existing_post["comments_chat_id"])
            if existing_post is not None and existing_post["comments_chat_id"] is not None
            else int(comments_chat_id)
        )

        self.store.upsert_post(
            post_message_id=post_message_id,
            channel_chat_id=channel_chat_id,
            comments_chat_id=stored_comments_chat_id,
            post_url=post_url,
            post_text=clean_post_text,
            post_attachments=clean_attachments,
            post_title=post_title,
            discussion_message_id=(
                safe_text(existing_post["discussion_message_id"]) or None if existing_post is not None else None
            ),
        )

        discussion_message_id = (
            safe_text(existing_post["discussion_message_id"]) or None if existing_post is not None else None
        )
        if stored_comments_chat_id > 0 and not discussion_message_id:
            discussion_text = self.build_discussion_post_text(post_message_id, clean_post_text, post_url)
            discussion_message = self.api.send_message(
                chat_id=stored_comments_chat_id,
                text=discussion_text,
            )
            discussion_message_id = extract_message_id(discussion_message)
            if discussion_message_id:
                self.store.set_discussion_message_id(post_message_id, discussion_message_id)

        self.api.edit_message(
            post_message_id,
            text=self.render_channel_post_text(clean_post_text, post_message_id),
            attachments=self.render_channel_post_attachments(
                post_message_id,
                post_attachments=clean_attachments,
            ),
        )

        stored_post = self.store.get_post(post_message_id)
        if stored_post is None:
            raise MaxApiError("Post was registered but could not be reloaded")
        return stored_post

    def publish_post(
        self,
        text: str,
        *,
        admin_user_id: int | None,
        channel_chat_id: int,
        post_title: str | None = None,
    ) -> dict[str, Any]:
        binding = self.get_channel_binding(channel_chat_id)
        if binding is None:
            raise MaxApiError("Channel is not connected")
        channel_message = self.api.send_message(
            chat_id=channel_chat_id,
            text=text,
        ).get("message", {})

        post_message_id = safe_text(channel_message.get("body", {}).get("mid") or channel_message.get("mid"))
        if not post_message_id:
            raise MaxApiError("MAX API did not return message id for the channel post")

        post_url = channel_message.get("url")
        self.register_channel_post_for_comments(
            post_message_id=post_message_id,
            channel_chat_id=int(binding["channel_chat_id"]),
            comments_chat_id=int(binding["comments_chat_id"]),
            post_url=post_url,
            post_text=safe_text(channel_message.get("body", {}).get("text")) or text,
            post_title=post_title,
            source_attachments=self.extract_post_attachments_from_message(channel_message),
        )

        extra_link = self.direct_webapp_url(post_message_id)
        lines = [
            "Пост опубликован.",
            f"ID поста: `{post_message_id}`",
            f"Ссылка: {post_url or 'MAX не вернул публичную ссылку'}",
        ]
        if extra_link:
            lines.append(f"WebApp: {extra_link}")
        if admin_user_id is not None:
            self.api.send_message(user_id=admin_user_id, text="\n".join(lines))
        return {
            "ok": True,
            "post_message_id": post_message_id,
            "post_url": safe_text(post_url),
            "webapp_url": safe_text(extra_link),
            "channel_chat_id": int(binding["channel_chat_id"]),
            "comments_chat_id": int(binding["comments_chat_id"]),
        }

    def attach_existing_post(self, post_message_id: str, *, admin_user_id: int | None) -> dict[str, Any]:
        message = self.api.get_message(post_message_id)
        post_text = safe_text(((message.get("body") or {}).get("text")))
        recipient = message.get("recipient") or {}
        chat_id = recipient.get("chat_id")
        binding = self.get_channel_binding(chat_id)
        if binding is None:
            raise MaxApiError("Channel is not connected")
        post_url = message.get("url")

        self.register_channel_post_for_comments(
            post_message_id=post_message_id,
            channel_chat_id=int(binding["channel_chat_id"]),
            comments_chat_id=int(binding["comments_chat_id"]),
            post_url=post_url,
            post_text=post_text,
            source_attachments=self.extract_post_attachments_from_message(message),
        )

        if admin_user_id is not None:
            self.api.send_message(
                user_id=admin_user_id,
                text=(
                    "Комментарии подключены к существующему посту.\n"
                    f"ID поста: `{post_message_id}`"
                ),
            )
        return {
            "ok": True,
            "post_message_id": post_message_id,
            "channel_chat_id": int(binding["channel_chat_id"]),
            "comments_chat_id": int(binding["comments_chat_id"]),
        }

    def build_comment_button(
        self,
        post_message_id: str,
        *,
        comment_count: int | None = None,
    ) -> list[dict[str, Any]]:
        if comment_count is None:
            comment_count = self.store.get_comment_count(post_message_id)
        buttons = [[self.build_webapp_link_button(post_message_id, comment_count=comment_count)]]
        return [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]

    def build_webapp_link_button(
        self,
        post_message_id: str,
        *,
        comment_count: int | None = None,
    ) -> dict[str, Any]:
        bot_username = self.get_bot_username()
        payload = post_ref_for_message_id(post_message_id)
        if not bot_username:
            raise MaxApiError("Bot username is required for startapp deep links")
        if comment_count is None:
            comment_count = self.store.get_comment_count(post_message_id)
        return {
            "type": "link",
            "text": comment_button_text(comment_count),
            "url": f"https://max.ru/{bot_username}?startapp={payload}",
        }

    def refresh_post_comment_button(
        self,
        post_message_id: str,
        *,
        comment_count: int | None = None,
    ) -> None:
        post = self.store.get_post(post_message_id)
        if post is None:
            return
        if comment_count is None:
            comment_count = int(post["comment_count"])
        try:
            self.api.edit_message(
                post_message_id,
                text=self.render_channel_post_text(post["post_text"], post_message_id),
                attachments=self.render_channel_post_attachments(
                    post_message_id,
                    post_attachments=self.stored_post_attachments(post),
                    comment_count=comment_count,
                ),
            )
        except Exception:
            logger.exception(
                "Failed to refresh comment button for post %s",
                post_message_id,
            )

    def build_discussion_post_text(
        self,
        post_message_id: str,
        post_text: str,
        post_url: str | None,
    ) -> str:
        lines = [
            f"Обсуждение поста `{post_message_id}`",
            f"Пост: {snippet(post_text, 300)}",
            "",
            "Новые комментарии из мини-приложения будут добавляться ответами к этому сообщению.",
        ]
        if post_url:
            lines.insert(2, f"Ссылка на пост: {post_url}")
        direct_url = self.direct_webapp_url(post_message_id)
        if direct_url:
            lines.append(f"WebApp: {direct_url}")
        return "\n".join(lines)

    def render_channel_post_text(self, post_text: str, post_message_id: str) -> str:
        footer_lines = [
            "",
            "",
            CHANNEL_POST_FOOTER,
        ]
        return f"{post_text}{chr(10).join(footer_lines)}"

    def direct_webapp_url(self, post_message_id: str) -> str | None:
        if not WEB_APP_PUBLIC_URL:
            return None
        ref = post_ref_for_message_id(post_message_id)
        return f"{WEB_APP_PUBLIC_URL}/?post={parse.quote(ref)}"

    def build_comment_media_item(
        self,
        *,
        file_name: str,
        mime_type: str,
        binary: bytes,
        width: int | None = None,
        height: int | None = None,
    ) -> dict[str, Any]:
        ext, normalized_mime = detect_image_format(binary)
        if mime_type and mime_type != normalized_mime:
            mime_type = normalized_mime
        relative_path = (
            f"{datetime.now(timezone.utc):%Y/%m}/"
            f"{uuid.uuid4().hex}.{ext}"
        )
        absolute_path = COMMENT_MEDIA_DIR / relative_path
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_path.write_bytes(binary)
        return {
            "kind": "image",
            "storage_path": relative_path,
            "url": comment_media_public_url(relative_path),
            "path": comment_media_public_path(relative_path),
            "mime_type": normalized_mime,
            "file_name": safe_file_name(file_name, fallback=f"comment-image.{ext}"),
            "size_bytes": len(binary),
            "width": width,
            "height": height,
        }

    def build_comment_media_from_webapp(self, photo_payload: Any) -> list[dict[str, Any]]:
        if not isinstance(photo_payload, dict):
            return []

        raw_data_url = safe_text(photo_payload.get("data_url"))
        if not raw_data_url:
            return []
        if len(raw_data_url) > COMMENT_IMAGE_DATA_URL_MAX_LENGTH:
            raise MaxApiError("Image is too large")

        header, marker, encoded = raw_data_url.partition(",")
        if marker != "," or ";base64" not in header:
            raise MaxApiError("Invalid image data")

        declared_mime = safe_text(header.removeprefix("data:").split(";", 1)[0]).lower()
        if declared_mime not in {"image/jpeg", "image/png"}:
            raise MaxApiError("Unsupported image format. Use JPG or PNG.")

        try:
            binary = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise MaxApiError("Image data could not be decoded") from exc
        if not binary:
            raise MaxApiError("Image is empty")
        if len(binary) > COMMENT_IMAGE_MAX_BYTES:
            raise MaxApiError("Image is too large")

        width = validate_positive_int(photo_payload.get("width"))
        height = validate_positive_int(photo_payload.get("height"))
        return [
            self.build_comment_media_item(
                file_name=safe_text(photo_payload.get("file_name")) or "comment-image.jpg",
                mime_type=declared_mime,
                binary=binary,
                width=width,
                height=height,
            )
        ]

    def comment_media_absolute_path(self, media_item: dict[str, Any]) -> Path | None:
        storage_path = safe_text(media_item.get("storage_path"))
        if not storage_path:
            return None
        candidate = (COMMENT_MEDIA_DIR / storage_path).resolve()
        try:
            candidate.relative_to(COMMENT_MEDIA_DIR.resolve())
        except ValueError:
            return None
        return candidate

    def delete_comment_media_files(self, media_items: list[dict[str, Any]]) -> None:
        for media_item in media_items:
            file_path = self.comment_media_absolute_path(media_item)
            if file_path is None or not file_path.exists():
                continue
            try:
                file_path.unlink()
            except OSError:
                logger.exception("Failed to delete comment media file %s", file_path)

    def build_max_image_attachments(
        self,
        media_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        for media_item in media_items:
            if safe_text(media_item.get("kind")) != "image":
                continue
            file_path = self.comment_media_absolute_path(media_item)
            if file_path is None or not file_path.exists():
                continue
            try:
                attachments.append(
                    self.api.upload_image(
                        file_name=safe_text(media_item.get("file_name")) or file_path.name,
                        mime_type=safe_text(media_item.get("mime_type")) or "image/jpeg",
                        binary=file_path.read_bytes(),
                    )
                )
            except Exception:
                logger.exception("Failed to upload comment image to MAX for %s", file_path)
        return attachments

    def build_discussion_comment_text(
        self,
        *,
        post_message_id: str,
        comment_count: int,
        display_name: str,
        username: str | None,
        text: str,
        media_items: list[dict[str, Any]],
        parent_comment: sqlite3.Row | None = None,
    ) -> str:
        lines = [
            f"Комментарий #{comment_count} к посту `{post_message_id}`",
            f"Автор: {display_name}" + (f" (`@{username}`)" if username else ""),
        ]
        if parent_comment is not None:
            lines.extend(
                [
                    "",
                    f"В ответ на комментарий {parent_comment['display_name']}:",
                    f"> {comment_reply_preview_text(parent_comment)}",
                ]
            )
        if text:
            lines.extend(["", text])
        elif media_items:
            lines.extend(["", "Фото"])
        return "\n".join(lines)

    def build_admin_comment_notice(
        self,
        *,
        post_message_id: str,
        display_name: str,
        text: str,
        media_items: list[dict[str, Any]],
        parent_comment: sqlite3.Row | None = None,
    ) -> str:
        lines = [
            f"Новый комментарий к посту `{post_message_id}`",
            f"От: {display_name}",
        ]
        if parent_comment is not None:
            lines.extend(
                [
                    "",
                    f"Ответ на: {parent_comment['display_name']}",
                    comment_reply_preview_text(parent_comment),
                ]
            )
        if text:
            lines.extend(["", text])
        if media_items:
            first_image = next(
                (item for item in media_items if safe_text(item.get("kind")) == "image"),
                None,
            )
            if first_image:
                lines.extend(["", f"Фото: {first_image.get('url')}"])
        return "\n".join(lines)

    def send_comment_edit_notice(
        self,
        *,
        post_message_id: str,
        comments_chat_id: int,
        display_name: str,
        username: str | None,
        text: str,
        media_items: list[dict[str, Any]],
        discussion_message_id: str | None,
    ) -> None:
        lines = [
            f"Комментарий к посту `{post_message_id}` был изменён",
            f"Автор: {display_name}" + (f" (`@{username}`)" if username else ""),
        ]
        if text:
            lines.extend(["", "Новый текст:", text])
        elif media_items:
            lines.extend(["", "Текст очищен, фото осталось в комментарии."])
        else:
            lines.extend(["", "Комментарий обновлён."])

        link_payload = None
        if discussion_message_id:
            link_payload = {"type": "reply", "mid": discussion_message_id}

        self.api.send_message(
            chat_id=comments_chat_id,
            text="\n".join(lines),
            link=link_payload,
            notify=False,
        )

        admin_notice = "\n".join(
            [
                f"Комментарий к посту `{post_message_id}` обновлён",
                f"От: {display_name}",
                "",
                text or "Текст очищен",
            ]
        )
        for admin_id in self.admin_notice_user_ids():
            self.api.send_message(
                user_id=admin_id,
                text=admin_notice,
                notify=False,
            )

    def build_reply_notification_text(
        self,
        *,
        post: sqlite3.Row,
        replier_display_name: str,
        replier_username: str | None,
        reply_text: str,
        reply_media_items: list[dict[str, Any]],
        parent_comment: sqlite3.Row,
    ) -> str:
        post_title = snippet(post["post_text"], 120)
        lines = [
            "На ваш комментарий пришёл ответ.",
            f"Пост: {post_title}",
            f"Ответил: {replier_display_name}" + (f" (`@{replier_username}`)" if replier_username else ""),
            "",
            "Ваш комментарий:",
            f"> {comment_reply_preview_text(parent_comment)}",
            "",
            "Ответ:",
        ]
        if reply_text:
            lines.append(reply_text)
        elif reply_media_items:
            lines.append("Фото")
        else:
            lines.append("Комментарий")

        post_url = safe_text(post["post_url"])
        if post_url:
            lines.extend(["", f"Пост: {post_url}"])
        direct_url = self.direct_webapp_url(post["post_message_id"])
        if direct_url:
            lines.append(f"Комментарии: {direct_url}")
        return "\n".join(lines)

    def notify_reply_target(
        self,
        *,
        post: sqlite3.Row,
        parent_comment: sqlite3.Row | None,
        replier_user_id: int,
        replier_display_name: str,
        replier_username: str | None,
        reply_text: str,
        reply_media_items: list[dict[str, Any]],
    ) -> None:
        if parent_comment is None:
            return
        parent_user_id = int(parent_comment["user_id"])
        if parent_user_id == int(replier_user_id):
            return
        try:
            self.api.send_message(
                user_id=parent_user_id,
                text=self.build_reply_notification_text(
                    post=post,
                    replier_display_name=replier_display_name,
                    replier_username=replier_username,
                    reply_text=reply_text,
                    reply_media_items=reply_media_items,
                    parent_comment=parent_comment,
                ),
            )
        except Exception:
            logger.exception(
                "Failed to notify user %s about reply to comment %s",
                parent_user_id,
                parent_comment["id"],
            )

    def send_message_with_attachment_retry(
        self,
        *,
        chat_id: int | None = None,
        user_id: int | None = None,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        link: dict[str, Any] | None = None,
        notify: bool = True,
    ) -> dict[str, Any]:
        attempts = [0.0, 1.0, 2.0, 4.0] if attachments else [0.0]
        last_error: MaxApiError | None = None
        for delay_seconds in attempts:
            if delay_seconds > 0:
                time.sleep(delay_seconds)
            try:
                return self.api.send_message(
                    chat_id=chat_id,
                    user_id=user_id,
                    text=text,
                    attachments=attachments,
                    link=link,
                    notify=notify,
                )
            except MaxApiError as exc:
                last_error = exc
                if "attachment.not.ready" not in str(exc):
                    raise
        if last_error is not None:
            raise last_error
        raise MaxApiError("Message could not be sent")

    def submit_comment(
        self,
        *,
        post_message_id: str,
        parent_comment_id: int | None = None,
        user_id: int,
        display_name: str,
        username: str | None,
        text: str,
        media: list[dict[str, Any]] | None = None,
        source_kind: str,
        source_message_id: str | None = None,
    ) -> dict[str, Any]:
        post = self.store.get_post(post_message_id)
        if post is None:
            raise MaxApiError("Post not found")
        media_items = [item for item in (media or []) if isinstance(item, dict)]
        clean_text = safe_text(text)
        if not clean_text and not media_items:
            raise MaxApiError("Comment is empty")
        ensure_comment_text_has_no_links(clean_text)
        ensure_comment_text_has_no_taboo(clean_text)
        parent_comment = None
        if parent_comment_id is not None:
            parent_comment = self.store.get_comment(int(parent_comment_id))
            if parent_comment is None or safe_text(parent_comment["post_message_id"]) != safe_text(post_message_id):
                raise MaxApiError("Parent comment not found")
            if safe_text(parent_comment["status"] if "status" in parent_comment.keys() else COMMENT_STATUS_ACTIVE) != COMMENT_STATUS_ACTIVE:
                raise MaxApiError("Parent comment not found")

        comment_id = self.store.add_comment(
            post_message_id=post_message_id,
            parent_comment_id=int(parent_comment["id"]) if parent_comment is not None else None,
            user_id=user_id,
            display_name=display_name,
            username=username,
            text=clean_text,
            media=media_items,
            source_message_id=source_message_id,
            discussion_copy_message_id=None,
            source_kind=source_kind,
        )
        comment_count = self.store.get_comment_count(post_message_id)
        comments_chat_id = self.resolve_post_comments_chat_id(post)
        if comments_chat_id is not None:
            discussion_text = self.build_discussion_comment_text(
                post_message_id=post_message_id,
                comment_count=comment_count,
                display_name=display_name,
                username=username,
                text=clean_text,
                media_items=media_items,
                parent_comment=parent_comment,
            )
            discussion_attachments = self.build_max_image_attachments(media_items)

            link_payload = None
            if post["discussion_message_id"]:
                link_payload = {"type": "reply", "mid": post["discussion_message_id"]}

            discussion_comment_message = self.send_message_with_attachment_retry(
                chat_id=comments_chat_id,
                text=discussion_text,
                attachments=discussion_attachments or None,
                link=link_payload,
            )
            discussion_copy_message_id = extract_message_id(discussion_comment_message)
            if discussion_copy_message_id:
                self.store.set_comment_discussion_copy_message_id(
                    comment_id=comment_id,
                    post_message_id=post_message_id,
                    discussion_copy_message_id=discussion_copy_message_id,
                )

        for admin_id in self.admin_notice_user_ids():
            self.api.send_message(
                user_id=admin_id,
                text=self.build_admin_comment_notice(
                    post_message_id=post_message_id,
                    display_name=display_name,
                    text=clean_text,
                    media_items=media_items,
                    parent_comment=parent_comment,
                ),
                notify=False,
            )

        self.notify_reply_target(
            post=post,
            parent_comment=parent_comment,
            replier_user_id=user_id,
            replier_display_name=display_name,
            replier_username=username,
            reply_text=clean_text,
            reply_media_items=media_items,
        )

        self.refresh_post_comment_button(
            post_message_id,
            comment_count=comment_count,
        )

        created = self.store.get_comment(comment_id)
        if created is None:
            raise MaxApiError("Comment was stored but could not be reloaded")
        return self.serialize_comment(created, comment_id_override=comment_id)

    def save_user_comment(
        self,
        message: dict[str, Any],
        pending: PendingComment,
        text: str,
    ) -> None:
        sender = message.get("sender") or {}
        user_id = int(sender["user_id"])
        display_name = self.user_display_name(sender)
        username = safe_text(sender.get("username")) or None
        source_message_id = safe_text(message.get("body", {}).get("mid") or message.get("mid")) or None

        try:
            self.submit_comment(
                post_message_id=pending.post_message_id,
                parent_comment_id=None,
                user_id=user_id,
                display_name=display_name,
                username=username,
                text=text,
                source_kind="bot",
                source_message_id=source_message_id,
            )
        except MaxApiError as exc:
            recoverable_errors = {
                "Comment is empty",
                "Comment is too long",
                "Links are not allowed in comments",
                COMMENT_BLOCKED_CODE,
            }
            if safe_text(str(exc)) in recoverable_errors:
                self.store.set_pending_comment(user_id, pending.post_message_id)
            self.api.send_message(
                user_id=user_id,
                text=humanize_comment_error_message(str(exc)),
            )
            return

        self.api.send_message(
            user_id=user_id,
            text="Комментарий принят. Он добавлен в обсуждение поста.",
        )

    def authenticate_webapp_user(self, init_data: str) -> AuthenticatedWebAppUser:
        return validate_webapp_init_data(
            init_data,
            bot_token=BOT_TOKEN,
            max_age_seconds=WEB_APP_AUTH_MAX_AGE_SECONDS,
        )

    def is_admin_user(self, user_id: int) -> bool:
        return self.admin_context_for_user(user_id) is not None

    def is_super_admin_user(self, user_id: int) -> bool:
        context = self.admin_context_for_user(user_id)
        return bool(context and context.is_super_admin)

    def admin_context_for_user(self, user_id: int) -> AdminContext | None:
        normalized_user_id = int(user_id)
        admin_user = self.store.get_admin_user_by_max_user_id(normalized_user_id)
        if admin_user is None or int(admin_user["is_active"]) != 1:
            return None
        role = safe_text(admin_user["role"])
        if role == ROLE_SUPER_ADMIN:
            return AdminContext(
                user_id=normalized_user_id,
                role=ROLE_SUPER_ADMIN,
                channel_ids=set(),
                admin_user_id=int(admin_user["id"]),
                must_change_password=bool(int(admin_user["must_change_password"])),
            )
        if role != ROLE_CHANNEL_ADMIN:
            return None
        channel_ids = self.store.list_channel_admin_channel_ids(normalized_user_id)
        return AdminContext(
            user_id=normalized_user_id,
            role=ROLE_CHANNEL_ADMIN,
            channel_ids=channel_ids,
            admin_user_id=int(admin_user["id"]),
            must_change_password=bool(int(admin_user["must_change_password"])),
        )

    def token_super_admin_context(self) -> AdminContext | None:
        return None

    def bound_channel_ids(self) -> set[int]:
        return {int(binding["channel_chat_id"]) for binding in self.list_channel_bindings()}

    def resolve_admin_channel_ids(
        self,
        context: AdminContext,
        *,
        requested_channel_id: int | None = None,
    ) -> set[int]:
        bound_ids = self.bound_channel_ids()
        if context.is_super_admin:
            if requested_channel_id is not None:
                channel_id = int(requested_channel_id)
                if channel_id not in bound_ids:
                    raise MaxApiError("Channel is not connected")
                return {channel_id}
            return bound_ids

        allowed_ids = context.channel_ids.intersection(bound_ids)
        if requested_channel_id is not None:
            channel_id = int(requested_channel_id)
            if channel_id not in allowed_ids:
                raise MaxApiError("Access denied")
            return {channel_id}
        if not allowed_ids:
            raise MaxApiError("Access denied")
        return allowed_ids

    def require_admin_access_to_channel(self, context: AdminContext, channel_id: int) -> None:
        self.resolve_admin_channel_ids(context, requested_channel_id=int(channel_id))

    def list_channel_bindings_for_admin(self, context: AdminContext) -> list[sqlite3.Row]:
        if context.is_super_admin:
            return self.list_channel_bindings()
        return self.store.list_channel_bindings_for_channels(context.channel_ids)

    def serialize_admin_identity(self, context: AdminContext) -> dict[str, Any]:
        return {
            "id": context.admin_user_id,
            "user_id": context.user_id,
            "login": context.login or safe_text(context.user_id),
            "role": context.role,
            "is_super_admin": context.is_super_admin,
            "must_change_password": context.must_change_password,
        }

    def serialize_admin_user(self, admin_user: sqlite3.Row) -> dict[str, Any]:
        raw_channel_ids = safe_text(admin_user["channel_ids"] if "channel_ids" in admin_user.keys() else "")
        channel_ids = [
            int(item)
            for item in raw_channel_ids.split(",")
            if safe_text(item)
        ]
        return {
            "id": int(admin_user["id"]),
            "max_user_id": safe_text(admin_user["max_user_id"]),
            "role": safe_text(admin_user["role"]),
            "must_change_password": bool(int(admin_user["must_change_password"])),
            "is_active": bool(int(admin_user["is_active"])),
            "channel_ids": channel_ids,
            "created_at": safe_text(admin_user["created_at"]),
            "updated_at": safe_text(admin_user["updated_at"]),
        }

    def require_super_admin(self, context: AdminContext) -> None:
        if not context.is_super_admin:
            raise MaxApiError("FORBIDDEN")

    def super_admin_panel_is_configured(self) -> bool:
        login = safe_text(SUPER_ADMIN_LOGIN)
        password = safe_text(SUPER_ADMIN_PASSWORD)
        return bool(login and password and ":" not in login and not hmac.compare_digest(login, password))

    def super_admin_panel_context(self) -> AdminContext:
        return AdminContext(
            user_id=0,
            role=ROLE_SUPER_ADMIN,
            channel_ids=set(),
            admin_user_id=0,
            must_change_password=False,
            login=safe_text(SUPER_ADMIN_LOGIN),
        )

    def sign_admin_session(self, max_user_id: str, expires_at: int) -> str:
        payload = f"{safe_text(max_user_id)}:{int(expires_at)}"
        return hmac.new(
            safe_text(ADMIN_SESSION_SECRET).encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def create_admin_session_token(self, max_user_id: str | int) -> str:
        normalized_user_id = safe_text(max_user_id)
        expires_at = int(time.time()) + ADMIN_SESSION_MAX_AGE_SECONDS
        signature = self.sign_admin_session(normalized_user_id, expires_at)
        return f"v1:{normalized_user_id}:{expires_at}:{signature}"

    def admin_context_from_session_token(self, token: str) -> AdminContext | None:
        parts = safe_text(token).split(":")
        if len(parts) != 4 or parts[0] != "v1":
            return None
        _version, max_user_id, raw_expires_at, signature = parts
        try:
            expires_at = int(raw_expires_at)
            user_id = int(max_user_id)
        except ValueError:
            return None
        if expires_at < int(time.time()):
            return None
        expected_signature = self.sign_admin_session(max_user_id, expires_at)
        if not hmac.compare_digest(signature, expected_signature):
            return None
        return self.admin_context_for_user(user_id)

    def sign_super_admin_session(self, login: str, expires_at: int) -> str:
        payload = f"super:{safe_text(login)}:{int(expires_at)}"
        return hmac.new(
            safe_text(ADMIN_SESSION_SECRET).encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def create_super_admin_session_token(self) -> str:
        login = safe_text(SUPER_ADMIN_LOGIN)
        expires_at = int(time.time()) + ADMIN_SESSION_MAX_AGE_SECONDS
        signature = self.sign_super_admin_session(login, expires_at)
        return f"v1:{login}:{expires_at}:{signature}"

    def super_admin_context_from_session_token(self, token: str) -> AdminContext | None:
        if not self.super_admin_panel_is_configured():
            return None
        parts = safe_text(token).split(":")
        if len(parts) != 4 or parts[0] != "v1":
            return None
        _version, login, raw_expires_at, signature = parts
        try:
            expires_at = int(raw_expires_at)
        except ValueError:
            return None
        if expires_at < int(time.time()):
            return None
        if not hmac.compare_digest(login, safe_text(SUPER_ADMIN_LOGIN)):
            return None
        expected_signature = self.sign_super_admin_session(login, expires_at)
        if not hmac.compare_digest(signature, expected_signature):
            return None
        return self.super_admin_panel_context()

    def authenticate_admin_credentials(self, *, login: str, password: str) -> AdminContext:
        normalized_login = safe_text(login)
        admin_user = self.store.get_admin_user_by_max_user_id(normalized_login)
        if admin_user is None or int(admin_user["is_active"]) != 1:
            raise MaxApiError("ACCESS_DENIED")
        if safe_text(admin_user["role"]) != ROLE_CHANNEL_ADMIN:
            raise MaxApiError("ACCESS_DENIED")
        if not verify_admin_password(password, safe_text(admin_user["password_hash"])):
            raise MaxApiError("INVALID_CREDENTIALS")
        try:
            user_id = int(admin_user["max_user_id"])
        except ValueError as exc:
            raise MaxApiError("ACCESS_DENIED") from exc
        context = self.admin_context_for_user(user_id)
        if context is None:
            raise MaxApiError("ACCESS_DENIED")
        return context

    def authenticate_super_admin_credentials(self, *, login: str, password: str) -> AdminContext:
        if not self.super_admin_panel_is_configured():
            raise MaxApiError("SUPER_ADMIN_NOT_CONFIGURED")
        login_matches = hmac.compare_digest(safe_text(login), safe_text(SUPER_ADMIN_LOGIN))
        password_matches = hmac.compare_digest(safe_text(password), safe_text(SUPER_ADMIN_PASSWORD))
        if not login_matches or not password_matches:
            raise MaxApiError("INVALID_CREDENTIALS")
        return self.super_admin_panel_context()

    def change_admin_password(
        self,
        context: AdminContext,
        *,
        old_password: str,
        new_password: str,
    ) -> dict[str, Any]:
        if context.admin_user_id is None:
            raise MaxApiError("ACCESS_DENIED")
        admin_user = self.store.get_admin_user_by_id(context.admin_user_id)
        if admin_user is None or int(admin_user["is_active"]) != 1:
            raise MaxApiError("ACCESS_DENIED")
        if not verify_admin_password(old_password, safe_text(admin_user["password_hash"])):
            raise MaxApiError("INVALID_CREDENTIALS")
        clean_new_password = safe_text(new_password)
        if len(clean_new_password) < 6:
            raise MaxApiError("Новый пароль должен быть не короче 6 символов.")
        self.store.set_admin_password(
            admin_user_id=int(admin_user["id"]),
            password=clean_new_password,
            must_change_password=False,
        )
        return {"ok": True}

    def primary_channel_payload(self, bindings: list[sqlite3.Row]) -> dict[str, Any] | None:
        if not bindings:
            return None
        return self.serialize_channel_binding(bindings[0])

    def serialize_admin_post(self, post: sqlite3.Row) -> dict[str, Any]:
        text = safe_text(post["post_text"])
        title = safe_text(post["post_title"] if "post_title" in post.keys() else "")
        if not title:
            title = snippet(text.splitlines()[0] if text.splitlines() else text, 90)
        return {
            "id": safe_text(post["post_message_id"]),
            "post_message_id": safe_text(post["post_message_id"]),
            "post_ref": post_ref_for_message_id(post["post_message_id"]),
            "channel_id": int(post["channel_chat_id"]),
            "channel_chat_id": int(post["channel_chat_id"]),
            "title": title or "Пост без заголовка",
            "content": text,
            "status": safe_text(post["status"] if "status" in post.keys() else "published"),
            "comment_count": int(post["comment_count"]),
            "post_url": safe_text(post["post_url"]),
            "webapp_url": self.direct_webapp_url(post["post_message_id"]),
            "created_at": safe_text(post["created_at"]),
            "updated_at": safe_text(post["updated_at"]),
        }

    def serialize_admin_comment(self, comment: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(comment["id"]),
            "post_id": safe_text(comment["post_message_id"]),
            "post_message_id": safe_text(comment["post_message_id"]),
            "channel_id": int(comment["channel_chat_id"]),
            "author": safe_text(comment["display_name"]),
            "username": safe_text(comment["username"]),
            "text": safe_text(comment["text"]),
            "status": safe_text(comment["status"] if "status" in comment.keys() else COMMENT_STATUS_ACTIVE),
            "created_at": safe_text(comment["created_at"]),
            "deleted_at": safe_text(comment["deleted_at"] if "deleted_at" in comment.keys() else ""),
            "delete_reason": safe_text(comment["delete_reason"] if "delete_reason" in comment.keys() else ""),
            "post_title": safe_text(comment["post_title"] if "post_title" in comment.keys() else "") or snippet(safe_text(comment["post_text"]), 90),
            "post_preview": snippet(safe_text(comment["post_text"]), 140),
            "reports_count": int(comment["report_count"] if "report_count" in comment.keys() else 0),
        }

    def serialize_report(self, report: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(report["id"]),
            "comment_id": int(report["comment_id"]),
            "post_id": safe_text(report["post_id"]),
            "channel_id": int(report["channel_id"]),
            "reason": safe_text(report["reason"]),
            "details": safe_text(report["details"]),
            "status": safe_text(report["status"]),
            "admin_comment": safe_text(report["admin_comment"]),
            "resolved_by_user_id": int(report["resolved_by_user_id"]) if report["resolved_by_user_id"] is not None else None,
            "resolved_at": safe_text(report["resolved_at"]),
            "created_at": safe_text(report["created_at"]),
            "updated_at": safe_text(report["updated_at"]),
            "comment_text": safe_text(report["comment_text"]),
            "comment_author": safe_text(report["comment_author"]),
            "comment_username": safe_text(report["comment_username"]),
            "comment_created_at": safe_text(report["comment_created_at"]),
            "comment_status": safe_text(report["comment_status"]),
            "post_title": safe_text(report["post_title"] if "post_title" in report.keys() else "") or snippet(safe_text(report["post_text"]), 90),
            "post_preview": snippet(safe_text(report["post_text"]), 140),
            "reports_count": int(report["report_count"] if "report_count" in report.keys() else 0),
        }

    def admin_dashboard(
        self,
        context: AdminContext,
        *,
        requested_channel_id: int | None = None,
    ) -> dict[str, Any]:
        channel_ids = self.resolve_admin_channel_ids(
            context,
            requested_channel_id=requested_channel_id,
        )
        bindings = [
            binding
            for binding in self.list_channel_bindings_for_admin(context)
            if int(binding["channel_chat_id"]) in channel_ids
        ]
        latest_posts = self.store.list_posts(
            limit=6,
            channel_ids=channel_ids,
            status="published",
        )
        latest_comments = self.store.list_admin_comments(
            channel_ids=channel_ids,
            status=COMMENT_STATUS_ACTIVE,
            limit=6,
        )
        latest_reports = self.store.list_reports(
            channel_ids=channel_ids,
            limit=6,
        )
        return {
            "ok": True,
            "admin": self.serialize_admin_identity(context),
            "app": {
                "version": APP_VERSION,
                "delivery_mode": self.delivery_mode,
                "web_app_public_url": WEB_APP_PUBLIC_URL,
                "webhook_public_url": self.webhook_url(),
                "webhook_path": WEBHOOK_PATH,
                "bot_username": self.get_bot_username(),
                "channel_sync_interval_seconds": CHANNEL_SYNC_INTERVAL_SECONDS,
                "admin_count": len(self.store.list_admin_users()),
            },
            "channel": self.primary_channel_payload(bindings),
            "channels": [self.serialize_channel_binding(binding) for binding in bindings],
            "stats": {
                **self.store.dashboard_stats(channel_ids=channel_ids),
                "newRequestsCount": (
                    self.store.count_channel_connection_requests(status=CHANNEL_REQUEST_STATUS_PENDING)
                    if context.is_super_admin
                    else 0
                ),
            },
            "latestPosts": [self.serialize_admin_post(post) for post in latest_posts],
            "latestComments": [self.serialize_admin_comment(comment) for comment in latest_comments],
            "latestReports": [self.serialize_report(report) for report in latest_reports],
            "latestRequests": (
                [
                    self.serialize_channel_connection_request(row)
                    for row in self.store.list_channel_connection_requests(limit=6)
                ]
                if context.is_super_admin
                else []
            ),
        }

    def serialize_channel_connection_request(
        self,
        row: sqlite3.Row,
        *,
        include_raw_payload: bool = False,
    ) -> dict[str, Any]:
        raw_payload: dict[str, Any] = {}
        if include_raw_payload and safe_text(row["raw_payload"]):
            try:
                decoded = json.loads(row["raw_payload"])
            except json.JSONDecodeError:
                decoded = {}
            raw_payload = decoded if isinstance(decoded, dict) else {}
        channel_id = safe_text(row["channel_id"])
        payload = {
            "id": int(row["id"]),
            "channelId": channel_id,
            "channel_id": channel_id,
            "channelTitle": safe_text(row["channel_title"]),
            "channel_title": safe_text(row["channel_title"]),
            "forwardedPostId": safe_text(row["forwarded_post_id"]),
            "forwarded_post_id": safe_text(row["forwarded_post_id"]),
            "requesterUserId": int(row["requester_user_id"]) if row["requester_user_id"] is not None else None,
            "requester_user_id": int(row["requester_user_id"]) if row["requester_user_id"] is not None else None,
            "requesterMaxUserId": safe_text(row["requester_max_user_id"]),
            "requester_max_user_id": safe_text(row["requester_max_user_id"]),
            "status": safe_text(row["status"]),
            "adminComment": safe_text(row["admin_comment"]),
            "admin_comment": safe_text(row["admin_comment"]),
            "reviewedByAdminId": int(row["reviewed_by_admin_id"]) if row["reviewed_by_admin_id"] is not None else None,
            "reviewed_by_admin_id": int(row["reviewed_by_admin_id"]) if row["reviewed_by_admin_id"] is not None else None,
            "reviewedAt": safe_text(row["reviewed_at"]),
            "reviewed_at": safe_text(row["reviewed_at"]),
            "createdAt": safe_text(row["created_at"]),
            "created_at": safe_text(row["created_at"]),
            "updatedAt": safe_text(row["updated_at"]),
            "updated_at": safe_text(row["updated_at"]),
        }
        if include_raw_payload:
            payload["rawPayload"] = raw_payload
            payload["raw_payload"] = raw_payload
        return payload

    def admin_list_channel_requests(
        self,
        context: AdminContext,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        self.require_super_admin(context)
        clean_status = safe_text(status)
        if clean_status and clean_status not in CHANNEL_REQUEST_STATUSES:
            raise MaxApiError("Некорректный статус заявки.")
        rows = self.store.list_channel_connection_requests(
            status=clean_status or None,
            limit=limit,
            offset=offset,
        )
        return {
            "ok": True,
            "requests": [self.serialize_channel_connection_request(row) for row in rows],
        }

    def admin_get_channel_request(self, context: AdminContext, *, request_id: int) -> dict[str, Any]:
        self.require_super_admin(context)
        row = self.store.get_channel_connection_request(request_id)
        if row is None:
            raise MaxApiError("Заявка не найдена.")
        return {
            "ok": True,
            "request": self.serialize_channel_connection_request(row, include_raw_payload=True),
        }

    def notify_requester_about_channel_request_result(self, row: sqlite3.Row, *, approved: bool) -> None:
        requester_user_id = validate_positive_int(row["requester_user_id"], minimum=1, maximum=10**18)
        if requester_user_id is None:
            return
        channel_title = safe_text(row["channel_title"]) or safe_text(row["channel_id"])
        if approved:
            text = (
                "Заявка одобрена.\n\n"
                f"Канал “{channel_title}” подключён к боту. "
                "Теперь кнопка “Комментарии” будет автоматически появляться у новых постов канала.\n\n"
                f"Панель администратора канала:\n{public_app_url('/admin')}\n\n"
                "Логин: ваш MAX user id\n"
                "Пароль по умолчанию: ваш MAX user id"
            )
        else:
            reason = safe_text(row["admin_comment"]) or "Причина не указана"
            text = (
                "Заявка на подключение канала отклонена.\n\n"
                f"Канал: {channel_title}\n\n"
                "Причина:\n"
                f"{reason}\n\n"
                "Вы можете исправить замечания и отправить заявку повторно."
            )
        try:
            self.api.send_message(user_id=int(requester_user_id), text=text)
        except Exception:
            logger.exception("Failed to notify requester %s about channel request result", requester_user_id)

    def admin_approve_channel_request(
        self,
        context: AdminContext,
        *,
        request_id: int,
        admin_comment: str | None = None,
    ) -> dict[str, Any]:
        self.require_super_admin(context)
        row = self.store.get_channel_connection_request(request_id)
        if row is None:
            raise MaxApiError("Заявка не найдена.")
        if safe_text(row["status"]) != CHANNEL_REQUEST_STATUS_PENDING:
            raise MaxApiError("Заявка уже обработана.")
        channel_id = validate_positive_int(row["channel_id"], minimum=-10**18, maximum=10**18)
        if channel_id is None or channel_id == 0:
            raise MaxApiError("Некорректный ID канала в заявке.")
        if self.get_channel_binding(int(channel_id)) is not None:
            duplicate = self.store.update_channel_connection_request_status(
                request_id=request_id,
                status=CHANNEL_REQUEST_STATUS_DUPLICATE,
                admin_comment="Канал уже подключён.",
                reviewed_by_admin_id=context.user_id,
            )
            self.store.add_admin_audit_log(
                admin_user_id=context.user_id,
                channel_id=int(channel_id),
                action="duplicate_channel_request",
                entity_type="channel_connection_request",
                entity_id=request_id,
                payload={"status": safe_text(duplicate["status"]) if duplicate is not None else "duplicate"},
            )
            raise MaxApiError("Канал уже подключён.")

        can_manage, manage_error = self.ensure_bot_can_manage_channel(int(channel_id))
        if not can_manage:
            raise MaxApiError(manage_error or "Бот не является администратором канала.")

        requester_max_user_id = safe_text(row["requester_max_user_id"])
        self.store.upsert_channel_binding(
            channel_chat_id=int(channel_id),
            comments_chat_id=WEBAPP_ONLY_COMMENTS_CHAT_ID,
            comments_chat_url=None,
        )
        existing_requester_admin = self.store.get_admin_user_by_max_user_id(requester_max_user_id)
        if existing_requester_admin is None or safe_text(existing_requester_admin["role"]) != ROLE_SUPER_ADMIN:
            self.store.ensure_admin_user(
                max_user_id=requester_max_user_id,
                role=ROLE_CHANNEL_ADMIN,
                password=requester_max_user_id,
                must_change_password=True,
                is_active=True,
            )
        self.store.add_channel_admin(
            user_id=int(requester_max_user_id),
            channel_id=int(channel_id),
        )
        updated = self.store.update_channel_connection_request_status(
            request_id=request_id,
            status=CHANNEL_REQUEST_STATUS_APPROVED,
            admin_comment=admin_comment,
            reviewed_by_admin_id=context.user_id,
        )
        if updated is None:
            raise MaxApiError("Заявка не найдена.")
        self.store.add_admin_audit_log(
            admin_user_id=context.user_id,
            channel_id=int(channel_id),
            action="approve_channel_request",
            entity_type="channel_connection_request",
            entity_id=request_id,
            payload={
                "requester_max_user_id": requester_max_user_id,
                "forwarded_post_id": safe_text(row["forwarded_post_id"]),
            },
        )
        self.start_channel_sync()
        attached_count = 0
        try:
            attached_count = self.sync_recent_channel_posts_for_binding(
                channel_chat_id=int(channel_id),
                comments_chat_id=WEBAPP_ONLY_COMMENTS_CHAT_ID,
            )
        except Exception:
            logger.exception("Failed to sync newly approved channel %s", channel_id)
        forwarded_post_id = safe_text(row["forwarded_post_id"])
        if forwarded_post_id:
            try:
                if self.store.get_post(forwarded_post_id) is None:
                    self.attach_existing_post(forwarded_post_id, admin_user_id=None)
                    attached_count += 1
            except Exception:
                logger.exception("Failed to attach comments to forwarded post %s", forwarded_post_id)
                try:
                    raw_payload = json.loads(safe_text(row["raw_payload"]) or "{}")
                except json.JSONDecodeError:
                    raw_payload = {}
                forwarded = self.extract_forwarded_channel_post(raw_payload) if isinstance(raw_payload, dict) else None
                if forwarded is not None:
                    try:
                        self.register_channel_post_for_comments(
                            post_message_id=forwarded_post_id,
                            channel_chat_id=int(channel_id),
                            comments_chat_id=WEBAPP_ONLY_COMMENTS_CHAT_ID,
                            post_url=forwarded.post_url,
                            post_text=forwarded.post_text,
                            source_attachments=forwarded.post_attachments,
                        )
                        attached_count += 1
                    except Exception:
                        logger.exception("Failed to attach forwarded raw payload post %s", forwarded_post_id)
        self.notify_requester_about_channel_request_result(updated, approved=True)
        binding = self.get_channel_binding(int(channel_id))
        return {
            "ok": True,
            "request": self.serialize_channel_connection_request(updated),
            "binding": self.serialize_channel_binding(binding) if binding is not None else None,
            "attached_count": attached_count,
        }

    def admin_reject_channel_request(
        self,
        context: AdminContext,
        *,
        request_id: int,
        admin_comment: str | None = None,
    ) -> dict[str, Any]:
        self.require_super_admin(context)
        row = self.store.get_channel_connection_request(request_id)
        if row is None:
            raise MaxApiError("Заявка не найдена.")
        if safe_text(row["status"]) != CHANNEL_REQUEST_STATUS_PENDING:
            raise MaxApiError("Заявка уже обработана.")
        updated = self.store.update_channel_connection_request_status(
            request_id=request_id,
            status=CHANNEL_REQUEST_STATUS_REJECTED,
            admin_comment=admin_comment,
            reviewed_by_admin_id=context.user_id,
        )
        if updated is None:
            raise MaxApiError("Заявка не найдена.")
        channel_id = validate_positive_int(row["channel_id"], minimum=-10**18, maximum=10**18) or 0
        self.store.add_admin_audit_log(
            admin_user_id=context.user_id,
            channel_id=int(channel_id),
            action="reject_channel_request",
            entity_type="channel_connection_request",
            entity_id=request_id,
            payload={"admin_comment": safe_text(admin_comment)},
        )
        self.notify_requester_about_channel_request_result(updated, approved=False)
        return {
            "ok": True,
            "request": self.serialize_channel_connection_request(updated),
        }

    def admin_list_posts(
        self,
        context: AdminContext,
        *,
        requested_channel_id: int | None = None,
    ) -> dict[str, Any]:
        channel_ids = self.resolve_admin_channel_ids(
            context,
            requested_channel_id=requested_channel_id,
        )
        posts = self.store.list_posts(limit=100, channel_ids=channel_ids)
        return {
            "ok": True,
            "posts": [self.serialize_admin_post(post) for post in posts],
        }

    def admin_list_users(self, context: AdminContext) -> dict[str, Any]:
        self.require_super_admin(context)
        return {
            "ok": True,
            "users": [
                self.serialize_admin_user(row)
                for row in self.store.list_admin_users()
                if safe_text(row["role"]) == ROLE_CHANNEL_ADMIN
            ],
        }

    def normalize_admin_channel_ids(self, raw_channel_ids: Any) -> set[int]:
        if raw_channel_ids is None:
            return set()
        if not isinstance(raw_channel_ids, list):
            raise MaxApiError("Список каналов должен быть массивом.")
        normalized_ids: set[int] = set()
        bound_ids = self.bound_channel_ids()
        for item in raw_channel_ids:
            channel_id = validate_positive_int(item, minimum=-10**18, maximum=10**18)
            if channel_id is None:
                raise MaxApiError("Некорректный ID канала.")
            if int(channel_id) not in bound_ids:
                raise MaxApiError("Channel is not connected")
            normalized_ids.add(int(channel_id))
        return normalized_ids

    def admin_save_user(
        self,
        context: AdminContext,
        *,
        max_user_id: str,
        role: str,
        channel_ids: set[int],
        is_active: bool = True,
    ) -> dict[str, Any]:
        self.require_super_admin(context)
        clean_user_id = safe_text(max_user_id)
        if not clean_user_id.isdigit():
            raise MaxApiError("MAX user id должен быть числом.")
        normalized_role = safe_text(role) or ROLE_CHANNEL_ADMIN
        if normalized_role != ROLE_CHANNEL_ADMIN:
            raise MaxApiError("FORBIDDEN")
        if not channel_ids:
            raise MaxApiError("Для channel_admin выберите хотя бы один канал.")
        row = self.store.ensure_admin_user(
            max_user_id=clean_user_id,
            role=normalized_role,
            password=clean_user_id,
            must_change_password=True,
            is_active=is_active,
        )
        existing_channel_ids = self.store.list_channel_admin_channel_ids(int(clean_user_id))
        merged_channel_ids = existing_channel_ids.union(channel_ids)
        self.store.replace_admin_channels(max_user_id=clean_user_id, channel_ids=merged_channel_ids)
        refreshed = self.store.get_admin_user_by_id(int(row["id"]))
        self.store.add_admin_audit_log(
            admin_user_id=context.user_id,
            channel_id=0,
            action="save_admin_user",
            entity_type="admin_user",
            entity_id=int(row["id"]),
            payload={
                "max_user_id": clean_user_id,
                "role": normalized_role,
                "added_channel_ids": sorted(channel_ids),
                "channel_ids": sorted(merged_channel_ids),
                "is_active": is_active,
            },
        )
        return {
            "ok": True,
            "user": self.serialize_admin_user(
                next(
                    item
                    for item in self.store.list_admin_users()
                    if int(item["id"]) == int(refreshed["id"])
                )
            ),
        }

    def admin_update_user(
        self,
        context: AdminContext,
        *,
        admin_user_id: int,
        role: str | None,
        channel_ids: set[int] | None,
        is_active: bool | None,
    ) -> dict[str, Any]:
        self.require_super_admin(context)
        existing = self.store.get_admin_user_by_id(admin_user_id)
        if existing is None:
            raise MaxApiError("Admin user not found")
        if safe_text(existing["role"]) != ROLE_CHANNEL_ADMIN:
            raise MaxApiError("FORBIDDEN")
        normalized_role = safe_text(role) or ROLE_CHANNEL_ADMIN
        if normalized_role != ROLE_CHANNEL_ADMIN:
            raise MaxApiError("FORBIDDEN")
        if channel_ids is not None and not channel_ids:
            raise MaxApiError("Для channel_admin выберите хотя бы один канал.")
        updated = self.store.update_admin_user(
            admin_user_id=admin_user_id,
            role=normalized_role,
            is_active=is_active,
        )
        if updated is None:
            raise MaxApiError("Admin user not found")
        if channel_ids is not None:
            self.store.replace_admin_channels(max_user_id=updated["max_user_id"], channel_ids=channel_ids)
        self.store.add_admin_audit_log(
            admin_user_id=context.user_id,
            channel_id=0,
            action="update_admin_user",
            entity_type="admin_user",
            entity_id=admin_user_id,
            payload={
                "role": normalized_role,
                "channel_ids": sorted(channel_ids) if channel_ids is not None else None,
                "is_active": is_active,
            },
        )
        refreshed = next(
            item
            for item in self.store.list_admin_users()
            if int(item["id"]) == int(admin_user_id)
        )
        return {"ok": True, "user": self.serialize_admin_user(refreshed)}

    def admin_reset_user_password(
        self,
        context: AdminContext,
        *,
        admin_user_id: int,
    ) -> dict[str, Any]:
        self.require_super_admin(context)
        row = self.store.get_admin_user_by_id(admin_user_id)
        if row is None:
            raise MaxApiError("Admin user not found")
        if safe_text(row["role"]) != ROLE_CHANNEL_ADMIN:
            raise MaxApiError("FORBIDDEN")
        updated = self.store.set_admin_password(
            admin_user_id=admin_user_id,
            password=safe_text(row["max_user_id"]),
            must_change_password=True,
        )
        if updated is None:
            raise MaxApiError("Admin user not found")
        self.store.add_admin_audit_log(
            admin_user_id=context.user_id,
            channel_id=0,
            action="reset_admin_password",
            entity_type="admin_user",
            entity_id=admin_user_id,
            payload={"max_user_id": safe_text(row["max_user_id"])},
        )
        return {"ok": True}

    def admin_delete_user_channel(
        self,
        context: AdminContext,
        *,
        admin_user_id: int,
        channel_id: int,
    ) -> dict[str, Any]:
        self.require_super_admin(context)
        row = self.store.get_admin_user_by_id(admin_user_id)
        if row is None:
            raise MaxApiError("Admin user not found")
        if safe_text(row["role"]) != ROLE_CHANNEL_ADMIN:
            raise MaxApiError("FORBIDDEN")
        deleted = self.store.delete_admin_channel(
            max_user_id=safe_text(row["max_user_id"]),
            channel_id=int(channel_id),
        )
        if not deleted:
            raise MaxApiError("Channel is not connected")
        self.store.add_admin_audit_log(
            admin_user_id=context.user_id,
            channel_id=int(channel_id),
            action="delete_admin_channel",
            entity_type="admin_user",
            entity_id=admin_user_id,
            payload={"max_user_id": safe_text(row["max_user_id"])},
        )
        return {"ok": True}

    def admin_create_post(
        self,
        context: AdminContext,
        *,
        requested_channel_id: int | None,
        title: str,
        content: str,
    ) -> dict[str, Any]:
        clean_title = safe_text(title)
        clean_content = safe_text(content)
        if len(clean_title) > 160:
            raise MaxApiError("Заголовок слишком длинный. Максимум 160 символов.")
        if len(clean_content) > 4000:
            raise MaxApiError("Текст поста слишком длинный. Максимум 4000 символов.")
        if not clean_title and not clean_content:
            raise MaxApiError("Post text is empty")

        channel_ids = self.resolve_admin_channel_ids(
            context,
            requested_channel_id=requested_channel_id,
        )
        if len(channel_ids) != 1:
            raise MaxApiError("Выберите канал для публикации.")
        channel_id = next(iter(channel_ids))
        publish_text = "\n\n".join(part for part in [clean_title, clean_content] if part)
        result = self.publish_post(
            publish_text,
            admin_user_id=None,
            channel_chat_id=channel_id,
            post_title=clean_title or None,
        )
        post = self.store.get_post(result["post_message_id"])
        if post is None:
            raise MaxApiError("Post was registered but could not be reloaded")
        self.store.add_admin_audit_log(
            admin_user_id=context.user_id,
            channel_id=channel_id,
            action="create_post",
            entity_type="post",
            entity_id=post["post_message_id"],
            payload={"title": clean_title},
        )
        return {
            "ok": True,
            "post": self.serialize_admin_post(post),
        }

    def admin_get_post(
        self,
        context: AdminContext,
        *,
        post_reference: str,
    ) -> dict[str, Any]:
        post = self.resolve_post_reference(post_reference)
        if post is None:
            raise MaxApiError("Post not found")
        self.require_admin_access_to_channel(context, int(post["channel_chat_id"]))
        return {
            "ok": True,
            "post": self.serialize_admin_post(post),
        }

    def admin_delete_post(
        self,
        context: AdminContext,
        *,
        post_reference: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        post = self.resolve_post_reference(post_reference)
        if post is None:
            raise MaxApiError("Post not found")
        channel_id = int(post["channel_chat_id"])
        self.require_admin_access_to_channel(context, channel_id)
        deleted = self.store.soft_delete_post(
            post_message_id=post["post_message_id"],
            deleted_by_user_id=context.user_id,
            reason=reason or "Удалено администратором",
        )
        if deleted is None:
            raise MaxApiError("Post not found")
        self.store.add_admin_audit_log(
            admin_user_id=context.user_id,
            channel_id=channel_id,
            action="delete_post",
            entity_type="post",
            entity_id=post["post_message_id"],
            payload={"reason": safe_text(reason)},
        )
        return {
            "ok": True,
            "post": self.serialize_admin_post(deleted),
        }

    def admin_list_comments(
        self,
        context: AdminContext,
        *,
        requested_channel_id: int | None = None,
        post_reference: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        normalized_status = safe_text(status)
        if normalized_status and normalized_status not in COMMENT_STATUSES:
            raise MaxApiError("Invalid comment status")
        channel_ids = self.resolve_admin_channel_ids(
            context,
            requested_channel_id=requested_channel_id,
        )
        post_message_id = None
        if post_reference:
            post = self.resolve_post_reference(post_reference)
            if post is None:
                raise MaxApiError("Post not found")
            self.require_admin_access_to_channel(context, int(post["channel_chat_id"]))
            post_message_id = safe_text(post["post_message_id"])
            channel_ids = {int(post["channel_chat_id"])}
        comments = self.store.list_admin_comments(
            channel_ids=channel_ids,
            post_message_id=post_message_id,
            status=normalized_status or None,
            limit=200,
        )
        return {
            "ok": True,
            "comments": [self.serialize_admin_comment(comment) for comment in comments],
        }

    def admin_set_comment_status(
        self,
        context: AdminContext,
        *,
        comment_id: int,
        status: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        normalized_status = safe_text(status)
        if normalized_status not in COMMENT_STATUSES:
            raise MaxApiError("Invalid comment status")
        clean_reason = safe_text(reason)
        if len(clean_reason) > 500:
            raise MaxApiError("Причина слишком длинная. Максимум 500 символов.")
        comment = self.store.get_comment(comment_id)
        if comment is None:
            raise MaxApiError("Comment not found")
        post = self.store.get_post(comment["post_message_id"])
        if post is None:
            raise MaxApiError("Post not found")
        channel_id = int(post["channel_chat_id"])
        self.require_admin_access_to_channel(context, channel_id)

        updated = self.store.set_comment_status(
            comment_id=comment_id,
            status=normalized_status,
            moderator_user_id=context.user_id,
            reason=clean_reason or None,
        )
        if updated is None:
            raise MaxApiError("Comment not found")
        if normalized_status != COMMENT_STATUS_ACTIVE:
            self.delete_comment_media_files(deserialize_comment_media(comment["media_json"]))
            self.delete_comment_discussion_copy(comment)
        self.refresh_post_comment_button(
            post["post_message_id"],
            comment_count=self.store.get_comment_count(post["post_message_id"]),
        )
        self.store.add_admin_audit_log(
            admin_user_id=context.user_id,
            channel_id=channel_id,
            action="restore_comment" if normalized_status == COMMENT_STATUS_ACTIVE else "delete_comment",
            entity_type="comment",
            entity_id=comment_id,
            payload={"status": normalized_status, "reason": clean_reason},
        )
        return {
            "ok": True,
            "comment": self.serialize_comment(updated),
            "admin_comment": None,
        }

    def report_comment_from_webapp(
        self,
        *,
        comment_id: int,
        init_data: str,
        reason: str,
        details: str | None = None,
    ) -> dict[str, Any]:
        auth_user = self.authenticate_webapp_user(init_data)
        normalized_reason = safe_text(reason)
        if normalized_reason not in REPORT_REASONS:
            raise MaxApiError("Invalid report reason")
        clean_details = safe_text(details)
        if len(clean_details) > 1000:
            raise MaxApiError("Описание жалобы слишком длинное. Максимум 1000 символов.")

        comment = self.store.get_comment(comment_id)
        if comment is None:
            raise MaxApiError("Comment not found")
        if safe_text(comment["status"] if "status" in comment.keys() else COMMENT_STATUS_ACTIVE) != COMMENT_STATUS_ACTIVE:
            raise MaxApiError("Comment not found")
        if int(comment["user_id"]) == int(auth_user.user_id):
            raise MaxApiError("Нельзя пожаловаться на свой комментарий.")
        post = self.store.get_post(comment["post_message_id"])
        if post is None or safe_text(post["status"] if "status" in post.keys() else "published") != "published":
            raise MaxApiError("Comment not found")

        try:
            self.store.create_comment_report(
                comment_id=comment_id,
                post_id=safe_text(comment["post_message_id"]),
                channel_id=int(post["channel_chat_id"]),
                reporter_user_id=auth_user.user_id,
                reason=normalized_reason,
                details=clean_details or None,
            )
        except sqlite3.IntegrityError as exc:
            raise MaxApiError("Report already exists") from exc
        return {
            "ok": True,
            "message": REPORT_CREATED_MESSAGE,
        }

    def set_comment_reaction_from_webapp(
        self,
        *,
        comment_id: int,
        init_data: str,
        emoji: str,
    ) -> dict[str, Any]:
        auth_user = self.authenticate_webapp_user(init_data)
        normalized_emoji = safe_text(emoji)
        if normalized_emoji not in ALLOWED_COMMENT_REACTION_SET:
            raise MaxApiError("INVALID_REACTION")

        comment = self.store.get_comment(comment_id)
        if comment is None:
            raise MaxApiError("Comment not found")
        if safe_text(comment["status"] if "status" in comment.keys() else COMMENT_STATUS_ACTIVE) != COMMENT_STATUS_ACTIVE:
            raise MaxApiError("Comment not found")
        post = self.store.get_post(comment["post_message_id"])
        if post is None or safe_text(post["status"] if "status" in post.keys() else "published") != "published":
            raise MaxApiError("Comment not found")

        reaction_summary = self.store.toggle_comment_reaction(
            comment_id=int(comment_id),
            user_id=auth_user.user_id,
            emoji=normalized_emoji,
        )
        return {
            "ok": True,
            "commentId": int(comment_id),
            "comment_id": int(comment_id),
            "myReaction": reaction_summary.get("myReaction"),
            "my_reaction": reaction_summary.get("my_reaction"),
            "reactions": reaction_summary.get("reactions") or [],
        }

    def admin_list_reports(
        self,
        context: AdminContext,
        *,
        requested_channel_id: int | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        normalized_status = safe_text(status)
        if normalized_status and normalized_status not in REPORT_STATUSES:
            raise MaxApiError("Invalid report status")
        channel_ids = self.resolve_admin_channel_ids(
            context,
            requested_channel_id=requested_channel_id,
        )
        reports = self.store.list_reports(
            channel_ids=channel_ids,
            status=normalized_status or None,
            limit=200,
        )
        return {
            "ok": True,
            "reports": [self.serialize_report(report) for report in reports],
        }

    def admin_get_report(self, context: AdminContext, *, report_id: int) -> dict[str, Any]:
        report = self.store.get_report(report_id)
        if report is None:
            raise MaxApiError("Report not found")
        self.require_admin_access_to_channel(context, int(report["channel_id"]))
        return {
            "ok": True,
            "report": self.serialize_report(report),
        }

    def admin_update_report(
        self,
        context: AdminContext,
        *,
        report_id: int,
        status: str,
        action: str | None = None,
        admin_comment: str | None = None,
    ) -> dict[str, Any]:
        normalized_status = safe_text(status)
        if normalized_status not in REPORT_STATUSES:
            raise MaxApiError("Invalid report status")
        clean_admin_comment = safe_text(admin_comment)
        if len(clean_admin_comment) > 1000:
            raise MaxApiError("Комментарий администратора слишком длинный. Максимум 1000 символов.")
        report = self.store.get_report(report_id)
        if report is None:
            raise MaxApiError("Report not found")
        channel_id = int(report["channel_id"])
        self.require_admin_access_to_channel(context, channel_id)

        normalized_action = safe_text(action)
        if normalized_action == "delete_comment":
            comment = self.store.get_comment(int(report["comment_id"]))
            if comment is not None:
                deleted = self.store.delete_comment(
                    comment_id=int(report["comment_id"]),
                    post_message_id=safe_text(report["post_id"]),
                    deleted_by_user_id=context.user_id,
                    reason=clean_admin_comment or "Жалоба принята",
                )
                if deleted is not None:
                    self.delete_comment_media_files(deserialize_comment_media(deleted["media_json"]))
                    self.delete_comment_discussion_copy(deleted)
                    self.refresh_post_comment_button(
                        safe_text(report["post_id"]),
                        comment_count=self.store.get_comment_count(safe_text(report["post_id"])),
                    )
                    self.store.add_admin_audit_log(
                        admin_user_id=context.user_id,
                        channel_id=channel_id,
                        action="delete_comment",
                        entity_type="comment",
                        entity_id=int(report["comment_id"]),
                        payload={"source": "report", "report_id": report_id},
                    )
            normalized_status = "accepted"

        updated = self.store.update_report(
            report_id=report_id,
            status=normalized_status,
            admin_comment=clean_admin_comment or None,
            resolved_by_user_id=context.user_id if normalized_status in {"accepted", "rejected"} else None,
        )
        if updated is None:
            raise MaxApiError("Report not found")
        audit_action = {
            "in_review": "review_report",
            "accepted": "accept_report",
            "rejected": "reject_report",
        }.get(normalized_status, "update_report")
        self.store.add_admin_audit_log(
            admin_user_id=context.user_id,
            channel_id=channel_id,
            action=audit_action,
            entity_type="report",
            entity_id=report_id,
            payload={
                "status": normalized_status,
                "action": normalized_action,
                "admin_comment": clean_admin_comment,
            },
        )
        return {
            "ok": True,
            "report": self.serialize_report(updated),
        }

    def build_viewer_payload(
        self,
        init_data: str,
        post: sqlite3.Row | None = None,
    ) -> dict[str, Any] | None:
        viewer = self.authenticate_optional_webapp_user(init_data)
        return self.build_viewer_payload_for_user(viewer, post=post)

    def authenticate_optional_webapp_user(self, init_data: str) -> AuthenticatedWebAppUser | None:
        clean_init_data = safe_text(init_data)
        if not clean_init_data:
            return None
        try:
            return self.authenticate_webapp_user(clean_init_data)
        except WebAppAuthError:
            return None

    def build_viewer_payload_for_user(
        self,
        viewer: AuthenticatedWebAppUser | None,
        post: sqlite3.Row | None = None,
    ) -> dict[str, Any] | None:
        if viewer is None:
            return None
        admin_context = self.admin_context_for_user(viewer.user_id)
        can_admin_current_post = False
        if admin_context is not None:
            if post is None or admin_context.is_super_admin:
                can_admin_current_post = True
            else:
                try:
                    self.require_admin_access_to_channel(admin_context, int(post["channel_chat_id"]))
                    can_admin_current_post = True
                except MaxApiError:
                    can_admin_current_post = False
        return {
            "user_id": viewer.user_id,
            "display_name": viewer.display_name,
            "username": viewer.username,
            "is_admin": can_admin_current_post,
            "role": admin_context.role if admin_context else ROLE_USER,
        }

    def comment_read_payload(
        self,
        *,
        post_message_id: str,
        viewer: AuthenticatedWebAppUser | None,
    ) -> dict[str, Any]:
        latest_comment_id = self.store.get_latest_active_comment_id(post_message_id)
        last_read_comment_id = None
        if viewer is not None:
            read_state = self.store.get_comment_read_state(
                user_id=viewer.user_id,
                post_message_id=post_message_id,
            )
            if read_state is not None and read_state["last_read_comment_id"] is not None:
                last_read_comment_id = int(read_state["last_read_comment_id"])
        has_unread = bool(
            viewer is not None
            and latest_comment_id is not None
            and (
                last_read_comment_id is None
                or int(last_read_comment_id) < int(latest_comment_id)
            )
        )
        return {
            "readState": {
                "lastReadCommentId": last_read_comment_id,
                "last_read_comment_id": last_read_comment_id,
            },
            "targetCommentId": latest_comment_id,
            "target_comment_id": latest_comment_id,
            "hasUnread": has_unread,
            "has_unread": has_unread,
        }

    def get_post_payload(self, reference: str) -> dict[str, Any]:
        post = self.resolve_post_reference(reference)
        if post is None:
            raise MaxApiError("Post not found")
        if safe_text(post["status"] if "status" in post.keys() else "published") != "published":
            raise MaxApiError("Post not found")
        if safe_text(post["status"] if "status" in post.keys() else "published") != "published":
            raise MaxApiError("Post not found")
        return self.serialize_post(post)

    def get_comments_payload(
        self,
        reference: str,
        *,
        limit: int = COMMENTS_PAGE_SIZE_DEFAULT,
        before_comment_id: int | None = None,
        viewer_init_data: str = "",
    ) -> dict[str, Any]:
        post = self.resolve_post_reference(reference)
        if post is None:
            raise MaxApiError("Post not found")
        if safe_text(post["status"] if "status" in post.keys() else "published") != "published":
            raise MaxApiError("Post not found")
        viewer = self.authenticate_optional_webapp_user(viewer_init_data)
        normalized_limit = normalize_comments_page_limit(limit)
        rows, has_more = self.store.list_comments_page(
            post["post_message_id"],
            limit=normalized_limit,
            before_comment_id=before_comment_id,
        )
        reaction_summaries = self.store.list_comment_reaction_summaries(
            [int(row["id"]) for row in rows],
            viewer_user_id=viewer.user_id if viewer is not None else None,
        )
        parent_ids = [
            int(row["parent_comment_id"])
            for row in rows
            if row["parent_comment_id"] is not None
        ]
        parent_rows = self.store.list_comments_by_ids(parent_ids)
        comments = [
            self.serialize_comment(
                row,
                parent_comment=parent_rows.get(int(row["parent_comment_id"]))
                if row["parent_comment_id"] is not None
                else None,
                reaction_summary=reaction_summaries.get(int(row["id"])),
            )
            for row in rows
        ]
        oldest_comment_id = comments[0]["id"] if comments else None
        payload = {
            "post": self.serialize_post(post),
            "comments": comments,
            "page": {
                "limit": normalized_limit,
                "has_more": has_more,
                "oldest_comment_id": oldest_comment_id,
                "before_comment_id": before_comment_id,
            },
            "viewer": self.build_viewer_payload_for_user(viewer, post=post),
        }
        payload.update(
            self.comment_read_payload(
                post_message_id=post["post_message_id"],
                viewer=viewer,
            )
        )
        return payload

    def create_comment_from_webapp(
        self,
        *,
        reference: str,
        text: str,
        init_data: str,
        photo: Any = None,
        reply_to_comment_id: Any = None,
    ) -> dict[str, Any]:
        clean_text = safe_text(text)
        if len(clean_text) > 4000:
            raise MaxApiError("Comment is too long")

        auth_user = self.authenticate_webapp_user(init_data)
        post = self.resolve_post_reference(reference)
        if post is None:
            raise MaxApiError("Post not found")
        if safe_text(post["status"] if "status" in post.keys() else "published") != "published":
            raise MaxApiError("Post not found")
        media_items = self.build_comment_media_from_webapp(photo)
        if not clean_text and not media_items:
            raise MaxApiError("Comment is empty")
        normalized_reply_to_comment_id = validate_positive_int(reply_to_comment_id, minimum=1, maximum=2_000_000_000)

        comment = self.submit_comment(
            post_message_id=post["post_message_id"],
            parent_comment_id=normalized_reply_to_comment_id,
            user_id=auth_user.user_id,
            display_name=auth_user.display_name,
            username=auth_user.username,
            text=clean_text,
            media=media_items,
            source_kind="webapp",
            source_message_id=None,
        )
        self.store.upsert_comment_read_state(
            user_id=auth_user.user_id,
            post_message_id=post["post_message_id"],
            last_read_comment_id=int(comment["id"]),
        )
        return {
            "ok": True,
            "comment": comment,
            "post": self.serialize_post(post),
        }

    def mark_comments_read_from_webapp(
        self,
        *,
        reference: str,
        last_read_comment_id: Any,
        init_data: str,
    ) -> dict[str, Any]:
        auth_user = self.authenticate_webapp_user(init_data)
        post = self.resolve_post_reference(reference)
        if post is None:
            raise MaxApiError("Post not found")
        if safe_text(post["status"] if "status" in post.keys() else "published") != "published":
            raise MaxApiError("Post not found")
        normalized_comment_id = validate_positive_int(
            last_read_comment_id,
            minimum=1,
            maximum=2_000_000_000,
        )
        if normalized_comment_id is None:
            raise MaxApiError("Comment not found")

        comment = self.store.get_comment(normalized_comment_id)
        if (
            comment is None
            or safe_text(comment["post_message_id"]) != safe_text(post["post_message_id"])
            or safe_text(comment["status"] if "status" in comment.keys() else COMMENT_STATUS_ACTIVE) != COMMENT_STATUS_ACTIVE
        ):
            raise MaxApiError("Comment not found")

        read_state = self.store.upsert_comment_read_state(
            user_id=auth_user.user_id,
            post_message_id=post["post_message_id"],
            last_read_comment_id=normalized_comment_id,
        )
        last_read_id = (
            int(read_state["last_read_comment_id"])
            if read_state["last_read_comment_id"] is not None
            else None
        )
        return {
            "ok": True,
            "readState": {
                "lastReadCommentId": last_read_id,
                "last_read_comment_id": last_read_id,
            },
        }

    def delete_comment_from_webapp(
        self,
        *,
        reference: str,
        comment_id: int,
        init_data: str,
    ) -> dict[str, Any]:
        auth_user = self.authenticate_webapp_user(init_data)
        post = self.resolve_post_reference(reference)
        if post is None:
            raise MaxApiError("Post not found")
        if safe_text(post["status"] if "status" in post.keys() else "published") != "published":
            raise MaxApiError("Post not found")
        comment = self.store.get_comment(comment_id)
        if comment is None or safe_text(comment["post_message_id"]) != safe_text(post["post_message_id"]):
            raise MaxApiError("Comment not found")
        if safe_text(comment["status"] if "status" in comment.keys() else COMMENT_STATUS_ACTIVE) != COMMENT_STATUS_ACTIVE:
            raise MaxApiError("Comment not found")
        admin_context = self.admin_context_for_user(auth_user.user_id)
        is_admin = False
        if admin_context is not None:
            try:
                self.require_admin_access_to_channel(admin_context, int(post["channel_chat_id"]))
                is_admin = True
            except MaxApiError:
                is_admin = False
        is_author = int(comment["user_id"]) == int(auth_user.user_id)
        if not is_admin and not is_author:
            raise MaxApiError("You can delete only your own comments")

        deleted = self.store.delete_comment(
            comment_id=comment_id,
            post_message_id=post["post_message_id"],
            deleted_by_user_id=auth_user.user_id,
            reason="Удалено пользователем",
        )
        if deleted is None:
            raise MaxApiError("Comment not found")
        self.delete_comment_media_files(
            deserialize_comment_media(deleted["media_json"])
        )
        warning = self.delete_comment_discussion_copy(deleted)

        comment_count = self.store.get_comment_count(post["post_message_id"])
        self.refresh_post_comment_button(
            post["post_message_id"],
            comment_count=comment_count,
        )

        return {
            "ok": True,
            "deleted_comment_id": comment_id,
            "post": self.serialize_post(self.store.get_post(post["post_message_id"]) or post),
            "warning": warning,
        }

    def delete_comment_discussion_copy(self, comment: sqlite3.Row) -> str | None:
        discussion_copy_message_id = safe_text(comment["discussion_copy_message_id"])
        if not discussion_copy_message_id:
            logger.info(
                "Comment %s has no discussion_copy_message_id; skipping mirror deletion",
                comment["id"],
            )
            return None

        try:
            self.api.delete_message(discussion_copy_message_id)
        except MaxApiError as exc:
            logger.warning(
                "Failed to delete mirrored discussion message %s for comment %s: %s",
                discussion_copy_message_id,
                comment["id"],
                exc,
            )
            if "24" in str(exc):
                return (
                    "Комментарий удалён из ленты. Копию в чате обсуждения уже нельзя удалить автоматически из-за ограничения MAX в 24 часа."
                )
            return (
                "Комментарий удалён из ленты. Копию в чате обсуждения автоматически удалить не удалось."
            )
        return None

    def update_comment_from_webapp(
        self,
        *,
        reference: str,
        comment_id: int,
        text: str,
        init_data: str,
    ) -> dict[str, Any]:
        auth_user = self.authenticate_webapp_user(init_data)
        post = self.resolve_post_reference(reference)
        if post is None:
            raise MaxApiError("Post not found")
        if safe_text(post["status"] if "status" in post.keys() else "published") != "published":
            raise MaxApiError("Post not found")

        comment = self.store.get_comment(comment_id)
        if comment is None or safe_text(comment["post_message_id"]) != safe_text(post["post_message_id"]):
            raise MaxApiError("Comment not found")
        if safe_text(comment["status"] if "status" in comment.keys() else COMMENT_STATUS_ACTIVE) != COMMENT_STATUS_ACTIVE:
            raise MaxApiError("Comment not found")
        if int(comment["user_id"]) != int(auth_user.user_id):
            raise MaxApiError("You can edit only your own comments")

        clean_text = safe_text(text)
        media_items = deserialize_comment_media(comment["media_json"])
        if len(clean_text) > 4000:
            raise MaxApiError("Comment is too long")
        if not clean_text and not media_items:
            raise MaxApiError("Comment is empty")
        ensure_comment_text_has_no_links(clean_text)
        ensure_comment_text_has_no_taboo(clean_text)

        updated = self.store.update_comment_text(
            comment_id=comment_id,
            post_message_id=post["post_message_id"],
            text=clean_text,
        )
        if updated is None:
            raise MaxApiError("Comment not found")

        comments_chat_id = self.resolve_post_comments_chat_id(post)
        if comments_chat_id is not None:
            self.send_comment_edit_notice(
                post_message_id=post["post_message_id"],
                comments_chat_id=comments_chat_id,
                display_name=auth_user.display_name,
                username=auth_user.username,
                text=clean_text,
                media_items=media_items,
                discussion_message_id=safe_text(post["discussion_message_id"]) or None,
            )

        refreshed_post = self.store.get_post(post["post_message_id"]) or post
        return {
            "ok": True,
            "comment": self.serialize_comment(updated),
            "post": self.serialize_post(refreshed_post),
        }

    def serialize_post(self, post: sqlite3.Row) -> dict[str, Any]:
        post_attachments = self.stored_post_attachments(post)
        return {
            "post_message_id": post["post_message_id"],
            "post_ref": post_ref_for_message_id(post["post_message_id"]),
            "channel_chat_id": int(post["channel_chat_id"]),
            "comments_chat_id": (
                int(post["comments_chat_id"])
                if post["comments_chat_id"] is not None
                else None
            ),
            "comment_code": comment_code_for_post(post["post_message_id"]),
            "post_title": safe_text(post["post_title"] if "post_title" in post.keys() else ""),
            "post_text": post["post_text"],
            "post_preview": snippet(post["post_text"], 220),
            "post_url": post["post_url"],
            "media": extract_post_media_from_attachments(post_attachments),
            "comment_count": int(post["comment_count"]),
            "status": safe_text(post["status"] if "status" in post.keys() else "published"),
            "created_at": post["created_at"],
            "updated_at": post["updated_at"],
            "discussion_message_id": post["discussion_message_id"],
            "webapp_url": self.direct_webapp_url(post["post_message_id"]),
        }

    def serialize_comment_author(self, comment: sqlite3.Row) -> dict[str, Any]:
        user_id = int(comment["user_id"])
        return {
            "id": user_id,
            "maxUserId": str(user_id),
            "displayName": safe_text(comment["display_name"]) or "Пользователь",
            "username": safe_text(comment["username"]) or None,
            "avatarUrl": None,
            "profileUrl": None,
        }

    def serialize_comment(
        self,
        comment: sqlite3.Row,
        *,
        comment_id_override: int | None = None,
        parent_comment: sqlite3.Row | None = None,
        reaction_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        reaction_payload = reaction_summary if isinstance(reaction_summary, dict) else {}
        normalized_my_reaction = (
            safe_text(reaction_payload.get("myReaction") or reaction_payload.get("my_reaction")) or None
        )
        if normalized_my_reaction not in ALLOWED_COMMENT_REACTION_SET:
            normalized_my_reaction = None
        reactions = [
            {
                "emoji": safe_text(item.get("emoji")),
                "count": int(item.get("count") or 0),
                "selected": bool(item.get("selected")),
            }
            for item in (reaction_payload.get("reactions") or [])
            if isinstance(item, dict)
            and safe_text(item.get("emoji")) in ALLOWED_COMMENT_REACTION_SET
            and int(item.get("count") or 0) > 0
        ]
        return {
            "id": comment_id_override or int(comment["id"]),
            "post_message_id": comment["post_message_id"],
            "parent_comment_id": int(comment["parent_comment_id"]) if comment["parent_comment_id"] is not None else None,
            "user_id": int(comment["user_id"]),
            "display_name": comment["display_name"],
            "username": comment["username"],
            "author": self.serialize_comment_author(comment),
            "text": comment["text"],
            "media": deserialize_comment_media(comment["media_json"]),
            "parent_comment": self.serialize_parent_comment(parent_comment),
            "source_kind": comment["source_kind"],
            "status": safe_text(comment["status"] if "status" in comment.keys() else COMMENT_STATUS_ACTIVE),
            "reactions": reactions,
            "myReaction": normalized_my_reaction,
            "my_reaction": normalized_my_reaction,
            "created_at": comment["created_at"],
        }

    def serialize_parent_comment(self, comment: sqlite3.Row | None) -> dict[str, Any] | None:
        if comment is None:
            return None
        is_deleted = safe_text(comment["status"] if "status" in comment.keys() else COMMENT_STATUS_ACTIVE) != COMMENT_STATUS_ACTIVE
        return {
            "id": int(comment["id"]),
            "user_id": int(comment["user_id"]),
            "display_name": comment["display_name"],
            "username": comment["username"],
            "author": self.serialize_comment_author(comment),
            "text": "Комментарий удалён модератором" if is_deleted else comment_reply_preview_text(comment),
            "has_media": False if is_deleted else bool(deserialize_comment_media(comment["media_json"])),
            "status": safe_text(comment["status"] if "status" in comment.keys() else COMMENT_STATUS_ACTIVE),
        }

    @staticmethod
    def user_display_name(user: dict[str, Any]) -> str:
        first_name = safe_text(user.get("first_name"))
        last_name = safe_text(user.get("last_name"))
        full_name = " ".join(part for part in [first_name, last_name] if part)
        if full_name:
            return full_name
        username = safe_text(user.get("username"))
        if username:
            return f"@{username}"
        return f"user:{user.get('user_id', 'unknown')}"

    def get_bot_username(self) -> str:
        if self.bot_info is None:
            self.bot_info = self.api.get_me()
        return safe_text(self.bot_info.get("username"))


class CommentWebServer:
    def __init__(self, app: MaxCommentsBot, host: str, port: int) -> None:
        self.app = app
        self.host = host
        self.port = port
        self.httpd = ThreadingHTTPServer((host, port), self._build_handler())
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        app = self.app

        class Handler(BaseHTTPRequestHandler):
            server_version = f"MaxCommentsWeb/{APP_VERSION}"

            def do_GET(self) -> None:
                parsed = parse.urlparse(self.path)
                path = parsed.path
                if path in {
                    "/super-admin",
                    "/super-admin/",
                    "/super-admin/index.html",
                    "/super-admin/login",
                    "/super-admin/dashboard",
                }:
                    self.serve_super_admin_static("index.html", "text/html; charset=utf-8")
                    return
                if path in {"/admin", "/admin/", "/admin/index.html"}:
                    self.serve_admin_static("index.html", "text/html; charset=utf-8")
                    return
                if path == "/admin/app.css":
                    self.serve_admin_static("app.css", "text/css; charset=utf-8")
                    return
                if path == "/admin/app.js":
                    self.serve_admin_static("app.js", "application/javascript; charset=utf-8")
                    return
                if path in {"/", "/index.html", "/webapp", "/webapp/"}:
                    self.serve_static("index.html", "text/html; charset=utf-8")
                    return
                if path == "/webapp/app.css":
                    self.serve_static("app.css", "text/css; charset=utf-8")
                    return
                if path == "/webapp/app.js":
                    self.serve_static("app.js", "application/javascript; charset=utf-8")
                    return
                if path.startswith("/media/comments/"):
                    self.serve_comment_media(path)
                    return
                if path == "/api/healthz":
                    self.send_json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "version": APP_VERSION,
                            "delivery_mode": app.delivery_mode,
                            "webhook_path": WEBHOOK_PATH,
                        },
                    )
                    return
                if path == "/api/admin/state":
                    context = self.require_admin_context()
                    if context is None:
                        return
                    try:
                        self.send_json(HTTPStatus.OK, app.admin_dashboard(context))
                    except MaxApiError as exc:
                        self.send_max_api_error(exc)
                    return
                if path == "/api/super-admin/state":
                    context = self.require_super_admin_context()
                    if context is None:
                        return
                    self.send_json(HTTPStatus.OK, app.get_admin_state())
                    return
                if path.startswith("/api/super-admin/"):
                    self.handle_super_admin_get(path, parse.parse_qs(parsed.query, keep_blank_values=True))
                    return
                if path.startswith("/api/admin/"):
                    self.handle_admin_get(path, parse.parse_qs(parsed.query, keep_blank_values=True))
                    return
                if path.startswith("/api/posts/"):
                    self.handle_api_get(path, parse.parse_qs(parsed.query, keep_blank_values=True))
                    return
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def do_HEAD(self) -> None:
                parsed = parse.urlparse(self.path)
                path = parsed.path
                if path in {
                    "/super-admin",
                    "/super-admin/",
                    "/super-admin/index.html",
                    "/super-admin/login",
                    "/super-admin/dashboard",
                }:
                    self.send_super_admin_static_headers("index.html", "text/html; charset=utf-8")
                    return
                if path in {"/admin", "/admin/", "/admin/index.html"}:
                    self.send_admin_static_headers("index.html", "text/html; charset=utf-8")
                    return
                if path == "/admin/app.css":
                    self.send_admin_static_headers("app.css", "text/css; charset=utf-8")
                    return
                if path == "/admin/app.js":
                    self.send_admin_static_headers("app.js", "application/javascript; charset=utf-8")
                    return
                if path in {"/", "/index.html", "/webapp", "/webapp/"}:
                    self.send_static_headers("index.html", "text/html; charset=utf-8")
                    return
                if path == "/webapp/app.css":
                    self.send_static_headers("app.css", "text/css; charset=utf-8")
                    return
                if path == "/webapp/app.js":
                    self.send_static_headers("app.js", "application/javascript; charset=utf-8")
                    return
                if path.startswith("/media/comments/"):
                    self.send_comment_media_headers(path)
                    return
                if path == "/api/healthz":
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("X-App-Version", APP_VERSION)
                    self.send_header("X-Delivery-Mode", app.delivery_mode)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(HTTPStatus.NOT_FOUND)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self) -> None:
                parsed = parse.urlparse(self.path)
                path = parsed.path
                if path == WEBHOOK_PATH:
                    self.handle_webhook()
                    return
                if path == "/api/super-admin/auth/login":
                    self.handle_super_admin_login()
                    return
                if path == "/api/super-admin/auth/logout":
                    self.handle_super_admin_logout()
                    return
                if path.startswith("/api/super-admin/"):
                    self.handle_super_admin_post(path)
                    return
                if path == "/admin/login":
                    self.handle_admin_login()
                    return
                if path == "/admin/logout":
                    self.handle_admin_logout()
                    return
                if path == "/api/admin/auth/login":
                    self.handle_admin_login()
                    return
                if path == "/api/admin/auth/logout":
                    self.handle_admin_logout()
                    return
                if path == "/api/admin/auth/change-password":
                    self.handle_admin_change_password()
                    return
                if path.startswith("/api/admin/"):
                    self.handle_admin_post(path)
                    return
                if path.startswith("/api/comments/") and path.endswith("/reaction"):
                    self.handle_comment_reaction(path)
                    return
                if path.startswith("/api/comments/") and path.endswith("/report"):
                    self.handle_report_comment(path)
                    return
                if path.startswith("/api/posts/") and path.endswith("/comments/read"):
                    self.handle_mark_comments_read(path)
                    return
                if path.startswith("/api/posts/") and path.endswith("/comments"):
                    self.handle_create_comment(path)
                    return
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def do_PATCH(self) -> None:
                parsed = parse.urlparse(self.path)
                path = parsed.path
                if path.startswith("/api/super-admin/"):
                    self.handle_super_admin_patch(path)
                    return
                if path.startswith("/api/admin/"):
                    self.handle_admin_patch(path)
                    return
                if path.startswith("/api/posts/") and "/comments/" in path:
                    self.handle_update_comment(path)
                    return
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def do_DELETE(self) -> None:
                parsed = parse.urlparse(self.path)
                path = parsed.path
                if path.startswith("/api/super-admin/"):
                    self.handle_super_admin_delete(path)
                    return
                if path.startswith("/api/admin/"):
                    self.handle_admin_delete(path)
                    return
                if path.startswith("/api/posts/") and "/comments/" in path:
                    self.handle_delete_comment(path)
                    return
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def serve_static(self, file_name: str, content_type: str) -> None:
                file_path = WEBAPP_DIR / file_name
                if not file_path.exists():
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Static file not found"})
                    return
                payload = file_path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.end_headers()
                self.wfile.write(payload)

            def serve_admin_static(self, file_name: str, content_type: str) -> None:
                file_path = ADMIN_DIR / file_name
                if not file_path.exists():
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Admin file not found"})
                    return
                payload = file_path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.end_headers()
                self.wfile.write(payload)

            def serve_super_admin_static(self, file_name: str, content_type: str) -> None:
                file_path = SUPER_ADMIN_DIR / file_name
                if not file_path.exists():
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Super admin file not found"})
                    return
                payload = file_path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.end_headers()
                self.wfile.write(payload)

            def resolve_comment_media_path(self, request_path: str) -> Path | None:
                prefix = "/media/comments/"
                relative_path = parse.unquote(request_path.removeprefix(prefix)).strip("/")
                if not relative_path:
                    return None
                candidate = (COMMENT_MEDIA_DIR / relative_path).resolve()
                try:
                    candidate.relative_to(COMMENT_MEDIA_DIR.resolve())
                except ValueError:
                    return None
                if not candidate.exists() or not candidate.is_file():
                    return None
                return candidate

            def serve_comment_media(self, request_path: str) -> None:
                file_path = self.resolve_comment_media_path(request_path)
                if file_path is None:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Media file not found"})
                    return
                payload = file_path.read_bytes()
                content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.end_headers()
                self.wfile.write(payload)

            def send_comment_media_headers(self, request_path: str) -> None:
                file_path = self.resolve_comment_media_path(request_path)
                if file_path is None:
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(file_path.stat().st_size))
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.end_headers()

            def send_static_headers(self, file_name: str, content_type: str) -> None:
                file_path = WEBAPP_DIR / file_name
                if not file_path.exists():
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                size = file_path.stat().st_size
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.end_headers()

            def send_admin_static_headers(self, file_name: str, content_type: str) -> None:
                file_path = ADMIN_DIR / file_name
                if not file_path.exists():
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                size = file_path.stat().st_size
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.end_headers()

            def send_super_admin_static_headers(self, file_name: str, content_type: str) -> None:
                file_path = SUPER_ADMIN_DIR / file_name
                if not file_path.exists():
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                size = file_path.stat().st_size
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.end_headers()

            def handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
                reference, suffix = self.extract_post_reference(path)
                if reference is None:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                    return
                try:
                    if suffix == "":
                        self.send_json(HTTPStatus.OK, app.get_post_payload(reference))
                        return
                    if suffix == "/comments":
                        before_values = query.get("before_id") or query.get("before_comment_id") or []
                        limit_values = query.get("limit") or []
                        before_comment_id = None
                        if before_values:
                            try:
                                before_comment_id = int(before_values[0])
                            except ValueError:
                                before_comment_id = None
                        limit = normalize_comments_page_limit(limit_values[0] if limit_values else None)
                        self.send_json(
                            HTTPStatus.OK,
                            app.get_comments_payload(
                                reference,
                                limit=limit,
                                before_comment_id=before_comment_id,
                                viewer_init_data=self.read_init_data_header(),
                            ),
                        )
                        return
                except WebAppAuthError as exc:
                    self.send_json(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
                    return
                except MaxApiError as exc:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": humanize_comment_error_message(str(exc))})
                    return
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def handle_create_comment(self, path: str) -> None:
                reference, suffix = self.extract_post_reference(path)
                if reference is None or suffix != "/comments":
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                    return

                raw_body = self.read_body()
                try:
                    payload = json.loads(raw_body.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON"})
                    return

                try:
                    result = app.create_comment_from_webapp(
                        reference=reference,
                        text=safe_text(payload.get("text")),
                        init_data=safe_text(payload.get("initData")),
                        photo=payload.get("photo"),
                        reply_to_comment_id=payload.get("reply_to_comment_id"),
                    )
                except WebAppAuthError as exc:
                    self.send_json(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
                    return
                except CommentBlockedError:
                    self.send_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "ok": False,
                            "error": COMMENT_BLOCKED_CODE,
                            "message": COMMENT_BLOCKED_MESSAGE,
                        },
                    )
                    return
                except MaxApiError as exc:
                    status = HTTPStatus.BAD_REQUEST
                    if "not found" in str(exc).lower():
                        status = HTTPStatus.NOT_FOUND
                    self.send_json(status, {"error": humanize_comment_error_message(str(exc))})
                    return

                self.send_json(HTTPStatus.CREATED, result)

            def handle_mark_comments_read(self, path: str) -> None:
                reference, suffix = self.extract_post_reference(path)
                if reference is None or suffix != "/comments/read":
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                    return

                payload = self.read_json_body()
                if payload is None:
                    return

                try:
                    result = app.mark_comments_read_from_webapp(
                        reference=reference,
                        last_read_comment_id=payload.get("lastReadCommentId")
                        if "lastReadCommentId" in payload
                        else payload.get("last_read_comment_id"),
                        init_data=safe_text(payload.get("initData")) or self.read_init_data_header(),
                    )
                except WebAppAuthError as exc:
                    self.send_json(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
                    return
                except MaxApiError as exc:
                    status = HTTPStatus.BAD_REQUEST
                    if "not found" in str(exc).lower():
                        status = HTTPStatus.NOT_FOUND
                    self.send_json(status, {"error": humanize_comment_error_message(str(exc))})
                    return

                self.send_json(HTTPStatus.OK, result)

            def handle_webhook(self) -> None:
                if app.delivery_mode != "webhook":
                    self.send_json(HTTPStatus.CONFLICT, {"error": "Webhook mode is disabled"})
                    return
                if not self.verify_webhook_secret():
                    self.send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid webhook secret"})
                    return
                raw_body = self.read_body()
                try:
                    payload = json.loads(raw_body.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON"})
                    return

                updates = payload if isinstance(payload, list) else [payload]
                accepted = 0
                for update in updates:
                    if not isinstance(update, dict):
                        continue
                    app.enqueue_update(update)
                    accepted += 1
                self.send_json(HTTPStatus.OK, {"ok": True, "accepted": accepted})

            def handle_admin_login(self) -> None:
                payload = self.read_json_body()
                if payload is None:
                    return
                try:
                    context = app.authenticate_admin_credentials(
                        login=safe_text(payload.get("login")),
                        password=safe_text(payload.get("password")),
                    )
                except MaxApiError as exc:
                    code = safe_text(str(exc))
                    if code == "ACCESS_DENIED":
                        self.send_json(
                            HTTPStatus.FORBIDDEN,
                            {
                                "ok": False,
                                "error": "ACCESS_DENIED",
                                "message": ADMIN_LOGIN_ACCESS_DENIED_MESSAGE,
                            },
                        )
                        return
                    self.send_json(
                        HTTPStatus.UNAUTHORIZED,
                        {
                            "ok": False,
                            "error": "INVALID_CREDENTIALS",
                            "message": INVALID_CREDENTIALS_MESSAGE,
                        },
                    )
                    return
                session_token = app.create_admin_session_token(context.user_id)
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "admin": app.serialize_admin_identity(context),
                    },
                    headers={"Set-Cookie": self.admin_session_cookie(session_token)},
                )

            def handle_super_admin_login(self) -> None:
                payload = self.read_json_body()
                if payload is None:
                    return
                try:
                    context = app.authenticate_super_admin_credentials(
                        login=safe_text(payload.get("login")),
                        password=safe_text(payload.get("password")),
                    )
                except MaxApiError as exc:
                    code = safe_text(str(exc))
                    if code == "SUPER_ADMIN_NOT_CONFIGURED":
                        self.send_json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {
                                "ok": False,
                                "error": "SUPER_ADMIN_NOT_CONFIGURED",
                                "message": SUPER_ADMIN_NOT_CONFIGURED_MESSAGE,
                            },
                        )
                        return
                    self.send_json(
                        HTTPStatus.UNAUTHORIZED,
                        {
                            "ok": False,
                            "error": "INVALID_CREDENTIALS",
                            "message": INVALID_CREDENTIALS_MESSAGE,
                        },
                    )
                    return
                session_token = app.create_super_admin_session_token()
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "admin": app.serialize_admin_identity(context),
                    },
                    headers={"Set-Cookie": self.super_admin_session_cookie(session_token)},
                )

            def handle_admin_logout(self) -> None:
                self.send_json(
                    HTTPStatus.OK,
                    {"ok": True},
                    headers={"Set-Cookie": self.admin_session_cookie("", max_age=0)},
                )

            def handle_super_admin_logout(self) -> None:
                self.send_json(
                    HTTPStatus.OK,
                    {"ok": True},
                    headers={"Set-Cookie": self.super_admin_session_cookie("", max_age=0)},
                )

            def handle_admin_change_password(self) -> None:
                context = self.require_admin_context()
                if context is None:
                    return
                payload = self.read_json_body()
                if payload is None:
                    return
                try:
                    result = app.change_admin_password(
                        context,
                        old_password=safe_text(payload.get("oldPassword")),
                        new_password=safe_text(payload.get("newPassword")),
                    )
                except MaxApiError as exc:
                    code = safe_text(str(exc))
                    status = HTTPStatus.BAD_REQUEST
                    if code == "ACCESS_DENIED":
                        status = HTTPStatus.FORBIDDEN
                    elif code == "INVALID_CREDENTIALS":
                        status = HTTPStatus.UNAUTHORIZED
                    self.send_json(
                        status,
                        {
                            "ok": False,
                            "error": code or "CHANGE_PASSWORD_FAILED",
                            "message": humanize_comment_error_message(code),
                        },
                    )
                    return
                self.send_json(HTTPStatus.OK, result)

            def admin_context_from_init_data(self) -> AdminContext | None:
                init_data = self.read_init_data_header()
                if not init_data:
                    return None
                try:
                    user = app.authenticate_webapp_user(init_data)
                except WebAppAuthError:
                    return None
                return app.admin_context_for_user(user.user_id)

            def admin_context_from_session(self) -> AdminContext | None:
                return app.admin_context_from_session_token(self.admin_auth_token())

            def super_admin_context_from_session(self) -> AdminContext | None:
                return app.super_admin_context_from_session_token(self.super_admin_auth_token())

            def require_admin_context(self) -> AdminContext | None:
                context = self.admin_context_from_session()
                if context is not None and context.role == ROLE_CHANNEL_ADMIN:
                    return context
                if context is not None:
                    self.send_json(
                        HTTPStatus.UNAUTHORIZED,
                        {
                            "ok": False,
                            "error": "WRONG_ADMIN_PANEL",
                            "message": "Войдите как администратор канала.",
                        },
                        headers={"Set-Cookie": self.admin_session_cookie("", max_age=0)},
                    )
                    return None
                self.send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Unauthorized"})
                return None

            def require_super_admin_context(self) -> AdminContext | None:
                context = self.super_admin_context_from_session()
                if context is not None and context.is_super_admin:
                    return context
                self.send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Unauthorized"})
                return None

            def query_int(self, query: dict[str, list[str]], key: str) -> int | None:
                values = query.get(key) or []
                if not values or not safe_text(values[0]):
                    return None
                return int(values[0])

            def admin_error_status(self, exc: MaxApiError) -> HTTPStatus:
                raw_message = safe_text(str(exc))
                message = raw_message.lower()
                if raw_message in {"FORBIDDEN", "ACCESS_DENIED"} or "access denied" in message:
                    return HTTPStatus.FORBIDDEN
                if "not found" in message or "not connected" in message:
                    return HTTPStatus.NOT_FOUND
                return HTTPStatus.BAD_REQUEST

            def send_max_api_error(self, exc: MaxApiError) -> None:
                self.send_json(
                    self.admin_error_status(exc),
                    {
                        "ok": False,
                        "error": safe_text(str(exc)),
                        "message": humanize_comment_error_message(str(exc)),
                    },
                )

            def super_admin_legacy_path(self, path: str) -> str:
                return path.replace("/api/super-admin", "/api/admin", 1)

            def handle_super_admin_get(self, path: str, query: dict[str, list[str]]) -> None:
                context = self.require_super_admin_context()
                if context is None:
                    return
                try:
                    if path == "/api/super-admin/state":
                        self.send_json(HTTPStatus.OK, app.get_admin_state())
                        return
                    if path == "/api/super-admin/channel-requests":
                        status = safe_text((query.get("status") or [""])[0]) or None
                        limit = int((query.get("limit") or ["100"])[0] or "100")
                        offset = int((query.get("offset") or ["0"])[0] or "0")
                        self.send_json(
                            HTTPStatus.OK,
                            app.admin_list_channel_requests(
                                context,
                                status=status,
                                limit=limit,
                                offset=offset,
                            ),
                        )
                        return
                    if path.startswith("/api/super-admin/channel-requests/"):
                        request_id = int(parse.unquote(path.removeprefix("/api/super-admin/channel-requests/")).split("/", 1)[0])
                        self.send_json(HTTPStatus.OK, app.admin_get_channel_request(context, request_id=request_id))
                        return
                except ValueError:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "INVALID_ID", "message": "Некорректный ID"})
                    return
                except MaxApiError as exc:
                    self.send_max_api_error(exc)
                    return
                self.handle_admin_get(self.super_admin_legacy_path(path), query, context=context)

            def handle_super_admin_post(self, path: str) -> None:
                context = self.require_super_admin_context()
                if context is None:
                    return
                if path.startswith("/api/super-admin/channel-requests/"):
                    payload = self.read_json_body()
                    if payload is None:
                        return
                    try:
                        tail = parse.unquote(path.removeprefix("/api/super-admin/channel-requests/")).strip("/")
                        raw_request_id, action = tail.split("/", 1)
                        request_id = int(raw_request_id)
                        if action == "approve":
                            self.send_json(
                                HTTPStatus.OK,
                                app.admin_approve_channel_request(
                                    context,
                                    request_id=request_id,
                                    admin_comment=safe_text(payload.get("adminComment")),
                                ),
                            )
                            return
                        if action == "reject":
                            self.send_json(
                                HTTPStatus.OK,
                                app.admin_reject_channel_request(
                                    context,
                                    request_id=request_id,
                                    admin_comment=safe_text(payload.get("adminComment")),
                                ),
                            )
                            return
                    except ValueError:
                        self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "INVALID_ID", "message": "Некорректный ID"})
                        return
                    except MaxApiError as exc:
                        self.send_max_api_error(exc)
                        return
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                    return
                self.handle_admin_post(self.super_admin_legacy_path(path), context=context)

            def handle_super_admin_patch(self, path: str) -> None:
                context = self.require_super_admin_context()
                if context is None:
                    return
                self.handle_admin_patch(self.super_admin_legacy_path(path), context=context)

            def handle_super_admin_delete(self, path: str) -> None:
                context = self.require_super_admin_context()
                if context is None:
                    return
                self.handle_admin_delete(self.super_admin_legacy_path(path), context=context)

            def handle_admin_get(
                self,
                path: str,
                query: dict[str, list[str]],
                context: AdminContext | None = None,
            ) -> None:
                context = context or self.require_admin_context()
                if context is None:
                    return
                try:
                    channel_id = self.query_int(query, "channel_id")
                    if path == "/api/admin/dashboard":
                        self.send_json(HTTPStatus.OK, app.admin_dashboard(context, requested_channel_id=channel_id))
                        return
                    if path == "/api/admin/posts":
                        self.send_json(HTTPStatus.OK, app.admin_list_posts(context, requested_channel_id=channel_id))
                        return
                    if path.startswith("/api/admin/posts/"):
                        tail = parse.unquote(path.removeprefix("/api/admin/posts/"))
                        if tail.endswith("/comments"):
                            post_reference = tail[: -len("/comments")]
                            status = safe_text((query.get("status") or [""])[0]) or None
                            self.send_json(
                                HTTPStatus.OK,
                                app.admin_list_comments(
                                    context,
                                    post_reference=post_reference,
                                    status=status,
                                ),
                            )
                            return
                        self.send_json(HTTPStatus.OK, app.admin_get_post(context, post_reference=tail))
                        return
                    if path == "/api/admin/comments":
                        status = safe_text((query.get("status") or [""])[0]) or None
                        post_reference = safe_text((query.get("post_id") or [""])[0]) or None
                        self.send_json(
                            HTTPStatus.OK,
                            app.admin_list_comments(
                                context,
                                requested_channel_id=channel_id,
                                post_reference=post_reference,
                                status=status,
                            ),
                        )
                        return
                    if path == "/api/admin/reports":
                        status = safe_text((query.get("status") or [""])[0]) or None
                        self.send_json(
                            HTTPStatus.OK,
                            app.admin_list_reports(
                                context,
                                requested_channel_id=channel_id,
                                status=status,
                            ),
                        )
                        return
                    if path.startswith("/api/admin/reports/"):
                        report_id = int(parse.unquote(path.removeprefix("/api/admin/reports/")).split("/", 1)[0])
                        self.send_json(HTTPStatus.OK, app.admin_get_report(context, report_id=report_id))
                        return
                    if path == "/api/admin/users":
                        self.send_json(HTTPStatus.OK, app.admin_list_users(context))
                        return
                except ValueError:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "INVALID_ID", "message": "Некорректный ID"})
                    return
                except MaxApiError as exc:
                    self.send_max_api_error(exc)
                    return
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def handle_admin_post(self, path: str, context: AdminContext | None = None) -> None:
                context = context or self.require_admin_context()
                if context is None:
                    return
                payload = self.read_json_body()
                if payload is None:
                    return
                try:
                    if path == "/api/admin/posts":
                        channel_id = int(payload.get("channel_id")) if safe_text(payload.get("channel_id")) else None
                        result = app.admin_create_post(
                            context,
                            requested_channel_id=channel_id,
                            title=safe_text(payload.get("title")),
                            content=safe_text(payload.get("content")),
                        )
                        self.send_json(HTTPStatus.CREATED, result)
                        return
                    if path == "/api/admin/users":
                        channel_ids = app.normalize_admin_channel_ids(payload.get("channel_ids"))
                        result = app.admin_save_user(
                            context,
                            max_user_id=safe_text(payload.get("max_user_id")),
                            role=safe_text(payload.get("role")),
                            channel_ids=channel_ids,
                            is_active=bool(payload.get("is_active", True)),
                        )
                        self.send_json(HTTPStatus.CREATED, result)
                        return
                    if path.startswith("/api/admin/users/") and path.endswith("/reset-password"):
                        raw_admin_user_id = path.removeprefix("/api/admin/users/").removesuffix("/reset-password").strip("/")
                        admin_user_id = int(parse.unquote(raw_admin_user_id))
                        self.send_json(
                            HTTPStatus.OK,
                            app.admin_reset_user_password(context, admin_user_id=admin_user_id),
                        )
                        return
                    if path == "/api/admin/channels":
                        if not context.is_super_admin:
                            raise MaxApiError("Access denied")
                        channel_chat_id = int(payload.get("channel_chat_id"))
                        comments_chat_id = int(payload.get("comments_chat_id"))
                        result = app.admin_add_channel_binding(
                            channel_chat_id=channel_chat_id,
                            comments_chat_id=comments_chat_id,
                            comments_chat_url=safe_text(payload.get("comments_chat_url")) or None,
                            sync_now=bool(payload.get("sync_now", True)),
                        )
                        self.send_json(HTTPStatus.OK, result)
                        return
                    if path == "/api/admin/publish":
                        channel_chat_id = int(payload.get("channel_chat_id")) if safe_text(payload.get("channel_chat_id")) else None
                        text = safe_text(payload.get("text"))
                        if not text:
                            raise MaxApiError("Post text is empty")
                        channel_ids = app.resolve_admin_channel_ids(context, requested_channel_id=channel_chat_id)
                        channel_chat_id = next(iter(channel_ids))
                        result = app.publish_post(
                            text,
                            admin_user_id=None,
                            channel_chat_id=channel_chat_id,
                        )
                        self.send_json(HTTPStatus.CREATED, result)
                        return
                    if path == "/api/admin/attach":
                        if not context.is_super_admin:
                            raise MaxApiError("Access denied")
                        post_message_id = safe_text(payload.get("post_message_id"))
                        if not post_message_id:
                            raise MaxApiError("Post message id is empty")
                        result = app.attach_existing_post(post_message_id, admin_user_id=None)
                        self.send_json(HTTPStatus.OK, result)
                        return
                    if path == "/api/admin/sync":
                        if not context.is_super_admin:
                            raise MaxApiError("Access denied")
                        self.send_json(HTTPStatus.OK, app.admin_sync_recent_channel_posts())
                        return
                except (TypeError, ValueError):
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Проверьте числовые ID"})
                    return
                except MaxApiError as exc:
                    self.send_max_api_error(exc)
                    return
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def handle_admin_patch(self, path: str, context: AdminContext | None = None) -> None:
                context = context or self.require_admin_context()
                if context is None:
                    return
                payload = self.read_json_body()
                if payload is None:
                    return
                try:
                    if path.startswith("/api/admin/users/"):
                        raw_admin_user_id = path.removeprefix("/api/admin/users/").strip("/")
                        if "/" in raw_admin_user_id:
                            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                            return
                        admin_user_id = int(parse.unquote(raw_admin_user_id))
                        channel_ids = (
                            app.normalize_admin_channel_ids(payload.get("channel_ids"))
                            if "channel_ids" in payload
                            else None
                        )
                        result = app.admin_update_user(
                            context,
                            admin_user_id=admin_user_id,
                            role=safe_text(payload.get("role")) or None,
                            channel_ids=channel_ids,
                            is_active=bool(payload.get("is_active")) if "is_active" in payload else None,
                        )
                        self.send_json(HTTPStatus.OK, result)
                        return
                    if path.startswith("/api/admin/comments/") and path.endswith("/status"):
                        raw_comment_id = path.removeprefix("/api/admin/comments/").removesuffix("/status")
                        comment_id = int(parse.unquote(raw_comment_id).strip("/"))
                        result = app.admin_set_comment_status(
                            context,
                            comment_id=comment_id,
                            status=safe_text(payload.get("status")),
                            reason=safe_text(payload.get("reason")),
                        )
                        self.send_json(HTTPStatus.OK, result)
                        return
                    if path.startswith("/api/admin/reports/"):
                        report_id = int(parse.unquote(path.removeprefix("/api/admin/reports/")).split("/", 1)[0])
                        result = app.admin_update_report(
                            context,
                            report_id=report_id,
                            status=safe_text(payload.get("status")),
                            action=safe_text(payload.get("action")) or None,
                            admin_comment=safe_text(payload.get("adminComment")),
                        )
                        self.send_json(HTTPStatus.OK, result)
                        return
                except ValueError:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "INVALID_ID", "message": "Некорректный ID"})
                    return
                except MaxApiError as exc:
                    self.send_max_api_error(exc)
                    return
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def handle_admin_delete(self, path: str, context: AdminContext | None = None) -> None:
                context = context or self.require_admin_context()
                if context is None:
                    return
                if path.startswith("/api/admin/posts/"):
                    post_reference = parse.unquote(path.removeprefix("/api/admin/posts/")).split("/", 1)[0]
                    try:
                        result = app.admin_delete_post(
                            context,
                            post_reference=post_reference,
                            reason="Удалено администратором",
                        )
                    except MaxApiError as exc:
                        self.send_max_api_error(exc)
                        return
                    self.send_json(HTTPStatus.OK, result)
                    return
                if path.startswith("/api/admin/users/") and "/channels/" in path:
                    try:
                        tail = path.removeprefix("/api/admin/users/")
                        raw_admin_user_id, raw_channel_tail = tail.split("/channels/", 1)
                        admin_user_id = int(parse.unquote(raw_admin_user_id))
                        channel_id = int(parse.unquote(raw_channel_tail).split("/", 1)[0])
                        result = app.admin_delete_user_channel(
                            context,
                            admin_user_id=admin_user_id,
                            channel_id=channel_id,
                        )
                    except ValueError:
                        self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "INVALID_ID", "message": "Некорректный ID"})
                        return
                    except MaxApiError as exc:
                        self.send_max_api_error(exc)
                        return
                    self.send_json(HTTPStatus.OK, result)
                    return
                prefix = "/api/admin/channels/"
                if not path.startswith(prefix):
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                    return
                if not context.is_super_admin:
                    self.send_json(
                        HTTPStatus.FORBIDDEN,
                        {"ok": False, "error": "ACCESS_DENIED", "message": ACCESS_DENIED_MESSAGE},
                    )
                    return
                raw_channel_chat_id = parse.unquote(path.removeprefix(prefix)).split("/", 1)[0]
                try:
                    channel_chat_id = int(raw_channel_chat_id)
                    result = app.admin_remove_channel_binding(channel_chat_id=channel_chat_id)
                except ValueError:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "CHANNEL_ID должен быть числом"})
                    return
                except MaxApiError as exc:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": humanize_comment_error_message(str(exc))})
                    return
                self.send_json(HTTPStatus.OK, result)

            def handle_report_comment(self, path: str) -> None:
                raw_comment_id = path.removeprefix("/api/comments/").removesuffix("/report").strip("/")
                try:
                    comment_id = int(parse.unquote(raw_comment_id))
                except ValueError:
                    self.send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "INVALID_COMMENT_ID", "message": COMMENT_NOT_FOUND_MESSAGE},
                    )
                    return
                payload = self.read_json_body()
                if payload is None:
                    return
                try:
                    result = app.report_comment_from_webapp(
                        comment_id=comment_id,
                        init_data=safe_text(payload.get("initData")) or self.read_init_data_header(),
                        reason=safe_text(payload.get("reason")),
                        details=safe_text(payload.get("details")),
                    )
                except WebAppAuthError as exc:
                    self.send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "UNAUTHORIZED", "message": str(exc)})
                    return
                except MaxApiError as exc:
                    error_code = safe_text(str(exc))
                    if error_code == "Report already exists":
                        self.send_json(
                            HTTPStatus.CONFLICT,
                            {
                                "ok": False,
                                "error": "REPORT_ALREADY_EXISTS",
                                "message": REPORT_ALREADY_EXISTS_MESSAGE,
                            },
                        )
                        return
                    if error_code == "Comment not found":
                        self.send_json(
                            HTTPStatus.NOT_FOUND,
                            {
                                "ok": False,
                                "error": "COMMENT_NOT_FOUND",
                                "message": COMMENT_NOT_FOUND_MESSAGE,
                            },
                        )
                        return
                    self.send_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "ok": False,
                            "error": error_code or "REPORT_ERROR",
                            "message": humanize_comment_error_message(error_code),
                        },
                    )
                    return
                self.send_json(HTTPStatus.CREATED, result)

            def handle_comment_reaction(self, path: str) -> None:
                raw_comment_id = path.removeprefix("/api/comments/").removesuffix("/reaction").strip("/")
                try:
                    comment_id = int(parse.unquote(raw_comment_id))
                except ValueError:
                    self.send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "INVALID_COMMENT_ID", "message": COMMENT_NOT_FOUND_MESSAGE},
                    )
                    return
                payload = self.read_json_body()
                if payload is None:
                    return
                try:
                    result = app.set_comment_reaction_from_webapp(
                        comment_id=comment_id,
                        init_data=safe_text(payload.get("initData")) or self.read_init_data_header(),
                        emoji=safe_text(payload.get("emoji")),
                    )
                except WebAppAuthError as exc:
                    self.send_json(
                        HTTPStatus.UNAUTHORIZED,
                        {"ok": False, "error": "UNAUTHORIZED", "message": str(exc)},
                    )
                    return
                except MaxApiError as exc:
                    error_code = safe_text(str(exc))
                    if error_code == "INVALID_REACTION":
                        self.send_json(
                            HTTPStatus.BAD_REQUEST,
                            {
                                "ok": False,
                                "error": "INVALID_REACTION",
                                "message": INVALID_REACTION_MESSAGE,
                            },
                        )
                        return
                    if error_code == "Comment not found":
                        self.send_json(
                            HTTPStatus.NOT_FOUND,
                            {
                                "ok": False,
                                "error": "COMMENT_NOT_FOUND",
                                "message": COMMENT_NOT_FOUND_MESSAGE,
                            },
                        )
                        return
                    self.send_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "ok": False,
                            "error": error_code or "REACTION_ERROR",
                            "message": humanize_comment_error_message(error_code),
                        },
                    )
                    return
                self.send_json(HTTPStatus.OK, result)

            def handle_delete_comment(self, path: str) -> None:
                reference, suffix = self.extract_post_reference(path)
                if reference is None or not suffix.startswith("/comments/"):
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                    return

                raw_comment_id = suffix.removeprefix("/comments/").split("/", 1)[0]
                try:
                    comment_id = int(raw_comment_id)
                except ValueError:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid comment id"})
                    return

                try:
                    result = app.delete_comment_from_webapp(
                        reference=reference,
                        comment_id=comment_id,
                        init_data=self.read_init_data_header(),
                    )
                except WebAppAuthError as exc:
                    self.send_json(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
                    return
                except CommentBlockedError:
                    self.send_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "ok": False,
                            "error": COMMENT_BLOCKED_CODE,
                            "message": COMMENT_BLOCKED_MESSAGE,
                        },
                    )
                    return
                except MaxApiError as exc:
                    status = HTTPStatus.BAD_REQUEST
                    message = str(exc).lower()
                    if "not found" in message:
                        status = HTTPStatus.NOT_FOUND
                    elif "only admins" in message or "only your own" in message:
                        status = HTTPStatus.FORBIDDEN
                    self.send_json(status, {"error": humanize_comment_error_message(str(exc))})
                    return

                self.send_json(HTTPStatus.OK, result)

            def handle_update_comment(self, path: str) -> None:
                reference, suffix = self.extract_post_reference(path)
                if reference is None or not suffix.startswith("/comments/"):
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                    return

                raw_comment_id = suffix.removeprefix("/comments/").split("/", 1)[0]
                try:
                    comment_id = int(raw_comment_id)
                except ValueError:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid comment id"})
                    return

                raw_body = self.read_body()
                try:
                    payload = json.loads(raw_body.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON"})
                    return

                try:
                    result = app.update_comment_from_webapp(
                        reference=reference,
                        comment_id=comment_id,
                        text=safe_text(payload.get("text")),
                        init_data=safe_text(payload.get("initData")) or self.read_init_data_header(),
                    )
                except WebAppAuthError as exc:
                    self.send_json(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
                    return
                except MaxApiError as exc:
                    status = HTTPStatus.BAD_REQUEST
                    message = str(exc).lower()
                    if "not found" in message:
                        status = HTTPStatus.NOT_FOUND
                    elif "only your own" in message:
                        status = HTTPStatus.FORBIDDEN
                    self.send_json(status, {"error": humanize_comment_error_message(str(exc))})
                    return

                self.send_json(HTTPStatus.OK, result)

            def extract_post_reference(self, path: str) -> tuple[str | None, str]:
                prefix = "/api/posts/"
                if not path.startswith(prefix):
                    return None, ""
                tail = path[len(prefix):]
                if "/" in tail:
                    reference, suffix = tail.split("/", 1)
                    return parse.unquote(reference), f"/{suffix}"
                return parse.unquote(tail), ""

            def read_body(self) -> bytes:
                content_length = int(self.headers.get("Content-Length", "0") or "0")
                return self.rfile.read(content_length)

            def read_json_body(self) -> dict[str, Any] | None:
                raw_body = self.read_body()
                try:
                    payload = json.loads(raw_body.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON"})
                    return None
                if not isinstance(payload, dict):
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON"})
                    return None
                return payload

            def read_init_data_header(self) -> str:
                return safe_text(
                    self.headers.get("X-Max-Init-Data")
                    or self.headers.get("x-max-init-data")
                    or ""
                )

            def verify_webhook_secret(self) -> bool:
                expected = app.webhook_secret()
                if not expected:
                    return True
                provided = safe_text(
                    self.headers.get("X-Max-Bot-Api-Secret")
                    or self.headers.get("x-max-bot-api-secret")
                    or ""
                )
                return hmac.compare_digest(provided, expected)

            def session_cookie(
                self,
                name: str,
                value: str,
                *,
                max_age: int = ADMIN_SESSION_MAX_AGE_SECONDS,
            ) -> str:
                encoded_value = parse.quote(value, safe="")
                secure = "; Secure" if WEB_APP_PUBLIC_URL.startswith("https://") else ""
                return (
                    f"{name}={encoded_value}; Path=/; Max-Age={int(max_age)}; "
                    f"HttpOnly; SameSite=Lax{secure}"
                )

            def admin_session_cookie(self, value: str, *, max_age: int = ADMIN_SESSION_MAX_AGE_SECONDS) -> str:
                return self.session_cookie(ADMIN_SESSION_COOKIE, value, max_age=max_age)

            def super_admin_session_cookie(self, value: str, *, max_age: int = ADMIN_SESSION_MAX_AGE_SECONDS) -> str:
                return self.session_cookie(SUPER_ADMIN_SESSION_COOKIE, value, max_age=max_age)

            def auth_token(self, cookie_name: str) -> str:
                auth_header = safe_text(self.headers.get("Authorization") or self.headers.get("authorization"))
                if auth_header.lower().startswith("bearer "):
                    return auth_header[7:].strip()
                cookie_header = safe_text(self.headers.get("Cookie") or self.headers.get("cookie"))
                for raw_item in cookie_header.split(";"):
                    name, separator, value = raw_item.strip().partition("=")
                    if separator and name == cookie_name:
                        return parse.unquote(value)
                return ""

            def admin_auth_token(self) -> str:
                return self.auth_token(ADMIN_SESSION_COOKIE)

            def super_admin_auth_token(self) -> str:
                return self.auth_token(SUPER_ADMIN_SESSION_COOKIE)

            def is_admin_authenticated(self) -> bool:
                return app.admin_context_from_session_token(self.admin_auth_token()) is not None

            def require_admin(self) -> bool:
                return self.require_admin_context() is not None

            def send_json(
                self,
                status: HTTPStatus,
                payload: dict[str, Any],
                *,
                headers: dict[str, str] | None = None,
            ) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                for header_name, header_value in (headers or {}).items():
                    self.send_header(header_name, header_value)
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format: str, *args: Any) -> None:
                logger.info("web: " + format, *args)

        return Handler


if __name__ == "__main__":
    MaxCommentsBot().run()
