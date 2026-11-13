"""Canonical complete query definitions and revision-checked named records."""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from typing import Iterator
from uuid import uuid4

from danbooru_multi_search.core.saved_queries import SavedQuery, SavedQueryDraft, SavedQueryError, saved_query_name
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.query_serialization import QUERY_DEFINITION_VERSION, decode_query_definition, encode_query_definition


class SavedQueryRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def list_queries(self, *, limit: int = 50, offset: int = 0) -> tuple[SavedQuery, ...]:
        if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
            raise SavedQueryError("invalid_pagination", "常用查询分页范围无效。")
        with self._connection() as connection:
            return tuple(_read(row) for row in connection.execute(
                "SELECT * FROM saved_queries ORDER BY normalized_name, id LIMIT ? OFFSET ?", (limit, offset)))

    def create(self, draft: SavedQueryDraft) -> SavedQuery:
        if not isinstance(draft, SavedQueryDraft):
            raise SavedQueryError("invalid_definition", "常用查询内容无效。")
        encoded = encode_query_definition(draft.definition)
        identifier = uuid4().hex
        with self._connection(write=True) as connection:
            self._check_name(connection, draft.name)
            connection.execute("""INSERT INTO saved_queries(id, revision, name, normalized_name, definition_version, definition_json)
                VALUES (?, 0, ?, ?, ?, ?)""", (identifier, draft.name, saved_query_name(draft.name)[1], QUERY_DEFINITION_VERSION, encoded))
            return SavedQuery(identifier, 0, draft.name, draft.definition)

    def rename(self, expected: SavedQuery, name: str) -> SavedQuery:
        display, key = saved_query_name(name)
        with self._connection(write=True) as connection:
            self._require_current(connection, expected)
            self._check_name(connection, display, excluding=expected.query_id)
            if display == expected.name:
                return expected
            if expected.revision == 2**63 - 1:
                raise SavedQueryError("revision_exhausted", "常用查询修订号已达到上限。")
            connection.execute("UPDATE saved_queries SET name = ?, normalized_name = ?, revision = revision + 1 WHERE id = ?",
                               (display, key, expected.query_id))
            return SavedQuery(expected.query_id, expected.revision + 1, display, expected.definition)

    def delete(self, expected: SavedQuery) -> None:
        with self._connection(write=True) as connection:
            self._require_current(connection, expected)
            connection.execute("DELETE FROM saved_queries WHERE id = ?", (expected.query_id,))

    @staticmethod
    def _require_current(connection: sqlite3.Connection, expected: SavedQuery) -> None:
        if not isinstance(expected, SavedQuery):
            raise SavedQueryError("invalid_record", "常用查询记录无效。")
        row = connection.execute("SELECT * FROM saved_queries WHERE id = ?", (expected.query_id,)).fetchone()
        if row is None or _read(row) != expected:
            raise SavedQueryError("stale_query", "常用查询已改变，请刷新后重新选择。")

    @staticmethod
    def _check_name(connection: sqlite3.Connection, name: str, *, excluding: str = "") -> None:
        if connection.execute("SELECT 1 FROM saved_queries WHERE normalized_name = ? AND id != ?",
                              (saved_query_name(name)[1], excluding)).fetchone():
            raise SavedQueryError("duplicate_name", "已经存在同名常用查询，请使用其他名称。")

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = self._database.connect()
            connection.execute("PRAGMA busy_timeout = 0")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            if write:
                connection.commit()
            else:
                connection.rollback()
        except (sqlite3.Error, OSError) as exc:
            if connection is not None:
                connection.rollback()
            raise SavedQueryError("storage_error", f"常用查询无法保存或读取：{exc}") from exc
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()


def _read(row: sqlite3.Row) -> SavedQuery:
    try:
        if type(row["definition_version"]) is not int or row["definition_version"] != QUERY_DEFINITION_VERSION:
            raise ValueError("unsupported saved query definition version")
        definition = decode_query_definition(row["definition_json"])
        value = SavedQuery(row["id"], row["revision"], row["name"], definition)
        if (value.name != row["name"] or saved_query_name(value.name)[1] != row["normalized_name"]
                or encode_query_definition(definition) != row["definition_json"]):
            raise ValueError("stored query is not canonical")
        return value
    except (SavedQueryError, TypeError, ValueError, OverflowError) as exc:
        raise SavedQueryError("invalid_record", f"保存的常用查询无效：{exc}") from exc
