"""Transactional recent-query, recent-tag, and browser-session persistence."""

from __future__ import annotations

import math
import sqlite3
import threading
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

from danbooru_multi_search.danbooru.query_definition import (
    GalleryQueryDefinition,
    QueryMode,
)
from danbooru_multi_search.danbooru.query_types import (
    GalleryResultSummary,
    QueryExecutionLimits,
)
from danbooru_multi_search.danbooru.query_language import parse_query_expression
from danbooru_multi_search.danbooru.query_planning import QueryRating
from danbooru_multi_search.danbooru.result_processing import (
    ResultFilterSpec,
    ResultSortSpec,
)
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.query_serialization import (
    QUERY_DEFINITION_VERSION,
    QuerySerializationError,
    decode_normalized_tags,
    decode_query_definition,
    decode_result_summary,
    encode_normalized_tags,
    encode_query_definition,
    encode_result_summary,
    query_definition_key,
    query_definition_tags,
)


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
    definition: GalleryQueryDefinition
    result_summary: GalleryResultSummary | None

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
        busy_timeout_ms: int = 5000,
    ) -> None:
        self._database = database
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._busy_timeout_ms = busy_timeout_ms

    def _connect(self) -> sqlite3.Connection:
        return self._database.connect() if self._busy_timeout_ms == 5000 else self._database.connect(busy_timeout_ms=self._busy_timeout_ms)

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
        """Keep the M1 simple-tag API as a version-1 definition wrapper."""

        tags = canonicalize_query_tags(normalized_tags)
        rating_code = _validate_rating(rating)
        _validate_page_size(page_size)
        parsed = parse_query_expression(" AND ".join(tags))
        definition = GalleryQueryDefinition(
            mode=QueryMode.STANDARD,
            normalized_expression=parsed.normalized_text,
            rating=QueryRating(rating_code),
            filter_spec=ResultFilterSpec(),
            sort_spec=ResultSortSpec(),
            limits=QueryExecutionLimits(page_size=page_size),
        )
        return self.record_accepted_definition(
            definition=definition,
            display_text=display_text,
            page=page,
            count_tag_usage=count_tag_usage,
            used_at=used_at,
        )

    def record_accepted_definition(
        self,
        *,
        definition: GalleryQueryDefinition,
        display_text: str,
        page: int,
        count_tag_usage: bool,
        used_at: datetime | None = None,
        transaction_hook: Callable[[sqlite3.Connection, RecentQuery], None] | None = None,
    ) -> RecentQuery | None:
        """Record one accepted M2 definition and its unique expression tags.

        Capacity cleanup may immediately remove an older non-active event. Its
        tag counters still commit in the same transaction and ``None`` is
        returned.
        """

        if not isinstance(definition, GalleryQueryDefinition):
            raise TypeError("definition must be a GalleryQueryDefinition")
        if not isinstance(display_text, str) or not display_text.strip():
            raise ValueError("display_text must be a non-empty string")
        if not isinstance(count_tag_usage, bool):
            raise TypeError("count_tag_usage must be a boolean")
        _validate_page(page)
        used_at_text = self._timestamp(used_at)
        tags = query_definition_tags(definition)
        encoded_tags = encode_normalized_tags(tags)
        definition_json = encode_query_definition(definition)
        query_key = query_definition_key(definition)

        try:
            with self._lock, closing(self._database.connect(busy_timeout_ms=0) if transaction_hook else self._connect()) as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    _validate_existing_query_identity(
                        connection,
                        definition_json,
                    )
                    connection.execute(
                        """
                        INSERT INTO recent_queries(
                            query_key, definition_version, query_mode,
                            definition_json, result_summary_json, display_text,
                            normalized_tags, rating, page_size,
                            last_submitted_page, last_used_at
                        ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(query_key) DO UPDATE SET
                            result_summary_json = CASE
                                WHEN excluded.last_used_at >= recent_queries.last_used_at
                                THEN excluded.result_summary_json
                                ELSE recent_queries.result_summary_json
                            END,
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
                            query_key,
                            QUERY_DEFINITION_VERSION,
                            definition.mode.value,
                            definition_json,
                            display_text,
                            encoded_tags,
                            definition.rating.value,
                            definition.limits.page_size,
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
                        WHERE query_key = ?
                        """,
                        (query_key,),
                    ).fetchone()
                    result = _row_to_query(row) if row is not None else None
                    if result is not None and transaction_hook is not None:
                        transaction_hook(connection, result)
        except sqlite3.Error as exc:
            raise HistoryError(f"Unable to record accepted query: {exc}") from exc
        return result

    def record_view_definition(
        self,
        *,
        source_query_id: int,
        definition: GalleryQueryDefinition,
        used_at: datetime | None = None,
        transaction_hook: Callable[[sqlite3.Connection, RecentQuery], None] | None = None,
    ) -> RecentQuery | None:
        """Create or reuse a view identity without incrementing recent tags."""

        _validate_identifier(source_query_id, "source_query_id")
        if not isinstance(definition, GalleryQueryDefinition):
            raise TypeError("definition must be a GalleryQueryDefinition")
        used_at_text = self._timestamp(used_at)
        definition_json = encode_query_definition(definition)
        query_key = query_definition_key(definition)
        encoded_tags = encode_normalized_tags(query_definition_tags(definition))
        try:
            with self._lock, closing(self._database.connect(busy_timeout_ms=0) if transaction_hook else self._connect()) as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    source = connection.execute(
                        "SELECT * FROM recent_queries WHERE id = ?",
                        (source_query_id,),
                    ).fetchone()
                    if source is None:
                        return None
                    source_query = _row_to_query(source)
                    _validate_existing_query_identity(
                        connection,
                        definition_json,
                    )
                    connection.execute(
                        """
                        INSERT INTO recent_queries(
                            query_key, definition_version, query_mode,
                            definition_json, result_summary_json, display_text,
                            normalized_tags, rating, page_size,
                            last_submitted_page, last_committed_page,
                            anchor_post_id, anchor_index, anchor_offset,
                            scroll_position, last_used_at
                        ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(query_key) DO UPDATE SET
                            result_summary_json = CASE
                                WHEN excluded.last_used_at >= recent_queries.last_used_at
                                THEN excluded.result_summary_json
                                ELSE recent_queries.result_summary_json
                            END,
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
                            last_committed_page = CASE
                                WHEN excluded.last_used_at >= recent_queries.last_used_at
                                THEN excluded.last_committed_page
                                ELSE recent_queries.last_committed_page
                            END,
                            anchor_post_id = CASE
                                WHEN excluded.last_used_at >= recent_queries.last_used_at
                                THEN excluded.anchor_post_id
                                ELSE recent_queries.anchor_post_id
                            END,
                            anchor_index = CASE
                                WHEN excluded.last_used_at >= recent_queries.last_used_at
                                THEN excluded.anchor_index
                                ELSE recent_queries.anchor_index
                            END,
                            anchor_offset = CASE
                                WHEN excluded.last_used_at >= recent_queries.last_used_at
                                THEN excluded.anchor_offset
                                ELSE recent_queries.anchor_offset
                            END,
                            scroll_position = CASE
                                WHEN excluded.last_used_at >= recent_queries.last_used_at
                                THEN excluded.scroll_position
                                ELSE recent_queries.scroll_position
                            END,
                            last_used_at = MAX(
                                recent_queries.last_used_at,
                                excluded.last_used_at
                            )
                        """,
                        (
                            query_key,
                            QUERY_DEFINITION_VERSION,
                            definition.mode.value,
                            definition_json,
                            source_query.display_text,
                            encoded_tags,
                            definition.rating.value,
                            definition.limits.page_size,
                            source_query.last_submitted_page,
                            source_query.last_committed_page,
                            source_query.anchor_post_id,
                            source_query.anchor_index,
                            source_query.anchor_offset,
                            source_query.scroll_position,
                            used_at_text,
                        ),
                    )
                    self._trim_queries(connection)
                    row = connection.execute(
                        "SELECT * FROM recent_queries WHERE query_key = ?",
                        (query_key,),
                    ).fetchone()
                    result = _row_to_query(row) if row is not None else None
                    if result is not None and transaction_hook is not None:
                        transaction_hook(connection, result)
        except sqlite3.Error as exc:
            raise HistoryError(f"Unable to record view definition: {exc}") from exc
        return result

    def update_result_summary(
        self,
        query_id: int,
        summary: GalleryResultSummary,
        *,
        transaction_hook: Callable[[sqlite3.Connection, RecentQuery], None] | None = None,
    ) -> bool:
        """Update only the result summary without changing query identity."""

        _validate_identifier(query_id, "query_id")
        encoded = encode_result_summary(summary)
        try:
            with self._lock, closing(self._database.connect(busy_timeout_ms=0) if transaction_hook else self._connect()) as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute(
                        "SELECT * FROM recent_queries WHERE id = ?",
                        (query_id,),
                    ).fetchone()
                    if row is None:
                        return False
                    query = _row_to_query(row)
                    if transaction_hook is not None:
                        transaction_hook(connection, query)
                    cursor = connection.execute(
                        """
                        UPDATE recent_queries
                        SET result_summary_json = ?
                        WHERE id = ?
                        """,
                        (encoded, query_id),
                    )
                    return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise HistoryError(f"Unable to update result summary: {exc}") from exc

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
            with self._lock, closing(self._connect()) as connection:
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
            with self._lock, closing(self._connect()) as connection:
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
            with self._lock, closing(self._connect()) as connection:
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
            with self._lock, closing(self._connect()) as connection:
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
            with self._lock, closing(self._connect()) as connection:
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
            with self._lock, closing(self._connect()) as connection:
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
            with self._lock, closing(self._connect()) as connection:
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
            with self._lock, closing(self._connect()) as connection:
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
            with self._lock, closing(self._connect()) as connection:
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


