"""SQLite connection and ordered schema migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    apply: Callable[[sqlite3.Connection], None] | None = None


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
    Migration(
        version=3,
        name="complex_query_definitions",
        statements=(),
        apply=lambda connection: _migrate_to_version_three(connection),
    ),
    Migration(
        version=4,
        name="local_favorites",
        statements=(
            """
            CREATE TABLE favorite_folders (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL CHECK (length(name) > 0),
                normalized_name TEXT NOT NULL UNIQUE
                    CHECK (length(normalized_name) > 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE favorite_items (
                id INTEGER PRIMARY KEY,
                object_type TEXT NOT NULL
                    CHECK (object_type IN ('post', 'tag', 'pool')),
                object_key TEXT NOT NULL CHECK (length(object_key) > 0),
                tag_category TEXT
                    CHECK (
                        tag_category IS NULL
                        OR tag_category IN (
                            'artist', 'character', 'copyright', 'general'
                        )
                    ),
                folder_id INTEGER
                    REFERENCES favorite_folders(id) ON DELETE SET NULL,
                display_name TEXT NOT NULL CHECK (length(display_name) > 0),
                summary_version INTEGER NOT NULL
                    CHECK (summary_version = 1),
                summary_json TEXT NOT NULL CHECK (length(summary_json) > 0),
                post_id INTEGER
                    CHECK (post_id IS NULL OR post_id > 0),
                pool_id INTEGER
                    CHECK (pool_id IS NULL OR pool_id > 0),
                score INTEGER,
                fav_count INTEGER
                    CHECK (fav_count IS NULL OR fav_count >= 0),
                rating TEXT
                    CHECK (rating IS NULL OR rating IN ('g', 's', 'q', 'e')),
                favorited_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (object_type, object_key),
                CHECK (
                    (
                        object_type = 'post'
                        AND post_id IS NOT NULL
                        AND typeof(post_id) = 'integer'
                        AND pool_id IS NULL
                        AND tag_category IS NULL
                        AND object_key = CAST(post_id AS TEXT)
                        AND rating IS NOT NULL
                    )
                    OR (
                        object_type = 'pool'
                        AND pool_id IS NOT NULL
                        AND typeof(pool_id) = 'integer'
                        AND post_id IS NULL
                        AND tag_category IS NULL
                        AND object_key = CAST(pool_id AS TEXT)
                        AND score IS NULL
                        AND fav_count IS NULL
                        AND rating IS NULL
                    )
                    OR (
                        object_type = 'tag'
                        AND post_id IS NULL
                        AND pool_id IS NULL
                        AND tag_category IS NOT NULL
                        AND score IS NULL
                        AND fav_count IS NULL
                        AND rating IS NULL
                    )
                )
            )
            """,
            """
            CREATE TABLE favorite_post_tags (
                favorite_id INTEGER NOT NULL
                    REFERENCES favorite_items(id) ON DELETE CASCADE,
                tag_category TEXT NOT NULL
                    CHECK (tag_category IN (
                        'artist', 'character', 'copyright', 'general', 'meta'
                    )),
                tag_name TEXT NOT NULL CHECK (length(tag_name) > 0),
                PRIMARY KEY (favorite_id, tag_category, tag_name)
            )
            """,
            """
            CREATE INDEX favorite_items_folder_type_index
            ON favorite_items(folder_id, object_type, id)
            """,
            """
            CREATE INDEX favorite_items_favorited_at_index
            ON favorite_items(favorited_at, id)
            """,
            """
            CREATE INDEX favorite_items_score_index
            ON favorite_items(score, id)
            """,
            """
            CREATE INDEX favorite_items_fav_count_index
            ON favorite_items(fav_count, id)
            """,
            """
            CREATE INDEX favorite_items_rating_index
            ON favorite_items(rating, id)
            """,
            """
            CREATE INDEX favorite_post_tags_name_index
            ON favorite_post_tags(tag_name, favorite_id)
            """,
            """
            CREATE INDEX favorite_post_tags_category_name_index
            ON favorite_post_tags(tag_category, tag_name, favorite_id)
            """,
        ),
    ),
    Migration(
        version=5,
        name="favorite_collection_revision",
        statements=(
            """
            CREATE TABLE favorite_collection_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                revision INTEGER NOT NULL
                    CHECK (revision >= 0 AND typeof(revision) = 'integer')
            )
            """,
            """
            INSERT INTO favorite_collection_state(id, revision)
            VALUES (1, 0)
            """,
        ),
    ),
    Migration(
        version=6,
        name="saved_artist_combinations",
        statements=(
            """
            CREATE TABLE artist_combinations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL CHECK (length(name) > 0),
                normalized_name TEXT NOT NULL UNIQUE CHECK (length(normalized_name) > 0),
                artists_json TEXT NOT NULL CHECK (length(artists_json) > 0),
                mode TEXT NOT NULL CHECK (mode IN ('all', 'any', 'exclude'))
            )
            """,
        ),
    ),
    Migration(
        version=7,
        name="persistent_download_tasks",
        statements=(
            """CREATE TABLE download_batches (
                id TEXT PRIMARY KEY NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE download_items (
                id TEXT PRIMARY KEY NOT NULL,
                batch_id TEXT NOT NULL REFERENCES download_batches(id) ON DELETE CASCADE,
                position INTEGER NOT NULL CHECK(position >= 0),
                post_id INTEGER NOT NULL CHECK(post_id > 0),
                source_url TEXT NOT NULL,
                file_ext TEXT NOT NULL,
                label TEXT NOT NULL,
                directory TEXT NOT NULL,
                policy TEXT NOT NULL CHECK(policy IN ('skip', 'overwrite', 'auto_number')),
                state TEXT NOT NULL CHECK(state IN ('waiting', 'running', 'retry_wait', 'awaiting_resume', 'completed', 'skipped', 'failed', 'cancelled')),
                resolved_name TEXT,
                partial_name TEXT,
                bytes_written INTEGER NOT NULL DEFAULT 0 CHECK(bytes_written >= 0),
                total_bytes INTEGER CHECK(total_bytes IS NULL OR total_bytes >= 0),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
                error_code TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                UNIQUE(batch_id, position)
            )""",
            "CREATE INDEX download_items_batch_position ON download_items(batch_id, position)",
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
                        if migration.apply is not None:
                            migration.apply(connection)
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
        except DatabaseError:
            raise
        except Exception as exc:
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


def _migrate_to_version_three(connection: sqlite3.Connection) -> None:
    """Rebuild query and session tables while preserving all version-2 rows."""

    from danbooru_multi_search.storage.migrations_v3 import (
        version_three_definition_json,
    )

    connection.execute(
        """
        CREATE TABLE recent_queries_v3 (
            id INTEGER PRIMARY KEY,
            query_key TEXT NOT NULL UNIQUE,
            definition_version INTEGER NOT NULL
                CHECK (definition_version = 1),
            query_mode TEXT NOT NULL
                CHECK (query_mode IN ('standard', 'rank', 'latest')),
            definition_json TEXT NOT NULL,
            result_summary_json TEXT,
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
            CHECK (query_key = definition_json),
            CHECK (
                (anchor_post_id IS NULL AND anchor_index IS NULL
                    AND anchor_offset IS NULL)
                OR
                (anchor_post_id IS NOT NULL AND anchor_index IS NOT NULL
                    AND anchor_offset IS NOT NULL)
            )
        )
        """
    )
    rows = connection.execute(
        "SELECT * FROM recent_queries ORDER BY id"
    ).fetchall()
    for row in rows:
        definition_json = version_three_definition_json(
            str(row["normalized_tags"]),
            str(row["rating"]),
            int(row["page_size"]),
        )
        connection.execute(
            """
            INSERT INTO recent_queries_v3(
                id, query_key, definition_version, query_mode,
                definition_json, result_summary_json, display_text,
                normalized_tags, rating, page_size, last_submitted_page,
                last_committed_page, anchor_post_id, anchor_index,
                anchor_offset, scroll_position, last_used_at
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(row["id"]),
                definition_json,
                1,
                "standard",
                definition_json,
                str(row["display_text"]),
                str(row["normalized_tags"]),
                str(row["rating"]),
                int(row["page_size"]),
                int(row["last_submitted_page"]),
                row["last_committed_page"],
                row["anchor_post_id"],
                row["anchor_index"],
                row["anchor_offset"],
                row["scroll_position"],
                str(row["last_used_at"]),
            ),
        )

    connection.execute(
        """
        CREATE TABLE browser_session_v3 (
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
                REFERENCES recent_queries_v3(id) ON DELETE SET NULL,
            CHECK (
                (window_x IS NULL AND window_y IS NULL
                    AND window_width IS NULL AND window_height IS NULL)
                OR
                (window_x IS NOT NULL AND window_y IS NOT NULL
                    AND window_width IS NOT NULL AND window_height IS NOT NULL)
            )
        )
        """
    )
    connection.execute(
        """
        INSERT INTO browser_session_v3(
            id, window_x, window_y, window_width, window_height,
            is_maximized, active_query_id
        )
        SELECT id, window_x, window_y, window_width, window_height,
               is_maximized, active_query_id
        FROM browser_session
        """
    )
    connection.execute("DROP TABLE browser_session")
    connection.execute("DROP TABLE recent_queries")
    connection.execute("ALTER TABLE recent_queries_v3 RENAME TO recent_queries")
    connection.execute("ALTER TABLE browser_session_v3 RENAME TO browser_session")
    connection.execute(
        """
        CREATE INDEX recent_queries_eviction_index
        ON recent_queries(last_used_at ASC, id ASC)
        """
    )
