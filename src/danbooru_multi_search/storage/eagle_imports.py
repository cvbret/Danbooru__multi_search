"""Library-scoped Eagle item references and unresolved add requests."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from danbooru_multi_search.storage.database import Database, DatabaseError


@dataclass(frozen=True, slots=True)
class EagleImportRecord:
    library_id: str
    post_id: int
    item_id: str
    page_url: str
    # Absent when the item was matched by URL without verifying its file.
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class EagleImportAttempt:
    token: str
    library_id: str
    post_id: int
    sha256: str
    file_path: Path | None = None
    page_url: str | None = None


_RECORDS = """
    SELECT p.library_id, p.post_id, p.item_id, p.page_url, i.sha256
    FROM eagle_import_items AS i
    JOIN eagle_import_posts AS p USING (library_id, item_id)
"""


def _digest(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch("[0-9a-fA-F]{64}", value):
        raise DatabaseError("Eagle 导入记录中的 SHA256 无效。")
    return value.lower()


class EagleImportRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        try:
            with closing(self._database.connect()) as connection, connection:
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                yield connection
        except (sqlite3.Error, OSError, OverflowError) as exc:
            raise DatabaseError(f"Eagle 导入记录读写失败：{exc}") from exc

    def find_by_post(self, library_id: str, post_id: int) -> EagleImportRecord | None:
        """Return the confirmed association for this post in this library."""
        with self._connection() as connection:
            row = connection.execute(
                _RECORDS + " WHERE p.library_id = ? AND p.post_id = ?", (library_id, post_id)
            ).fetchone()
            return EagleImportRecord(**row) if row is not None else None

    def find_by_hash(self, library_id: str, sha256: str) -> tuple[EagleImportRecord, ...]:
        """Return every associated post, preserving conflicting item IDs for the caller."""
        with self._connection() as connection:
            rows = connection.execute(
                _RECORDS + " WHERE i.library_id = ? AND i.sha256 = ? ORDER BY p.post_id",
                (library_id, _digest(sha256)),
            )
            return tuple(EagleImportRecord(**row) for row in rows)

    def find_attempts(
        self, library_id: str, post_id: int, sha256: str | None = None,
    ) -> tuple[EagleImportAttempt, ...]:
        """Read unresolved attempts for either the same post or the same bytes."""
        digest = _digest(sha256) if sha256 is not None else None
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT token, library_id, post_id, sha256, file_path, page_url FROM eagle_import_attempts
                   WHERE library_id = ? AND (post_id = ? OR (? IS NOT NULL AND sha256 = ?)) ORDER BY post_id""",
                (library_id, post_id, digest, digest),
            )
            return tuple(
                EagleImportAttempt(
                    row["token"], row["library_id"], row["post_id"], row["sha256"],
                    Path(row["file_path"]) if row["file_path"] is not None else None, row["page_url"],
                )
                for row in rows
            )

    def begin_attempt(
        self, library_id: str, post_id: int, sha256: str, *,
        file_path: Path | None = None, page_url: str | None = None,
    ) -> str | None:
        """Reserve this library's post and content before dispatching an Eagle add."""
        token = uuid4().hex
        with self._connection(write=True) as connection:
            result = connection.execute(
                """INSERT INTO eagle_import_attempts (token, library_id, post_id, sha256, file_path, page_url)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (library_id, post_id) DO NOTHING
                   ON CONFLICT (library_id, sha256) DO NOTHING""",
                (token, library_id, post_id, _digest(sha256), str(file_path) if file_path is not None else None, page_url),
            )
            return token if result.rowcount == 1 else None

    def release_attempt(self, token: str) -> None:
        """Release a request known to have performed no write."""
        with self._connection(write=True) as connection:
            connection.execute("DELETE FROM eagle_import_attempts WHERE token = ?", (token,))

    def save(self, record: EagleImportRecord, *, attempt_token: str | None = None) -> None:
        """Confirm a verified association and finish its matching reservation atomically."""
        digest = _digest(record.sha256) if record.sha256 is not None else None
        with self._connection(write=True) as connection:
            if attempt_token is not None:
                removed = connection.execute(
                    """DELETE FROM eagle_import_attempts
                       WHERE token = ? AND library_id = ? AND post_id = ?
                       AND (? IS NULL OR sha256 = ?) AND (page_url IS NULL OR page_url = ?)""",
                    (attempt_token, record.library_id, record.post_id, digest, digest, record.page_url),
                )
                if removed.rowcount != 1:
                    raise DatabaseError("Eagle 导入确认与保存的请求不匹配。")
            item = connection.execute(
                """INSERT INTO eagle_import_items(library_id, item_id, sha256)
                   VALUES (?, ?, ?)
                   ON CONFLICT (library_id, item_id) DO UPDATE SET
                     sha256 = coalesce(eagle_import_items.sha256, excluded.sha256)
                   WHERE eagle_import_items.sha256 IS NULL OR excluded.sha256 IS NULL
                      OR eagle_import_items.sha256 = excluded.sha256""",
                (record.library_id, record.item_id, digest),
            )
            if item.rowcount != 1:
                raise DatabaseError("同一 Eagle 素材已有不同的文件 SHA256。")
            post = connection.execute(
                """INSERT INTO eagle_import_posts(library_id, post_id, item_id, page_url)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT (library_id, post_id) DO UPDATE SET page_url = excluded.page_url
                   WHERE eagle_import_posts.item_id = excluded.item_id""",
                (record.library_id, record.post_id, record.item_id, record.page_url),
            )
            if post.rowcount != 1:
                raise DatabaseError("同一作品已有不同的 Eagle 素材编号。")

    def forget(self, record: EagleImportRecord) -> bool:
        """Remove exactly the stale local association observed by the caller."""
        with self._connection(write=True) as connection:
            current = connection.execute(
                _RECORDS + " WHERE p.library_id = ? AND p.post_id = ?", (record.library_id, record.post_id)
            ).fetchone()
            if current is None or EagleImportRecord(**current) != record:
                return False
            connection.execute(
                "DELETE FROM eagle_import_posts WHERE library_id = ? AND post_id = ?",
                (record.library_id, record.post_id),
            )
            connection.execute(
                """DELETE FROM eagle_import_items WHERE library_id = ? AND item_id = ?
                   AND NOT EXISTS (SELECT 1 FROM eagle_import_posts WHERE library_id = ? AND item_id = ?)""",
                (record.library_id, record.item_id, record.library_id, record.item_id),
            )
            return True