def _validate_existing_query_identity(
    connection: sqlite3.Connection,
    definition_json: str,
) -> None:
    """Reject damaged redundant columns before an identity can be duplicated."""

    rows = connection.execute(
        """
        SELECT * FROM recent_queries
        WHERE query_key = ? OR definition_json = ?
        ORDER BY id
        """,
        (definition_json, definition_json),
    ).fetchall()
    for row in rows:
        _row_to_query(row)


def _row_to_query(row: sqlite3.Row) -> RecentQuery:
    try:
        definition_json = _stored_text(row["definition_json"], "definition_json")
        definition = decode_query_definition(definition_json)
        query_key = _stored_text(row["query_key"], "query_key")
        if query_key != definition_json or query_key != query_definition_key(definition):
            raise HistoryError("Stored recent-query key does not match its definition")
        version = _stored_integer(row["definition_version"], "definition_version")
        if version != QUERY_DEFINITION_VERSION:
            raise HistoryError("Stored recent-query definition version is unsupported")
        if _stored_text(row["query_mode"], "query_mode") != definition.mode.value:
            raise HistoryError("Stored recent-query mode does not match its definition")
        tags = decode_normalized_tags(
            _stored_text(row["normalized_tags"], "normalized_tags"),
            allow_empty=True,
        )
        if tags != query_definition_tags(definition):
            raise HistoryError("Stored recent-query tags do not match its definition")
        rating = _stored_text(row["rating"], "rating")
        if rating != definition.rating.value:
            raise HistoryError("Stored recent-query rating does not match its definition")
        page_size = _stored_integer(row["page_size"], "page_size")
        if page_size != definition.limits.page_size:
            raise HistoryError("Stored recent-query page size does not match its definition")
        query_id = _stored_positive_integer(row["id"], "id")
        display_text = _stored_text(row["display_text"], "display_text")
        if not display_text.strip():
            raise HistoryError("Stored recent-query display text is empty")
        submitted_page = _stored_positive_integer(
            row["last_submitted_page"],
            "last_submitted_page",
        )
        committed_page = _stored_optional_positive_integer(
            row["last_committed_page"],
            "last_committed_page",
        )
        anchor_post_id = _stored_optional_positive_integer(
            row["anchor_post_id"],
            "anchor_post_id",
        )
        anchor_index = _stored_optional_nonnegative_integer(
            row["anchor_index"],
            "anchor_index",
        )
        anchor_offset = _stored_optional_finite_float(
            row["anchor_offset"],
            "anchor_offset",
        )
        anchor_values = (anchor_post_id, anchor_index, anchor_offset)
        if any(value is None for value in anchor_values) and not all(
            value is None for value in anchor_values
        ):
            raise HistoryError("Stored recent-query anchor fields are incomplete")
        scroll_position = _stored_finite_float(
            row["scroll_position"],
            "scroll_position",
        )
        last_used_at = _stored_text(row["last_used_at"], "last_used_at")
        if not last_used_at:
            raise HistoryError("Stored recent-query timestamp is empty")
        encoded_summary = row["result_summary_json"]
        result_summary = (
            None
            if encoded_summary is None
            else decode_result_summary(
                _stored_text(encoded_summary, "result_summary_json")
            )
        )
        return RecentQuery(
            id=query_id,
            display_text=display_text,
            normalized_tags=tags,
            rating=rating,
            page_size=page_size,
            last_submitted_page=submitted_page,
            last_committed_page=committed_page,
            anchor_post_id=anchor_post_id,
            anchor_index=anchor_index,
            anchor_offset=anchor_offset,
            scroll_position=scroll_position,
            last_used_at=last_used_at,
            definition=definition,
            result_summary=result_summary,
        )
    except HistoryError:
        raise
    except (
        QuerySerializationError,
        TypeError,
        ValueError,
        OverflowError,
        KeyError,
        IndexError,
    ) as exc:
        raise HistoryError(f"Stored recent-query data is invalid: {exc}") from exc


def _stored_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise HistoryError(f"Stored {label} must be text")
    return value


def _stored_integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise HistoryError(f"Stored {label} must be an integer")
    return value


def _stored_positive_integer(value: object, label: str) -> int:
    parsed = _stored_integer(value, label)
    if parsed < 1:
        raise HistoryError(f"Stored {label} must be positive")
    return parsed


def _stored_optional_positive_integer(value: object, label: str) -> int | None:
    return None if value is None else _stored_positive_integer(value, label)


def _stored_optional_nonnegative_integer(
    value: object,
    label: str,
) -> int | None:
    if value is None:
        return None
    parsed = _stored_integer(value, label)
    if parsed < 0:
        raise HistoryError(f"Stored {label} cannot be negative")
    return parsed


def _stored_finite_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoryError(f"Stored {label} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise HistoryError(f"Stored {label} must be finite")
    return parsed


def _stored_optional_finite_float(value: object, label: str) -> float | None:
    return None if value is None else _stored_finite_float(value, label)
