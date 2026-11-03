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
                with connection:
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

