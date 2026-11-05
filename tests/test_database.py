from __future__ import annotations

import sqlite3

import pytest

import danbooru_multi_search.storage.database as database_module
from danbooru_multi_search.danbooru import QueryMode
from danbooru_multi_search.storage import HistoryRepository
from danbooru_multi_search.storage.database import Database, DatabaseError, Migration


def test_database_applies_migrations_once(tmp_path):
    database = Database(tmp_path / "application.sqlite3")

    assert database.initialize() == 3
    assert database.initialize() == 3

    with database.connect() as connection:
        migrations = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        session = connection.execute(
            "SELECT id, is_maximized, active_query_id FROM browser_session"
        ).fetchone()

    assert [(row["version"], row["name"]) for row in migrations] == [
        (1, "initial_application_metadata"),
        (2, "recent_queries_tags_and_browser_session"),
        (3, "complex_query_definitions"),
    ]
    assert {
        "app_metadata",
        "recent_queries",
        "recent_tags",
        "browser_session",
    }.issubset(tables)
    assert tuple(session) == (1, 0, None)


def test_database_upgrades_version_one_without_losing_existing_data(tmp_path):
    path = tmp_path / "application.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE app_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_migrations(version, name, applied_at)
            VALUES (1, 'initial_application_metadata', '2026-01-01T00:00:00Z');
            INSERT INTO app_metadata(key, value) VALUES ('preserved', 'yes');
            """
        )

    database = Database(path)

    assert database.initialize() == 3
    assert database.initialize() == 3
    with database.connect() as connection:
        preserved = connection.execute(
            "SELECT value FROM app_metadata WHERE key = 'preserved'"
        ).fetchone()
        migration_count = connection.execute(
            "SELECT COUNT(*) AS count FROM schema_migrations WHERE version IN (2, 3)"
        ).fetchone()["count"]

    assert preserved["value"] == "yes"
    assert migration_count == 2


def test_version_three_schema_enforces_single_session_and_query_constraints(tmp_path):
    database = Database(tmp_path / "application.sqlite3")
    database.initialize()

    with database.connect() as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO recent_queries(
                    query_key, definition_version, query_mode, definition_json,
                    display_text, normalized_tags, rating, page_size,
                    last_submitted_page, last_used_at
                ) VALUES (
                    'cat-definition', 1, 'standard', 'cat-definition',
                    'cat', '[\"cat\"]', 'general', 40, 1, 'now'
                )
                """
            )

        with sqlite3.connect(database.path) as raw_connection:
            raw_connection.execute("PRAGMA foreign_keys = ON")
            try:
                raw_connection.execute(
                    "INSERT INTO browser_session(id, is_maximized) VALUES (2, 0)"
                )
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError("browser_session accepted a second fixed id")

        try:
            connection.execute(
                """
                INSERT INTO recent_queries(
                    query_key, definition_version, query_mode, definition_json,
                    display_text, normalized_tags, rating, page_size,
                    last_submitted_page, last_used_at
                ) VALUES (
                    'bad-definition', 1, 'standard', 'bad-definition',
                    'bad', '[\"bad\"]', 'general', 201, 1, 'now'
                )
                """
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("recent_queries accepted page_size 201")


def test_version_two_rows_migrate_to_standard_definitions_without_data_loss(
    tmp_path,
    monkeypatch,
):
    original_migrations = database_module.MIGRATIONS
    monkeypatch.setattr(database_module, "MIGRATIONS", original_migrations[:2])
    database = Database(tmp_path / "application.sqlite3")
    assert database.initialize() == 2
    with database.connect() as connection:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO recent_queries(
                    display_text, normalized_tags, rating, page_size,
                    last_submitted_page, last_committed_page,
                    anchor_post_id, anchor_index, anchor_offset,
                    scroll_position, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "cat dog",
                    '["cat","dog"]',
                    "general",
                    40,
                    3,
                    2,
                    900,
                    4,
                    -12.5,
                    320.0,
                    "2026-01-02T03:04:05.000006+00:00",
                ),
            )
            query_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO recent_tags(normalized_tag, use_count, last_used_at)
                VALUES ('cat', 7, '2026-01-02T03:04:05.000006+00:00')
                """
            )
            connection.execute(
                """
                UPDATE browser_session SET
                    window_x = -500,
                    window_y = 20,
                    window_width = 900,
                    window_height = 700,
                    is_maximized = 1,
                    active_query_id = ?
                WHERE id = 1
                """,
                (query_id,),
            )

    monkeypatch.setattr(database_module, "MIGRATIONS", original_migrations)
    assert database.initialize() == 3
    assert database.initialize() == 3

    repository = HistoryRepository(database)
    query = repository.get_query(query_id)
    session = repository.load_session()
    tags = repository.list_tags()
    with database.connect() as connection:
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(browser_session)"
        ).fetchall()
        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

    assert query.id == query_id
    assert query.definition.mode == QueryMode.STANDARD
    assert query.definition.normalized_expression == "cat AND dog"
    assert query.definition.limits.page_size == 40
    assert query.definition.limits.candidate_limit == 1000
    assert query.result_summary is None
    assert query.last_submitted_page == 3
    assert query.last_committed_page == 2
    assert query.anchor_post_id == 900
    assert query.anchor_index == 4
    assert query.anchor_offset == -12.5
    assert query.scroll_position == 320.0
    assert query.last_used_at == "2026-01-02T03:04:05.000006+00:00"
    assert session.active_query_id == query_id
    assert (
        session.geometry.x,
        session.geometry.y,
        session.geometry.width,
        session.geometry.height,
    ) == (-500, 20, 900, 700)
    assert session.is_maximized is True
    assert [(tag.normalized_tag, tag.use_count) for tag in tags] == [("cat", 7)]
    assert tags[0].last_used_at == "2026-01-02T03:04:05.000006+00:00"
    assert [row["table"] for row in foreign_keys] == ["recent_queries"]
    assert foreign_key_errors == []
    compatible = repository.record_accepted_query(
        display_text="dog cat",
        normalized_tags=("dog", "cat"),
        rating="general",
        page_size=40,
        page=1,
        count_tag_usage=False,
    )
    assert compatible.id == query_id


def test_invalid_version_two_query_rolls_back_python_version_three_migration(
    tmp_path,
    monkeypatch,
):
    original_migrations = database_module.MIGRATIONS
    monkeypatch.setattr(database_module, "MIGRATIONS", original_migrations[:2])
    database = Database(tmp_path / "application.sqlite3")
    assert database.initialize() == 2
    with database.connect() as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO recent_queries(
                    display_text, normalized_tags, rating, page_size,
                    last_submitted_page, last_used_at
                ) VALUES ('bad', '["dog","cat"]', 'all', 40, 1, 'now')
                """
            )

    monkeypatch.setattr(database_module, "MIGRATIONS", original_migrations)
    with pytest.raises(DatabaseError, match="Unable to initialize"):
        database.initialize()

    with database.connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(recent_queries)")
        }
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]

    assert "query_key" not in columns
    assert versions == [1, 2]


