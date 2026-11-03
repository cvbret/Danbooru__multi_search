from __future__ import annotations

from danbooru_multi_search.storage.database import Database


def test_database_applies_first_migration_once(tmp_path):
    database = Database(tmp_path / "application.sqlite3")

    assert database.initialize() == 1
    assert database.initialize() == 1

    with database.connect() as connection:
        migrations = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        metadata_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'app_metadata'"
        ).fetchone()

    assert [(row["version"], row["name"]) for row in migrations] == [
        (1, "initial_application_metadata")
    ]
    assert metadata_table["name"] == "app_metadata"

