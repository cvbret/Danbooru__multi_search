"""SQLite connection and ordered schema migrations."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS = (
    Migration(
        version=1,
        name="initial_application_metadata",
        statements=(
            """
            CREATE TABLE app_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        version=2,
        name="recent_queries_tags_and_browser_session",
        statements=(
            """
            CREATE TABLE recent_queries (
                id INTEGER PRIMARY KEY,
                display_text TEXT NOT NULL,
                normalized_tags TEXT NOT NULL,
                rating TEXT NOT NULL
                    CHECK (rating IN (
                        'all', 'general', 'sensitive', 'questionable', 'explicit'
                    )),
                page_size INTEGER NOT NULL
                    CHECK (page_size BETWEEN 1 AND 200),
                last_submitted_page INTEGER NOT NULL
                    CHECK (last_submitted_page >= 1),
                last_committed_page INTEGER
                    CHECK (last_committed_page IS NULL OR last_committed_page >= 1),
                anchor_post_id INTEGER
                    CHECK (anchor_post_id IS NULL OR anchor_post_id >= 1),
                anchor_index INTEGER
                    CHECK (anchor_index IS NULL OR anchor_index >= 0),
                anchor_offset REAL,
                scroll_position REAL NOT NULL DEFAULT 0,
                last_used_at TEXT NOT NULL,
                UNIQUE (normalized_tags, rating, page_size),
                CHECK (
                    (anchor_post_id IS NULL AND anchor_index IS NULL
                        AND anchor_offset IS NULL)
                    OR
                    (anchor_post_id IS NOT NULL AND anchor_index IS NOT NULL
                        AND anchor_offset IS NOT NULL)
                )
            )
            """,
            """
            CREATE INDEX recent_queries_eviction_index
            ON recent_queries(last_used_at ASC, id ASC)
            """,
            """
            CREATE TABLE recent_tags (
                normalized_tag TEXT PRIMARY KEY,
                use_count INTEGER NOT NULL CHECK (use_count >= 1),
                last_used_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX recent_tags_eviction_index
            ON recent_tags(
                use_count ASC,
                last_used_at ASC,
                normalized_tag ASC
            )
            """,
            """
            CREATE TABLE browser_session (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                window_x INTEGER,
                window_y INTEGER,
                window_width INTEGER
                    CHECK (window_width IS NULL OR window_width > 0),
                window_height INTEGER
                    CHECK (window_height IS NULL OR window_height > 0),
                is_maximized INTEGER NOT NULL DEFAULT 0
                    CHECK (is_maximized IN (0, 1)),
                active_query_id INTEGER,
                FOREIGN KEY (active_query_id)
                    REFERENCES recent_queries(id) ON DELETE SET NULL,
                CHECK (
                    (window_x IS NULL AND window_y IS NULL
                        AND window_width IS NULL AND window_height IS NULL)
                    OR
                    (window_x IS NOT NULL AND window_y IS NOT NULL
                        AND window_width IS NOT NULL AND window_height IS NOT NULL)
                )
            )
            """,
            """
            INSERT INTO browser_session(id, is_maximized)
            VALUES (1, 0)
            """,
        ),
    ),
)


class DatabaseError(RuntimeError):
    """Raised when the local database cannot be initialized safely."""


class Database:
    """Own the SQLite file and apply numbered migrations exactly once."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> int:
        try:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS schema_migrations (
                            version INTEGER PRIMARY KEY,
                            name TEXT NOT NULL,
                            applied_at TEXT NOT NULL
                        )
                        """
                    )
                    applied = {
                        row["version"]
                        for row in connection.execute(
                            "SELECT version FROM schema_migrations ORDER BY version"
                        )
                    }
                    latest_supported = max(
                        (migration.version for migration in MIGRATIONS), default=0
                    )
                    if applied and max(applied) > latest_supported:
                        raise DatabaseError(
                            "Database schema is newer than this application version"
                        )

                    for migration in MIGRATIONS:
                        if migration.version in applied:
                            continue
                        for statement in migration.statements:
                            connection.execute(statement)
                        connection.execute(
                            """
                            INSERT INTO schema_migrations(version, name, applied_at)
                            VALUES (?, ?, ?)
                            """,
                            (
                                migration.version,
                                migration.name,
                                datetime.now(timezone.utc).isoformat(),
                            ),
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except sqlite3.Error as exc:
            raise DatabaseError(f"Unable to initialize database at {self.path}: {exc}") from exc

        return self.current_version()

    def current_version(self) -> int:
        try:
            with closing(self.connect()) as connection:
                row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
                ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError(f"Unable to read database version: {exc}") from exc
        return int(row["version"])

