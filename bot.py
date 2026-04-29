from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import mimetypes
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
    ADMIN_USER_IDS,
    BOT_TOKEN,
    CHANNEL_SYNC_INTERVAL_SECONDS,
    COMMENTS_CHAT_ID,
    COMMENTS_CHAT_URL,
    DATABASE_PATH,
    MAX_API_BASE_URL,
    POLL_LIMIT,
    POLL_TIMEOUT_SECONDS,
    TARGET_CHANNEL_CHAT_ID,
    WEB_APP_AUTH_MAX_AGE_SECONDS,
    WEB_APP_PUBLIC_URL,
    WEB_SERVER_ENABLED,
    WEB_SERVER_HOST,
    WEB_SERVER_PORT,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("max-comments-bot")

WEBAPP_DIR = Path(__file__).resolve().parent / "webapp"
COMMENT_MEDIA_DIR = Path(DATABASE_PATH).resolve().parent / "comment_media"
COMMENT_IMAGE_MAX_BYTES = 8 * 1024 * 1024
COMMENT_IMAGE_DATA_URL_MAX_LENGTH = 16 * 1024 * 1024


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


def post_ref_for_message_id(post_message_id: str) -> str:
    return f"post_{post_token_for_message_id(post_message_id)}"


def comment_code_for_post(post_message_id: str) -> str:
    digest = hashlib.sha1(post_message_id.encode("utf-8")).hexdigest()
    return f"c{digest[:8]}"


def comment_button_text(comment_count: int) -> str:
    return f"💬 {max(comment_count, 0)}"


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