def test_failed_version_three_rebuild_rolls_back_old_tables_and_rows(
    tmp_path,
    monkeypatch,
):
    original_migrations = database_module.MIGRATIONS
    monkeypatch.setattr(database_module, "MIGRATIONS", original_migrations[:2])
    database = Database(tmp_path / "application.sqlite3")
    assert database.initialize() == 2
    with database.connect() as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO recent_queries(
                    display_text, normalized_tags, rating, page_size,
                    last_submitted_page, last_used_at
                ) VALUES ('cat', '["cat"]', 'all', 40, 1, 'now')
                """
            )

    failing = Migration(
        version=3,
        name="failing_version_three",
        statements=(
            "CREATE TABLE partial_version_three(value TEXT)",
            "THIS IS NOT VALID SQL",
        ),
    )
    monkeypatch.setattr(
        database_module,
        "MIGRATIONS",
        original_migrations[:2] + (failing,),
    )

    with pytest.raises(DatabaseError, match="Unable to initialize"):
        database.initialize()

    with database.connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(recent_queries)")
        }
        row_count = connection.execute(
            "SELECT COUNT(*) AS count FROM recent_queries"
        ).fetchone()["count"]
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        partial = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'partial_version_three'
            """
        ).fetchone()

    assert "query_key" not in columns
    assert row_count == 1
    assert [row["version"] for row in versions] == [1, 2]
    assert partial is None


def test_failed_migration_rolls_back_its_partial_schema(tmp_path, monkeypatch):
    path = tmp_path / "application.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE app_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_migrations(version, name, applied_at)
            VALUES (1, 'initial_application_metadata', '2026-01-01T00:00:00Z');
            """
        )
    failing = Migration(
        version=2,
        name="failing_version_two",
        statements=(
            "CREATE TABLE partial_version_two(value TEXT)",
            "THIS IS NOT VALID SQL",
        ),
    )
    monkeypatch.setattr(
        database_module,
        "MIGRATIONS",
        (database_module.MIGRATIONS[0], failing),
    )

    with pytest.raises(DatabaseError, match="Unable to initialize"):
        Database(path).initialize()

    with sqlite3.connect(path) as connection:
        table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'partial_version_two'
            """
        ).fetchone()
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()

    assert table is None
    assert versions == [(1,)]
