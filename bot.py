from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import queue
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from config import (
    ADMIN_PANEL_TOKEN,
    ADMIN_USER_IDS,
    BOT_TOKEN,
    CHANNEL_SYNC_INTERVAL_SECONDS,
    COMMENTS_CHAT_ID,
    COMMENTS_CHAT_URL,
    DATABASE_PATH,
    DELIVERY_MODE,
    MAX_API_BASE_URL,
    POLL_LIMIT,
    POLL_TIMEOUT_SECONDS,
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
WEBAPP_DIR = APP_ROOT_DIR / "webapp"
ADMIN_DIR = APP_ROOT_DIR / "admin"
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
ADMIN_SESSION_MAX_AGE_SECONDS = 24 * 60 * 60


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
        and bool(ADMIN_USER_IDS)
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


def humanize_comment_error_message(message: str) -> str:
    normalized = safe_text(message)
    if not normalized:
        return "Не удалось обработать комментарий."
    return {
        "Post not found": "Пост для комментария больше не найден. Попробуйте открыть обсуждение заново.",
        "Parent comment not found": "Комментарий, на который вы отвечаете, больше не найден.",
        "Comment not found": "Комментарий не найден.",
        "Comment is empty": "Комментарий пустой. Напишите текст или прикрепите фото.",
        "Comment is too long": "Комментарий слишком длинный. Максимум 4000 символов.",
        "Links are not allowed in comments": "Ссылки запрещены правилами сервиса.",
        "Channel is not connected": "Этот канал не подключён к боту. Добавьте его через `/channel_add CHANNEL_ID COMMENTS_CHAT_ID`.",
        "Comments chat is not configured for this post": "Для этого поста не найден чат комментариев.",
        "You can edit only your own comments": "Можно редактировать только свои комментарии.",
        "You can delete only your own comments": "Можно удалять только свои комментарии.",
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


def comment_button_text(comment_count: int) -> str:
    return f"💬 {max(comment_count, 0)}"


def max_share_url(text: str) -> str:
    return f"https://max.ru/:share?text={parse.quote(safe_text(text))}"


CHANNEL_POST_FOOTER = "Комментарии к этому посту открываются в мини-приложении по кнопке ниже."
COMMENTS_PAGE_SIZE_DEFAULT = 40
COMMENTS_PAGE_SIZE_MAX = 100


def strip_managed_channel_footer(post_text: str) -> str:
    clean_text = safe_text(post_text)
    footer_marker = f"\n\n{CHANNEL_POST_FOOTER}"
    if footer_marker in clean_text:
        return clean_text.split(footer_marker, 1)[0].rstrip()
    return clean_text


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
class AuthenticatedWebAppUser:
    user_id: int
    display_name: str
    username: str | None
    platform: str
    chat_id: int | None
    chat_type: str | None


class MaxApiError(RuntimeError):
    pass


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
                    post_url TEXT,
                    post_text TEXT,
                    post_attachments_json TEXT NOT NULL DEFAULT '[]',
                    discussion_message_id TEXT,
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
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(post_message_id) REFERENCES posts(post_message_id)
                );

                CREATE TABLE IF NOT EXISTS pending_comments (
                    user_id INTEGER PRIMARY KEY,
                    post_message_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(post_message_id) REFERENCES posts(post_message_id)
                );

                CREATE TABLE IF NOT EXISTS pending_channel_bindings (
                    bind_code TEXT PRIMARY KEY,
                    requested_by_user_id INTEGER NOT NULL,
                    channel_chat_id INTEGER NOT NULL,
                    channel_title TEXT,
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
        post_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(posts)").fetchall()
        }
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

    def upsert_post(
        self,
        *,
        post_message_id: str,
        channel_chat_id: int,
        comments_chat_id: int | None,
        post_url: str | None,
        post_text: str,
        post_attachments: list[dict[str, Any]] | None = None,
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
                    post_url,
                    post_text,
                    post_attachments_json,
                    discussion_message_id,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_message_id) DO UPDATE SET
                    channel_chat_id = excluded.channel_chat_id,
                    comments_chat_id = COALESCE(posts.comments_chat_id, excluded.comments_chat_id),
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

    def list_posts(self, limit: int = 10) -> list[sqlite3.Row]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT * FROM posts
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return list(rows)

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
                WHERE post_message_id = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (post_message_id, limit),
            ).fetchall()
        return list(rows)

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
            "WHERE post_message_id = ?",
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

    def get_comment(self, comment_id: int) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM comments WHERE id = ?",
                (comment_id,),
            ).fetchone()

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
    ) -> sqlite3.Row | None:
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
            self.conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
            self.conn.execute(
                "UPDATE comments SET parent_comment_id = NULL WHERE parent_comment_id = ?",
                (comment_id,),
            )
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
                (utc_now(), post_message_id),
            )
            self.conn.commit()
        return row

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
        self.bootstrap_legacy_channel_binding()
        self.delivery_mode = self.resolve_delivery_mode()
        self.marker: int | None = None
        self.bot_info: dict[str, Any] | None = None
        self.web_server: CommentWebServer | None = None
        self.channel_sync_thread: threading.Thread | None = None
        self.bind_cleanup_thread: threading.Thread | None = None
        self.update_queue: queue.Queue = queue.Queue()
        self.update_worker_thread: threading.Thread | None = None
        self.chat_title_cache: dict[int, tuple[str, float]] = {}

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

    def resolve_post_comments_chat_id(self, post: sqlite3.Row) -> int:
        raw_comments_chat_id = post["comments_chat_id"] if "comments_chat_id" in post.keys() else None
        if raw_comments_chat_id is not None:
            return int(raw_comments_chat_id)

        channel_chat_id = int(post["channel_chat_id"])
        binding = self.get_channel_binding(channel_chat_id)
        if binding is not None:
            return int(binding["comments_chat_id"])
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
        text = safe_text(((message.get("body") or {}).get("text")))
        sender = message.get("sender") or {}
        user_id = sender.get("user_id")
        if not user_id:
            return
        if self.is_own_message(sender):
            return

        if text.startswith("/"):
            self.handle_command(message, text)
            return
        if self.maybe_auto_attach_channel_post(message):
            return

        pending = self.store.pop_pending_comment(int(user_id))
        if pending is None:
            self.api.send_message(
                user_id=int(user_id),
                text=(
                    "Я жду команду.\n\n"
                    "Для администратора:\n"
                    "`/publish текст поста`\n"
                    "`/attach MESSAGE_ID`\n"
                    "`/posts`\n\n"
                    "Для пользователей теперь доступно мини-приложение с комментариями под каждым постом."
                ),
            )
            return

        self.save_user_comment(message, pending, text)

    def handle_command(self, message: dict[str, Any], text: str) -> None:
        sender = message.get("sender") or {}
        user_id = int(sender["user_id"])
        command, _, raw_args = text.partition(" ")
        args = raw_args.strip()

        if command == "/start":
            self.api.send_message(
                user_id=user_id,
                text=(
                    "Бот подключен.\n\n"
                    "Теперь комментарии к постам открываются в мини-приложении MAX.\n"
                    "Если администратор публикует пост прямо в канале, бот автоматически добавляет кнопку комментариев.\n"
                    "Под каждым таким постом бот публикует кнопку, которая открывает WebApp именно для этого поста.\n\n"
                    f"{self.setup_status_text()}"
                ),
            )
            return

        if command == "/help":
            self.api.send_message(
                user_id=user_id,
                text=(
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
                    "`/bind_channel` - отправить прямо в канале\n"
                    "`/bind_comments CODE` - отправить в чате комментариев\n"
                    "`/bind_channel same` - если комментарии должны жить в этом же чате\n\n"
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
            self.send_posts_overview(user_id)
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

        if command == "/bind_channel":
            self.begin_channel_binding(user_id=user_id, message=message, args=args)
            return

        if command == "/bind_comments":
            self.complete_channel_binding(user_id=user_id, message=message, args=args)
            return

        if command == "/bind_status":
            self.send_pending_channel_bindings_status(user_id)
            return

        if user_id not in ADMIN_USER_IDS:
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
        if self.has_channel_bindings() and ADMIN_USER_IDS:
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
        if not ADMIN_USER_IDS:
            missing.append("`MAX_ADMIN_USER_IDS`")

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

    def send_posts_overview(self, user_id: int) -> None:
        posts = self.store.list_posts()
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
        show_all = user_id in ADMIN_USER_IDS
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

    def get_chat_title_cached(self, chat_id: int) -> str:
        normalized_chat_id = int(chat_id)
        cached = self.chat_title_cache.get(normalized_chat_id)
        now = time.time()
        if cached is not None and now - cached[1] < 10 * 60:
            return cached[0]
        try:
            payload = self.api.get_chat(normalized_chat_id)
        except MaxApiError:
            logger.exception("Failed to load chat title for %s", normalized_chat_id)
            return cached[0] if cached is not None else ""
        title = chat_title_from_payload(payload)
        self.chat_title_cache[normalized_chat_id] = (title, now)
        return title

    def serialize_channel_binding(self, binding: sqlite3.Row) -> dict[str, Any]:
        channel_chat_id = int(binding["channel_chat_id"])
        comments_chat_id = int(binding["comments_chat_id"])
        channel_title = self.get_chat_title_cached(channel_chat_id)
        comments_chat_title = self.get_chat_title_cached(comments_chat_id)
        same_chat = int(binding["channel_chat_id"]) == int(binding["comments_chat_id"])
        return {
            "channel_chat_id": channel_chat_id,
            "channel_title": channel_title,
            "channel_label": channel_title or str(channel_chat_id),
            "comments_chat_id": comments_chat_id,
            "comments_chat_title": comments_chat_title,
            "comments_chat_label": comments_chat_title or str(comments_chat_id),
            "comments_chat_url": safe_text(binding["comments_chat_url"]),
            "created_at": safe_text(binding["created_at"]),
            "updated_at": safe_text(binding["updated_at"]),
            "same_chat": same_chat,
        }

    def serialize_post_summary(self, post: sqlite3.Row) -> dict[str, Any]:
        return {
            "post_message_id": safe_text(post["post_message_id"]),
            "post_ref": post_ref_for_message_id(post["post_message_id"]),
            "channel_chat_id": int(post["channel_chat_id"]),
            "comments_chat_id": int(post["comments_chat_id"]) if post["comments_chat_id"] is not None else None,
            "post_url": safe_text(post["post_url"]),
            "post_text": safe_text(post["post_text"]),
            "comment_count": int(post["comment_count"]),
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
            "bind_code": pending.bind_code,
            "requested_by_user_id": pending.requested_by_user_id,
            "channel_chat_id": pending.channel_chat_id,
            "channel_title": pending.channel_title or "",
            "created_at": pending.created_at,
            "created_at_display": format_utc_timestamp(pending.created_at),
            "remaining_seconds": remaining_seconds,
            "remaining_display": format_duration_compact(remaining_seconds),
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
                "admin_count": len(ADMIN_USER_IDS),
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

    def begin_channel_binding(self, *, user_id: int, message: dict[str, Any], args: str) -> None:
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
            self.start_channel_sync()
            attached_count = self.sync_recent_channel_posts_for_binding(
                channel_chat_id=channel_chat_id,
                comments_chat_id=channel_chat_id,
            )
            self.api.send_message(
                user_id=user_id,
                text=(
                    "Канал подключён в режиме одного чата.\n"
                    f"Канал: `{channel_chat_id}`\n"
                    f"Чат комментариев: `{channel_chat_id}`\n"
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
            "Канал подготовлен к подключению комментариев.",
            f"Канал: `{title or '-'} ({channel_chat_id})`",
            f"Код привязки: `{bind_code}`",
            "",
            "Теперь откройте нужный чат комментариев и отправьте туда:",
            f"`/bind_comments {bind_code}`",
            "",
            f"Код действует {BIND_CHANNEL_CODE_TTL_SECONDS // 60} минут.",
        ]
        share_url = max_share_url(f"/bind_comments {bind_code}")
        self.api.send_message(user_id=user_id, text="\n".join(lines))
        self.api.send_message(
            user_id=user_id,
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
                        "payload": "/bind_channel same",
                    },
                ]
            ),
        )

    def complete_channel_binding(self, *, user_id: int, message: dict[str, Any], args: str) -> None:
        bind_code = safe_text(args).upper()
        if not bind_code:
            self.api.send_message(
                user_id=user_id,
                text="Формат: `/bind_comments CODE`",
            )
            return

        pending = self.get_valid_pending_channel_binding(bind_code)
        if pending is None:
            self.api.send_message(
                user_id=user_id,
                text="Код привязки не найден или уже истёк. Запустите `/bind_channel` заново в канале.",
            )
            return
        if int(pending.requested_by_user_id) != int(user_id):
            self.api.send_message(
                user_id=user_id,
                text="Этот код привязки создан другим пользователем. Завершить привязку должен тот же администратор.",
            )
            return

        comments_chat_id, comments_title, _chat_type, comments_chat_link = self.current_chat_context(message)
        if comments_chat_id is None:
            self.api.send_message(
                user_id=user_id,
                text="Эту команду нужно отправить прямо в чате комментариев.",
            )
            return

        self.store.upsert_channel_binding(
            channel_chat_id=pending.channel_chat_id,
            comments_chat_id=comments_chat_id,
            comments_chat_url=comments_chat_link,
        )
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
        self.api.send_message(
            user_id=user_id,
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
        sender_user_id = sender.get("user_id")
        if not sender_user_id or int(sender_user_id) not in ADMIN_USER_IDS:
            return False

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
        source_attachments: list[dict[str, Any]] | None,
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
            discussion_message_id=(
                safe_text(existing_post["discussion_message_id"]) or None if existing_post is not None else None
            ),
        )

        discussion_message_id = (
            safe_text(existing_post["discussion_message_id"]) or None if existing_post is not None else None
        )
        if not discussion_message_id:
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

    def publish_post(self, text: str, *, admin_user_id: int | None, channel_chat_id: int) -> dict[str, Any]:
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
        for admin_id in ADMIN_USER_IDS:
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
        parent_comment = None
        if parent_comment_id is not None:
            parent_comment = self.store.get_comment(int(parent_comment_id))
            if parent_comment is None or safe_text(parent_comment["post_message_id"]) != safe_text(post_message_id):
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

        for admin_id in ADMIN_USER_IDS:
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
        return int(user_id) in ADMIN_USER_IDS

    def build_viewer_payload(
        self,
        init_data: str,
    ) -> dict[str, Any] | None:
        clean_init_data = safe_text(init_data)
        if not clean_init_data:
            return None
        try:
            viewer = self.authenticate_webapp_user(clean_init_data)
        except WebAppAuthError:
            return None
        return {
            "user_id": viewer.user_id,
            "display_name": viewer.display_name,
            "username": viewer.username,
            "is_admin": self.is_admin_user(viewer.user_id),
        }

    def get_post_payload(self, reference: str) -> dict[str, Any]:
        post = self.resolve_post_reference(reference)
        if post is None:
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
        normalized_limit = normalize_comments_page_limit(limit)
        rows, has_more = self.store.list_comments_page(
            post["post_message_id"],
            limit=normalized_limit,
            before_comment_id=before_comment_id,
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
            )
            for row in rows
        ]
        oldest_comment_id = comments[0]["id"] if comments else None
        return {
            "post": self.serialize_post(post),
            "comments": comments,
            "page": {
                "limit": normalized_limit,
                "has_more": has_more,
                "oldest_comment_id": oldest_comment_id,
                "before_comment_id": before_comment_id,
            },
            "viewer": self.build_viewer_payload(viewer_init_data),
        }

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
        return {
            "ok": True,
            "comment": comment,
            "post": self.serialize_post(post),
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
        comment = self.store.get_comment(comment_id)
        if comment is None or safe_text(comment["post_message_id"]) != safe_text(post["post_message_id"]):
            raise MaxApiError("Comment not found")
        is_admin = self.is_admin_user(auth_user.user_id)
        is_author = int(comment["user_id"]) == int(auth_user.user_id)
        if not is_admin and not is_author:
            raise MaxApiError("You can delete only your own comments")

        deleted = self.store.delete_comment(
            comment_id=comment_id,
            post_message_id=post["post_message_id"],
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
            return (
                "Комментарий удалён из ленты. Копию в чате обсуждения автоматически удалить не удалось."
            )

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

        comment = self.store.get_comment(comment_id)
        if comment is None or safe_text(comment["post_message_id"]) != safe_text(post["post_message_id"]):
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

        updated = self.store.update_comment_text(
            comment_id=comment_id,
            post_message_id=post["post_message_id"],
            text=clean_text,
        )
        if updated is None:
            raise MaxApiError("Comment not found")

        self.send_comment_edit_notice(
            post_message_id=post["post_message_id"],
            comments_chat_id=self.resolve_post_comments_chat_id(post),
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
            "post_text": post["post_text"],
            "post_preview": snippet(post["post_text"], 220),
            "post_url": post["post_url"],
            "media": extract_post_media_from_attachments(post_attachments),
            "comment_count": int(post["comment_count"]),
            "created_at": post["created_at"],
            "updated_at": post["updated_at"],
            "discussion_message_id": post["discussion_message_id"],
            "webapp_url": self.direct_webapp_url(post["post_message_id"]),
        }

    def serialize_comment(
        self,
        comment: sqlite3.Row,
        *,
        comment_id_override: int | None = None,
        parent_comment: sqlite3.Row | None = None,
    ) -> dict[str, Any]:
        return {
            "id": comment_id_override or int(comment["id"]),
            "post_message_id": comment["post_message_id"],
            "parent_comment_id": int(comment["parent_comment_id"]) if comment["parent_comment_id"] is not None else None,
            "user_id": int(comment["user_id"]),
            "display_name": comment["display_name"],
            "username": comment["username"],
            "text": comment["text"],
            "media": deserialize_comment_media(comment["media_json"]),
            "parent_comment": self.serialize_parent_comment(parent_comment),
            "source_kind": comment["source_kind"],
            "created_at": comment["created_at"],
        }

    def serialize_parent_comment(self, comment: sqlite3.Row | None) -> dict[str, Any] | None:
        if comment is None:
            return None
        return {
            "id": int(comment["id"]),
            "user_id": int(comment["user_id"]),
            "display_name": comment["display_name"],
            "username": comment["username"],
            "text": comment_reply_preview_text(comment),
            "has_media": bool(deserialize_comment_media(comment["media_json"])),
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
                    if not self.require_admin():
                        return
                    self.send_json(HTTPStatus.OK, app.get_admin_state())
                    return
                if path.startswith("/api/posts/"):
                    self.handle_api_get(path, parse.parse_qs(parsed.query, keep_blank_values=True))
                    return
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def do_HEAD(self) -> None:
                parsed = parse.urlparse(self.path)
                path = parsed.path
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
                if path == "/admin/login":
                    self.handle_admin_login()
                    return
                if path == "/admin/logout":
                    self.handle_admin_logout()
                    return
                if path.startswith("/api/admin/"):
                    self.handle_admin_post(path)
                    return
                if path.startswith("/api/posts/") and path.endswith("/comments"):
                    self.handle_create_comment(path)
                    return
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def do_PATCH(self) -> None:
                parsed = parse.urlparse(self.path)
                path = parsed.path
                if path.startswith("/api/posts/") and "/comments/" in path:
                    self.handle_update_comment(path)
                    return
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def do_DELETE(self) -> None:
                parsed = parse.urlparse(self.path)
                path = parsed.path
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
                except MaxApiError as exc:
                    status = HTTPStatus.BAD_REQUEST
                    if "not found" in str(exc).lower():
                        status = HTTPStatus.NOT_FOUND
                    self.send_json(status, {"error": humanize_comment_error_message(str(exc))})
                    return

                self.send_json(HTTPStatus.CREATED, result)

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
                if not ADMIN_PANEL_TOKEN:
                    self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Admin panel is not configured"})
                    return
                payload = self.read_json_body()
                if payload is None:
                    return
                provided = safe_text(payload.get("token"))
                if not hmac.compare_digest(provided, safe_text(ADMIN_PANEL_TOKEN)):
                    self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Неверный токен доступа"})
                    return
                self.send_json(
                    HTTPStatus.OK,
                    {"ok": True},
                    headers={"Set-Cookie": self.admin_session_cookie(safe_text(ADMIN_PANEL_TOKEN))},
                )

            def handle_admin_logout(self) -> None:
                self.send_json(
                    HTTPStatus.OK,
                    {"ok": True},
                    headers={"Set-Cookie": self.admin_session_cookie("", max_age=0)},
                )

            def handle_admin_post(self, path: str) -> None:
                if not self.require_admin():
                    return
                payload = self.read_json_body()
                if payload is None:
                    return
                try:
                    if path == "/api/admin/channels":
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
                        channel_chat_id = int(payload.get("channel_chat_id"))
                        text = safe_text(payload.get("text"))
                        if not text:
                            raise MaxApiError("Post text is empty")
                        result = app.publish_post(
                            text,
                            admin_user_id=None,
                            channel_chat_id=channel_chat_id,
                        )
                        self.send_json(HTTPStatus.CREATED, result)
                        return
                    if path == "/api/admin/attach":
                        post_message_id = safe_text(payload.get("post_message_id"))
                        if not post_message_id:
                            raise MaxApiError("Post message id is empty")
                        result = app.attach_existing_post(post_message_id, admin_user_id=None)
                        self.send_json(HTTPStatus.OK, result)
                        return
                    if path == "/api/admin/sync":
                        self.send_json(HTTPStatus.OK, app.admin_sync_recent_channel_posts())
                        return
                except (TypeError, ValueError):
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Проверьте числовые ID"})
                    return
                except MaxApiError as exc:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": humanize_comment_error_message(str(exc))})
                    return
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def handle_admin_delete(self, path: str) -> None:
                if not self.require_admin():
                    return
                prefix = "/api/admin/channels/"
                if not path.startswith(prefix):
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
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

            def admin_session_cookie(self, value: str, *, max_age: int = ADMIN_SESSION_MAX_AGE_SECONDS) -> str:
                encoded_value = parse.quote(value, safe="")
                secure = "; Secure" if WEB_APP_PUBLIC_URL.startswith("https://") else ""
                return (
                    f"{ADMIN_SESSION_COOKIE}={encoded_value}; Path=/; Max-Age={int(max_age)}; "
                    f"HttpOnly; SameSite=Lax{secure}"
                )

            def admin_auth_token(self) -> str:
                auth_header = safe_text(self.headers.get("Authorization") or self.headers.get("authorization"))
                if auth_header.lower().startswith("bearer "):
                    return auth_header[7:].strip()
                cookie_header = safe_text(self.headers.get("Cookie") or self.headers.get("cookie"))
                for raw_item in cookie_header.split(";"):
                    name, separator, value = raw_item.strip().partition("=")
                    if separator and name == ADMIN_SESSION_COOKIE:
                        return parse.unquote(value)
                return ""

            def is_admin_authenticated(self) -> bool:
                expected = safe_text(ADMIN_PANEL_TOKEN)
                if not expected:
                    return False
                return hmac.compare_digest(self.admin_auth_token(), expected)

            def require_admin(self) -> bool:
                if not ADMIN_PANEL_TOKEN:
                    self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Admin panel is not configured"})
                    return False
                if not self.is_admin_authenticated():
                    self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
                    return False
                return True

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
