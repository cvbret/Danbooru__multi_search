from __future__ import annotations

import sqlite3

import pytest

import danbooru_multi_search.storage.database as database_module
from danbooru_multi_search.storage.database import Database, DatabaseError, Migration


def test_database_applies_migrations_once(tmp_path):
    database = Database(tmp_path / "application.sqlite3")

    assert database.initialize() == 2
    assert database.initialize() == 2

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

    assert database.initialize() == 2
    assert database.initialize() == 2
    with database.connect() as connection:
        preserved = connection.execute(
            "SELECT value FROM app_metadata WHERE key = 'preserved'"
        ).fetchone()
        migration_count = connection.execute(
            "SELECT COUNT(*) AS count FROM schema_migrations WHERE version = 2"
        ).fetchone()["count"]

    assert preserved["value"] == "yes"
    assert migration_count == 1


def test_version_two_schema_enforces_single_session_and_query_constraints(tmp_path):
    database = Database(tmp_path / "application.sqlite3")
    database.initialize()

    with database.connect() as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO recent_queries(
                    display_text, normalized_tags, rating, page_size,
                    last_submitted_page, last_used_at
                ) VALUES ('cat', '[\"cat\"]', 'general', 40, 1, 'now')
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
                    display_text, normalized_tags, rating, page_size,
                    last_submitted_page, last_used_at
                ) VALUES ('bad', '[\"bad\"]', 'general', 201, 1, 'now')
                """
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("recent_queries accepted page_size 201")


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