@dataclass
class PendingComment:
    user_id: int
    post_message_id: str


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
            "types": ["message_created"],
        }
        return self._request("GET", "/updates", query=query)

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
                CREATE TABLE IF NOT EXISTS posts (
                    post_message_id TEXT PRIMARY KEY,
                    channel_chat_id INTEGER NOT NULL,
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
        post_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(posts)").fetchall()
        }
        if "post_attachments_json" not in post_columns:
            self.conn.execute(
                "ALTER TABLE posts ADD COLUMN post_attachments_json TEXT NOT NULL DEFAULT '[]'"
            )

    def upsert_post(
        self,
        *,
        post_message_id: str,
        channel_chat_id: int,
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
                    post_url,
                    post_text,
                    post_attachments_json,
                    discussion_message_id,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_message_id) DO UPDATE SET
                    channel_chat_id = excluded.channel_chat_id,
                    post_url = excluded.post_url,
                    post_text = excluded.post_text,
                    post_attachments_json = excluded.post_attachments_json,
                    discussion_message_id = COALESCE(excluded.discussion_message_id, posts.discussion_message_id),
                    updated_at = excluded.updated_at
                """,
                (
                    post_message_id,
                    channel_chat_id,
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
                    source_kind,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        self.marker: int | None = None
        self.bot_info: dict[str, Any] | None = None
        self.web_server: CommentWebServer | None = None
        self.channel_sync_thread: threading.Thread | None = None

    def run(self) -> None:
        self.bot_info = self.api.get_me()
        logger.info(
            "Connected as %s (@%s)",
            self.bot_info.get("first_name") or self.bot_info.get("name"),
            self.bot_info.get("username"),
        )
        self.start_web_server()
        self.start_channel_sync()
        while True:
            try:
                payload = self.api.get_updates(self.marker)
                self.marker = payload.get("marker", self.marker)
                for update in payload.get("updates", []):
                    self.handle_update(update)
            except KeyboardInterrupt:
                logger.info("Shutting down")
                break
            except Exception:
                logger.exception("Polling loop failed, retrying in 3 seconds")
                time.sleep(3)

    def start_web_server(self) -> None:
        if not WEB_SERVER_ENABLED:
            logger.info("Built-in web server is disabled")
            return
        self.web_server = CommentWebServer(self, WEB_SERVER_HOST, WEB_SERVER_PORT)
        self.web_server.start()
        logger.info("Web app server started on http://%s:%s", WEB_SERVER_HOST, WEB_SERVER_PORT)
        if WEB_APP_PUBLIC_URL:
            logger.info("Expected public WebApp URL: %s", WEB_APP_PUBLIC_URL)

    def start_channel_sync(self) -> None:
        if TARGET_CHANNEL_CHAT_ID is None or COMMENTS_CHAT_ID is None:
            logger.info("Channel auto-attach sync is disabled until channel ids are configured")
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
            "Channel auto-attach sync started for chat %s every %s seconds",
            TARGET_CHANNEL_CHAT_ID,
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
                    "`/publish текст поста` - опубликовать пост в канал через бота и открыть комментарии в мини-приложении\n"
                    "`/attach MESSAGE_ID` - подключить мини-приложение к уже существующему посту\n"
                    "`/posts` - показать последние зарегистрированные посты\n"
                    "`/chatinfo` - показать данные текущего чата\n\n"
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
            if not args:
                self.api.send_message(user_id=user_id, text="Формат: `/publish текст поста`")
                return
            self.publish_post(args, admin_user_id=user_id)
            return

        if command == "/attach":
            if not self.ensure_publish_ready(user_id):
                return
            if not args:
                self.api.send_message(user_id=user_id, text="Формат: `/attach MESSAGE_ID`")
                return
            self.attach_existing_post(args, admin_user_id=user_id)
            return

        self.api.send_message(user_id=user_id, text="Неизвестная команда. Используйте `/help`.")

    def ensure_publish_ready(self, user_id: int) -> bool:
        if is_configured_for_publishing():
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
        missing: list[str] = []
        if TARGET_CHANNEL_CHAT_ID is None:
            missing.append("`MAX_CHANNEL_CHAT_ID`")
        if COMMENTS_CHAT_ID is None:
            missing.append("`MAX_COMMENTS_CHAT_ID`")
        if not ADMIN_USER_IDS:
            missing.append("`MAX_ADMIN_USER_IDS`")

        if not missing:
            status = "Режим публикации настроен."
            if uses_same_chat_for_posts_and_comments():
                status += (
                    "\n\n"
                    "Внимание: канал и чат обсуждения совпадают. "
                    "Для нормальной работы лучше использовать отдельный чат комментариев."
                )
            if not WEB_APP_PUBLIC_URL:
                status += (
                    "\n\n"
                    "Не забудьте настроить публичный HTTPS-URL мини-приложения в кабинете MAX. "
                    "Локальный сервер из этого проекта нужен для разработки и бэкенда комментариев."
                )
            return status

        return (
            "Сейчас доступен диагностический режим.\n"
            "Заполните: "
            f"{', '.join(missing)}\n"
            "Используйте `/me`, чтобы получить свой `user_id`, и `/chatinfo`, чтобы получить `chat_id`."
        )

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
                f"- `{post['post_message_id']}` | `{ref}` | {post['comment_count']} комм. | {snippet(post['post_text'], 60)}"
            )
        self.api.send_message(user_id=user_id, text="\n".join(lines))

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
        if not is_configured_for_publishing() or TARGET_CHANNEL_CHAT_ID is None:
            return False

        recipient = message.get("recipient") or {}
        chat_id = recipient.get("chat_id")
        if chat_id is None or int(chat_id) != int(TARGET_CHANNEL_CHAT_ID):
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
                channel_chat_id=int(TARGET_CHANNEL_CHAT_ID),
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
        if TARGET_CHANNEL_CHAT_ID is None:
            return 0
        attached_count = 0
        messages = self.api.get_chat_messages(TARGET_CHANNEL_CHAT_ID, count=count)
        for message in reversed(messages):
            if not self.should_auto_attach_channel_message(message):
                continue
            body = message.get("body") or {}
            post_message_id = safe_text(body.get("mid") or message.get("mid"))
            if not post_message_id:
                continue
            self.register_channel_post_for_comments(
                post_message_id=post_message_id,
                channel_chat_id=int(TARGET_CHANNEL_CHAT_ID),
                post_url=message.get("url"),
                post_text=safe_text(body.get("text")),
                source_attachments=self.extract_post_attachments_from_message(message),
            )
            attached_count += 1
        return attached_count

    def should_auto_attach_channel_message(self, message: dict[str, Any]) -> bool:
        recipient = message.get("recipient") or {}
        chat_id = recipient.get("chat_id")
        if TARGET_CHANNEL_CHAT_ID is None or chat_id is None:
            return False
        if int(chat_id) != int(TARGET_CHANNEL_CHAT_ID):
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
        post_url: str | None,
        post_text: str,
        source_attachments: list[dict[str, Any]] | None,
    ) -> sqlite3.Row:
        clean_post_text = strip_managed_channel_footer(post_text)
        clean_attachments = list(source_attachments or [])
        existing_post = self.store.get_post(post_message_id)

        self.store.upsert_post(
            post_message_id=post_message_id,
            channel_chat_id=channel_chat_id,
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
                chat_id=COMMENTS_CHAT_ID,
                text=discussion_text,
            ).get("message", {})
            discussion_message_id = safe_text(
                discussion_message.get("body", {}).get("mid") or discussion_message.get("mid")
            )
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

    def publish_post(self, text: str, *, admin_user_id: int) -> None:
        channel_message = self.api.send_message(
            chat_id=TARGET_CHANNEL_CHAT_ID,
            text=text,
        ).get("message", {})

        post_message_id = safe_text(channel_message.get("body", {}).get("mid") or channel_message.get("mid"))
        if not post_message_id:
            raise MaxApiError("MAX API did not return message id for the channel post")

        post_url = channel_message.get("url")
        self.register_channel_post_for_comments(
            post_message_id=post_message_id,
            channel_chat_id=int(TARGET_CHANNEL_CHAT_ID),
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
        self.api.send_message(user_id=admin_user_id, text="\n".join(lines))

    def attach_existing_post(self, post_message_id: str, *, admin_user_id: int) -> None:
        message = self.api.get_message(post_message_id)
        post_text = safe_text(((message.get("body") or {}).get("text")))
        recipient = message.get("recipient") or {}
        chat_id = recipient.get("chat_id") or TARGET_CHANNEL_CHAT_ID
        post_url = message.get("url")

        self.register_channel_post_for_comments(
            post_message_id=post_message_id,
            channel_chat_id=int(chat_id),
            post_url=post_url,
            post_text=post_text,
            source_attachments=self.extract_post_attachments_from_message(message),
        )

        self.api.send_message(
            user_id=admin_user_id,
            text=(
                "Комментарии подключены к существующему посту.\n"
                f"ID поста: `{post_message_id}`"
            ),
        )

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
            chat_id=COMMENTS_CHAT_ID,
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
            source_kind=source_kind,
        )
        comment_count = self.store.get_comment_count(post_message_id)
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

        self.send_message_with_attachment_retry(
            chat_id=COMMENTS_CHAT_ID,
            text=discussion_text,
            attachments=discussion_attachments or None,
            link=link_payload,
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
        except MaxApiError:
            self.api.send_message(
                user_id=user_id,
                text="Пост для комментария больше не найден. Попробуйте открыть обсуждение заново.",
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

        comment_count = self.store.get_comment_count(post["post_message_id"])
        self.refresh_post_comment_button(
            post["post_message_id"],
            comment_count=comment_count,
        )

        return {
            "ok": True,
            "deleted_comment_id": comment_id,
            "post": self.serialize_post(self.store.get_post(post["post_message_id"]) or post),
        }

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

        updated = self.store.update_comment_text(
            comment_id=comment_id,
            post_message_id=post["post_message_id"],
            text=clean_text,
        )
        if updated is None:
            raise MaxApiError("Comment not found")

        self.send_comment_edit_notice(
            post_message_id=post["post_message_id"],
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
            server_version = "MaxCommentsWeb/1.0"

            def do_GET(self) -> None:
                parsed = parse.urlparse(self.path)
                path = parsed.path
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
                    self.send_json(HTTPStatus.OK, {"ok": True})
                    return
                if path.startswith("/api/posts/"):
                    self.handle_api_get(path, parse.parse_qs(parsed.query, keep_blank_values=True))
                    return
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def do_HEAD(self) -> None:
                parsed = parse.urlparse(self.path)
                path = parsed.path
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
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(HTTPStatus.NOT_FOUND)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self) -> None:
                parsed = parse.urlparse(self.path)
                path = parsed.path
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
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
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
                    self.send_json(status, {"error": str(exc)})
                    return

                self.send_json(HTTPStatus.CREATED, result)

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
                    self.send_json(status, {"error": str(exc)})
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
                    self.send_json(status, {"error": str(exc)})
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

            def read_init_data_header(self) -> str:
                return safe_text(
                    self.headers.get("X-Max-Init-Data")
                    or self.headers.get("x-max-init-data")
                    or ""
                )

            def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format: str, *args: Any) -> None:
                logger.info("web: " + format, *args)

        return Handler


if __name__ == "__main__":
    MaxCommentsBot().run()
