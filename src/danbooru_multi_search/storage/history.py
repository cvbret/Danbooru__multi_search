"""Transactional recent-query, recent-tag, and browser-session persistence."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

from danbooru_multi_search.storage.database import Database


MAX_RECENT_QUERIES = 50
MAX_RECENT_TAGS = 100
_RATING_ALIASES = {
    "all": "all",
    "g": "general",
    "general": "general",
    "s": "sensitive",
    "sensitive": "sensitive",
    "q": "questionable",
    "questionable": "questionable",
    "e": "explicit",
    "explicit": "explicit",
}


class HistoryError(RuntimeError):
    """Raised when persistent browsing history cannot be read or updated."""


@dataclass(frozen=True, slots=True)
class RecentQuery:
    """One normalized query identity and its last restorable gallery state."""

    id: int
    display_text: str
    normalized_tags: tuple[str, ...]
    rating: str
    page_size: int
    last_submitted_page: int
    last_committed_page: int | None
    anchor_post_id: int | None
    anchor_index: int | None
    anchor_offset: float | None
    scroll_position: float
    last_used_at: str

    @property
    def restore_page(self) -> int:
        """Prefer the last page that actually replaced the visible gallery."""

        return self.last_committed_page or self.last_submitted_page


@dataclass(frozen=True, slots=True)
class RecentTag:
    normalized_tag: str
    use_count: int
    last_used_at: str


@dataclass(frozen=True, slots=True)
class WindowGeometry:
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("window width and height must be positive")


@dataclass(frozen=True, slots=True)
class GalleryPosition:
    query_id: int
    anchor_post_id: int | None
    anchor_index: int | None
    anchor_offset: float | None
    scroll_position: float

    def __post_init__(self) -> None:
        if self.query_id < 1:
            raise ValueError("query_id must be positive")
        values = (self.anchor_post_id, self.anchor_index, self.anchor_offset)
        if any(value is None for value in values) and not all(
            value is None for value in values
        ):
            raise ValueError("gallery anchor fields must be all present or all absent")
        if self.anchor_post_id is not None and self.anchor_post_id < 1:
            raise ValueError("anchor_post_id must be positive")
        if self.anchor_index is not None and self.anchor_index < 0:
            raise ValueError("anchor_index must not be negative")
        if self.anchor_offset is not None and not math.isfinite(self.anchor_offset):
            raise ValueError("anchor_offset must be finite")
        if not math.isfinite(self.scroll_position):
            raise ValueError("scroll_position must be finite")


@dataclass(frozen=True, slots=True)
class BrowserSession:
    geometry: WindowGeometry | None
    is_maximized: bool
    active_query_id: int | None


def canonicalize_query_tags(tags: Iterable[str]) -> tuple[str, ...]:
    """Return the sorted, de-duplicated identity used by recent queries."""

    if isinstance(tags, (str, bytes)):
        raise TypeError("tags must be an iterable of complete tag strings")
    normalized: set[str] = set()
    for value in tags:
        if not isinstance(value, str):
            raise TypeError("each tag must be a string")
        tag = value.strip().casefold()
        if not tag or not tag.isascii() or any(
            character.isspace() or ord(character) < 32 or ord(character) > 126
            for character in tag
        ):
            raise ValueError(f"invalid normalized tag: {value!r}")
        normalized.add(tag)
    if not normalized:
        raise ValueError("at least one normalized tag is required")
    return tuple(sorted(normalized))


class HistoryRepository:
    """Persist bounded history using one SQLite transaction per public update."""

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()

    def record_accepted_query(
        self,
        *,
        display_text: str,
        normalized_tags: Iterable[str],
        rating: str,
        page_size: int,
        page: int,
        count_tag_usage: bool,
        used_at: datetime | None = None,
    ) -> RecentQuery | None:
        """Record an accepted request and optionally count a user query action.

        An accepted event older than every retained non-active query may be
        removed by the same capacity-cleanup transaction.  In that case the
        tag counters and cleanup still commit and ``None`` is returned.
        """

        if not isinstance(display_text, str) or not display_text.strip():
            raise ValueError("display_text must be a non-empty string")
        tags = canonicalize_query_tags(normalized_tags)
        rating_code = _validate_rating(rating)
        _validate_page_size(page_size)
        _validate_page(page)
        used_at_text = self._timestamp(used_at)
        encoded_tags = _encode_tags(tags)

        try:
            with self._lock, closing(self._database.connect()) as connection:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO recent_queries(
                            display_text,
                            normalized_tags,
                            rating,
                            page_size,
                            last_submitted_page,
                            last_used_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(normalized_tags, rating, page_size) DO UPDATE SET
                            display_text = CASE
                                WHEN excluded.last_used_at >= recent_queries.last_used_at
                                THEN excluded.display_text
                                ELSE recent_queries.display_text
                            END,
                            last_submitted_page = CASE
                                WHEN excluded.last_used_at >= recent_queries.last_used_at
                                THEN excluded.last_submitted_page
                                ELSE recent_queries.last_submitted_page
                            END,
                            last_used_at = MAX(
                                recent_queries.last_used_at,
                                excluded.last_used_at
                            )
                        """,
                        (
                            display_text,
                            encoded_tags,
                            rating_code,
                            page_size,
                            page,
                            used_at_text,
                        ),
                    )
                    if count_tag_usage:
                        self._increment_tags(connection, tags, used_at_text)
                    self._trim_queries(connection)
                    self._trim_tags(connection)
                    row = connection.execute(
                        """
                        SELECT * FROM recent_queries
                        WHERE normalized_tags = ? AND rating = ? AND page_size = ?
                        """,
                        (encoded_tags, rating_code, page_size),
                    ).fetchone()
        except sqlite3.Error as exc:
            raise HistoryError(f"Unable to record accepted query: {exc}") from exc
        return _row_to_query(row) if row is not None else None

    def mark_query_committed(
        self,
        query_id: int,
        page: int,
        *,
        preserve_position: bool = False,
    ) -> bool:
        """Mark a page as visible and make its query the active gallery query."""

        _validate_identifier(query_id, "query_id")
        _validate_page(page)
        if not isinstance(preserve_position, bool):
            raise TypeError("preserve_position must be a boolean")
        try:
            with self._lock, closing(self._database.connect()) as connection:
                with connection:
                    if preserve_position:
                        cursor = connection.execute(
                            """
                            UPDATE recent_queries
                            SET last_committed_page = ?
                            WHERE id = ?
                            """,
                            (page, query_id),
                        )
                    else:
                        cursor = connection.execute(
                            """
                            UPDATE recent_queries SET
                                last_committed_page = ?,
                                anchor_post_id = NULL,
                                anchor_index = NULL,
                                anchor_offset = NULL,
                                scroll_position = 0
                            WHERE id = ?
                            """,
                            (page, query_id),
                        )
                    if cursor.rowcount == 0:
                        return False
                    connection.execute(
                        """
                        UPDATE browser_session SET active_query_id = ? WHERE id = 1
                        """,
                        (query_id,),
                    )
        except sqlite3.Error as exc:
            raise HistoryError(f"Unable to commit query page: {exc}") from exc
        return True

    def update_gallery_position(self, position: GalleryPosition) -> bool:
        """Update only an existing query; never recreate history after clearing."""

        try:
            with self._lock, closing(self._database.connect()) as connection:
                with connection:
                    updated = self._update_position(connection, position)
                    if not updated:
                        connection.execute(
                            """
                            UPDATE browser_session
                            SET active_query_id = NULL
                            WHERE id = 1 AND active_query_id = ?
                            """,
                            (position.query_id,),
                        )
                    return updated
        except sqlite3.Error as exc:
            raise HistoryError(f"Unable to update gallery position: {exc}") from exc

    def save_browser_state(
        self,
        *,
        geometry: WindowGeometry | None,
        is_maximized: bool,
        active_query_id: int | None,
        gallery_position: GalleryPosition | None,
    ) -> bool:
        """Save state atomically; ``None`` preserves the last normal geometry."""

        if not isinstance(is_maximized, bool):
            raise TypeError("is_maximized must be a boolean")
        if active_query_id is not None:
            _validate_identifier(active_query_id, "active_query_id")
        if gallery_position is not None and (
            active_query_id != gallery_position.query_id
        ):
            raise ValueError("gallery position must belong to active_query_id")

        try:
            with self._lock, closing(self._database.connect()) as connection:
                with connection:
                    actual_active_id = active_query_id
                    position_saved = gallery_position is None
                    if active_query_id is not None:
                        exists = connection.execute(
                            "SELECT 1 FROM recent_queries WHERE id = ?",
                            (active_query_id,),
                        ).fetchone()
                        if exists is None:
                            actual_active_id = None
                            position_saved = False
                        elif gallery_position is not None:
                            position_saved = self._update_position(
                                connection,
                                gallery_position,
                            )
                    self._write_session(
                        connection,
                        geometry=geometry,
                        is_maximized=is_maximized,
                        active_query_id=actual_active_id,
                    )
                    return position_saved
        except sqlite3.Error as exc:
            raise HistoryError(f"Unable to save browser state: {exc}") from exc

    def set_active_query(self, query_id: int | None) -> bool:
        """Change the current gallery query only when the target still exists."""

        if query_id is not None:
            _validate_identifier(query_id, "query_id")
        try:
            with self._lock, closing(self._database.connect()) as connection:
                with connection:
                    actual_id = query_id
                    if query_id is not None:
                        exists = connection.execute(
                            "SELECT 1 FROM recent_queries WHERE id = ?",
                            (query_id,),
                        ).fetchone()
                        if exists is None:
                            actual_id = None
                    connection.execute(
                        "UPDATE browser_session SET active_query_id = ? WHERE id = 1",
                        (actual_id,),
                    )
                    return actual_id is not None or query_id is None
        except sqlite3.Error as exc:
            raise HistoryError(f"Unable to change active query: {exc}") from exc

    def clear_history(self) -> None:
        """Delete query and tag history while preserving window geometry."""

        try:
            with self._lock, closing(self._database.connect()) as connection:
                with connection:
                    connection.execute(
                        "UPDATE browser_session SET active_query_id = NULL WHERE id = 1"
                    )
                    connection.execute("DELETE FROM recent_queries")
                    connection.execute("DELETE FROM recent_tags")
        except sqlite3.Error as exc:
            raise HistoryError(f"Unable to clear browsing history: {exc}") from exc

    def get_query(self, query_id: int) -> RecentQuery | None:
        _validate_identifier(query_id, "query_id")
        return self._read_one(
            "SELECT * FROM recent_queries WHERE id = ?",
            (query_id,),
        )

    def latest_query(self) -> RecentQuery | None:
        return self._read_one(
            """
            SELECT * FROM recent_queries
            ORDER BY last_used_at DESC, id DESC
            LIMIT 1
            """,
            (),
        )

    def list_queries(self, limit: int = MAX_RECENT_QUERIES) -> tuple[RecentQuery, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        try:
            with self._lock, closing(self._database.connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM recent_queries
                    ORDER BY last_used_at DESC, id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise HistoryError(f"Unable to list recent queries: {exc}") from exc
        return tuple(_row_to_query(row) for row in rows)

    def list_tags(self, limit: int = MAX_RECENT_TAGS) -> tuple[RecentTag, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        try:
            with self._lock, closing(self._database.connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT normalized_tag, use_count, last_used_at
                    FROM recent_tags
                    ORDER BY use_count DESC, last_used_at DESC, normalized_tag ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise HistoryError(f"Unable to list recent tags: {exc}") from exc
        return tuple(
            RecentTag(
                normalized_tag=str(row["normalized_tag"]),
                use_count=int(row["use_count"]),
                last_used_at=str(row["last_used_at"]),
            )
            for row in rows
        )

    def load_session(self) -> BrowserSession:
        try:
            with self._lock, closing(self._database.connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM browser_session WHERE id = 1"
                ).fetchone()
        except sqlite3.Error as exc:
            raise HistoryError(f"Unable to load browser session: {exc}") from exc
        if row is None:
            raise HistoryError("Browser session row is missing")
        geometry = None
        if row["window_x"] is not None:
            geometry = WindowGeometry(
                x=int(row["window_x"]),
                y=int(row["window_y"]),
                width=int(row["window_width"]),
                height=int(row["window_height"]),
            )
        active_id = row["active_query_id"]
        return BrowserSession(
            geometry=geometry,
            is_maximized=bool(row["is_maximized"]),
            active_query_id=int(active_id) if active_id is not None else None,
        )

    def _read_one(
        self,
        statement: str,
        parameters: tuple[object, ...],
    ) -> RecentQuery | None:
        try:
            with self._lock, closing(self._database.connect()) as connection:
                row = connection.execute(statement, parameters).fetchone()
        except sqlite3.Error as exc:
            raise HistoryError(f"Unable to read recent query: {exc}") from exc
        return _row_to_query(row) if row is not None else None

    def _timestamp(self, supplied: datetime | None = None) -> str:
        value = self._clock() if supplied is None else supplied
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("history clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _increment_tags(
        connection: sqlite3.Connection,
        tags: tuple[str, ...],
        used_at: str,
    ) -> None:
        for tag in tags:
            connection.execute(
                """
                INSERT INTO recent_tags(normalized_tag, use_count, last_used_at)
                VALUES (?, 1, ?)
                ON CONFLICT(normalized_tag) DO UPDATE SET
                    use_count = recent_tags.use_count + 1,
                    last_used_at = MAX(
                        recent_tags.last_used_at,
                        excluded.last_used_at
                    )
                """,
                (tag, used_at),
            )

    @staticmethod
    def _trim_queries(connection: sqlite3.Connection) -> None:
        count = int(connection.execute("SELECT COUNT(*) FROM recent_queries").fetchone()[0])
        excess = count - MAX_RECENT_QUERIES
        if excess <= 0:
            return
        connection.execute(
            """
            DELETE FROM recent_queries
            WHERE id IN (
                SELECT query.id
                FROM recent_queries AS query
                WHERE query.id != COALESCE(
                    (SELECT active_query_id FROM browser_session WHERE id = 1),
                    -1
                )
                ORDER BY query.last_used_at ASC, query.id ASC
                LIMIT ?
            )
            """,
            (excess,),
        )

    @staticmethod
    def _trim_tags(connection: sqlite3.Connection) -> None:
        count = int(connection.execute("SELECT COUNT(*) FROM recent_tags").fetchone()[0])
        excess = count - MAX_RECENT_TAGS
        if excess <= 0:
            return
        connection.execute(
            """
            DELETE FROM recent_tags
            WHERE normalized_tag IN (
                SELECT normalized_tag
                FROM recent_tags
                ORDER BY use_count ASC, last_used_at ASC, normalized_tag ASC
                LIMIT ?
            )
            """,
            (excess,),
        )

    @staticmethod
    def _update_position(
        connection: sqlite3.Connection,
        position: GalleryPosition,
    ) -> bool:
        cursor = connection.execute(
            """
            UPDATE recent_queries SET
                anchor_post_id = ?,
                anchor_index = ?,
                anchor_offset = ?,
                scroll_position = ?
            WHERE id = ?
            """,
            (
                position.anchor_post_id,
                position.anchor_index,
                position.anchor_offset,
                position.scroll_position,
                position.query_id,
            ),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _write_session(
        connection: sqlite3.Connection,
        *,
        geometry: WindowGeometry | None,
        is_maximized: bool,
        active_query_id: int | None,
    ) -> None:
        if geometry is None:
            connection.execute(
                """
                UPDATE browser_session SET
                    is_maximized = ?,
                    active_query_id = ?
                WHERE id = 1
                """,
                (int(is_maximized), active_query_id),
            )
            return
        connection.execute(
            """
            UPDATE browser_session SET
                window_x = ?,
                window_y = ?,
                window_width = ?,
                window_height = ?,
                is_maximized = ?,
                active_query_id = ?
            WHERE id = 1
            """,
            (
                geometry.x,
                geometry.y,
                geometry.width,
                geometry.height,
                int(is_maximized),
                active_query_id,
            ),
        )


def _validate_rating(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("rating must be a string")
    normalized = _RATING_ALIASES.get(value.strip().casefold())
    if normalized is None:
        raise ValueError(f"unsupported rating code: {value}")
    return normalized


def _validate_page_size(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 200:
        raise ValueError("page_size must be an integer between 1 and 200")


def _validate_page(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("page must be a positive integer")


def _validate_identifier(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _encode_tags(tags: tuple[str, ...]) -> str:
    return json.dumps(tags, ensure_ascii=True, separators=(",", ":"))


def _row_to_query(row: sqlite3.Row) -> RecentQuery:
    try:
        decoded = json.loads(str(row["normalized_tags"]))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HistoryError("Stored recent-query tags are invalid JSON") from exc
    if not isinstance(decoded, list) or not all(
        isinstance(value, str) for value in decoded
    ):
        raise HistoryError("Stored recent-query tags must be a JSON string list")
    tags = canonicalize_query_tags(decoded)
    return RecentQuery(
        id=int(row["id"]),
        display_text=str(row["display_text"]),
        normalized_tags=tags,
        rating=str(row["rating"]),
        page_size=int(row["page_size"]),
        last_submitted_page=int(row["last_submitted_page"]),
        last_committed_page=(
            int(row["last_committed_page"])
            if row["last_committed_page"] is not None
            else None
        ),
        anchor_post_id=(
            int(row["anchor_post_id"])
            if row["anchor_post_id"] is not None
            else None
        ),
        anchor_index=(
            int(row["anchor_index"]) if row["anchor_index"] is not None else None
        ),
        anchor_offset=(
            float(row["anchor_offset"]) if row["anchor_offset"] is not None else None
        ),
        scroll_position=float(row["scroll_position"]),
        last_used_at=str(row["last_used_at"]),
    )
