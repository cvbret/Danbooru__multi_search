"""Nonblocking, revision-checked reading progress transactions."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
import sqlite3
from typing import Iterator

from danbooru_multi_search.core.pool_progress import PoolProgressError, PoolReadingProgress, normalize_api_source
from danbooru_multi_search.core.pool_reader import ReaderMode
from danbooru_multi_search.storage.database import Database


class PoolProgressRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self, source: str, pool_id: int) -> PoolReadingProgress | None:
        """Read confirmed progress; absence and unreadable data stay distinct."""
        source = normalize_api_source(source)
        if type(pool_id) is not int or not 1 <= pool_id <= 2**63 - 1:
            raise PoolProgressError("invalid_progress", "作品池编号无效。")
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM pool_reading_progress WHERE source = ? AND pool_id = ?", (source, pool_id)).fetchone()
            return _read(row) if row else None

    def save(
        self, value: PoolReadingProgress, *, expected: PoolReadingProgress | None,
    ) -> PoolReadingProgress:
        """Commit only against the exact previously read record, or known absence."""
        if not isinstance(value, PoolReadingProgress) or (expected is not None and not isinstance(expected, PoolReadingProgress)):
            raise PoolProgressError("invalid_progress", "阅读进度记录无效。")
        if expected and (expected.source, expected.pool_id) != (value.source, value.pool_id):
            raise PoolProgressError("invalid_progress", "阅读进度来源已改变。")
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT * FROM pool_reading_progress WHERE source = ? AND pool_id = ?", (value.source, value.pool_id)).fetchone()
            current = _read(row) if row else None
            if current != expected:
                raise PoolProgressError("stale_progress", "阅读进度已由其他操作更新，请重新读取。")
            if current and current.revision == 2**63 - 1:
                raise PoolProgressError("revision_exhausted", "阅读进度修订号已达到上限。")
            saved = replace(value, revision=current.revision + 1 if current else 0)
            connection.execute("""INSERT INTO pool_reading_progress
                (source, pool_id, post_id, page_number, updated_at, offset, viewport_width, viewport_height, zoom, mode, scale_mode, revision)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, pool_id) DO UPDATE SET
                post_id=excluded.post_id, page_number=excluded.page_number, updated_at=excluded.updated_at,
                offset=excluded.offset, viewport_width=excluded.viewport_width, viewport_height=excluded.viewport_height, zoom=excluded.zoom,
                mode=excluded.mode, scale_mode=excluded.scale_mode, revision=excluded.revision""",
                (saved.source, saved.pool_id, saved.post_id, saved.page_number,
                 saved.updated_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                 saved.offset, saved.viewport_width, saved.viewport_height, saved.zoom, saved.mode.value, saved.scale_mode, saved.revision))
            return saved

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = self._database.connect(busy_timeout_ms=0)
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            if write:
                connection.commit()
        except PoolProgressError:
            raise
        except (sqlite3.Error, ValueError, TypeError, OverflowError, OSError) as exc:
            raise PoolProgressError("storage_error", f"阅读进度存储失败：{exc}") from exc
        finally:
            if connection is not None:
                connection.close()


def _read(row: sqlite3.Row) -> PoolReadingProgress:
    try:
        return PoolReadingProgress(
            row["source"], row["pool_id"], row["post_id"], row["page_number"],
            datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")),
            row["offset"], row["viewport_width"], row["viewport_height"], row["zoom"], ReaderMode(row["mode"]),
            row["scale_mode"], row["revision"],
        )
    except (ValueError, TypeError, OverflowError, AttributeError) as exc:
        raise PoolProgressError("invalid_record", f"阅读进度记录损坏：{exc}") from exc
