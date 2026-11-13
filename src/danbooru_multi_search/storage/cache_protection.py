"""Persistent redirect aliases and consistent saved-image protection facts."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import sqlite3
from typing import Iterator
from urllib.parse import urlsplit

from danbooru_multi_search.core.favorites import FavoriteError
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.favorites import FavoriteRepository


class CacheProtectionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CacheProtectionSnapshot:
    favorite_revision: int
    urls: frozenset[str]
    aliases: tuple[tuple[str, str], ...]
    alias_revision: int = 0


def _url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if not isinstance(value, str) or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.fragment:
            raise ValueError("invalid remote image URL")
        parsed.port
        return value
    except (TypeError, ValueError, AttributeError) as exc:
        raise CacheProtectionError(f"图片缓存地址记录无效：{exc}") from exc


class CacheProtectionRepository:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._favorites = FavoriteRepository(database)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = self._database.connect(busy_timeout_ms=0)
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit() if write else connection.rollback()
        except (sqlite3.Error, OSError, FavoriteError) as exc:
            if connection is not None:
                connection.rollback()
            raise CacheProtectionError(f"收藏图片保护信息无法读取或保存：{exc}") from exc
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def _snapshot(self, connection: sqlite3.Connection, previous: CacheProtectionSnapshot | None = None) -> CacheProtectionSnapshot:
        row = connection.execute("SELECT revision FROM favorite_collection_state WHERE id = 1").fetchone()
        if row is None:
            raise CacheProtectionError("收藏修订记录缺失，已保留现有图片缓存。")
        revision = row[0]
        if type(revision) is not int or revision < 0:
            raise CacheProtectionError("收藏修订记录无效，已保留现有图片缓存。")
        alias_revision = connection.execute("SELECT COALESCE(MAX(rowid), 0) FROM image_cache_redirects").fetchone()[0]
        aliases = previous.aliases if previous is not None and previous.alias_revision == alias_revision else tuple((_url(row[0]), _url(row[1])) for row in connection.execute(
            "SELECT original_url, final_url FROM image_cache_redirects ORDER BY original_url, final_url"))
        urls = previous.urls if previous is not None and previous.favorite_revision == revision else self._favorites.image_cache_urls(connection=connection)
        return CacheProtectionSnapshot(revision, urls, aliases, alias_revision)

    def load_snapshot(self) -> CacheProtectionSnapshot:
        with self._connection() as connection:
            return self._snapshot(connection)

    @contextmanager
    def locked_snapshot(self, previous: CacheProtectionSnapshot | None = None) -> Iterator[CacheProtectionSnapshot]:
        """Keep the favorite facts fixed while Qt removes cache entries."""
        with self._connection(write=True) as connection:
            yield self._snapshot(connection, previous)

    def remember_redirect(self, original: str, final: str) -> None:
        original, final = _url(original), _url(final)
        if original == final:
            return
        with self._connection(write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO image_cache_redirects(original_url, final_url) VALUES (?, ?)", (original, final))
