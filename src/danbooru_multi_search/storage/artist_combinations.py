"""Transactions for combinations saved independently from favorite records."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from typing import Iterator

from danbooru_multi_search.core.artist_combinations import (
    ArtistCombination, ArtistCombinationDraft, ArtistCombinationError,
    ArtistCombinationMode, combination_name,
)
from danbooru_multi_search.storage.database import Database


class ArtistCombinationRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def list_combinations(self) -> tuple[ArtistCombination, ...]:
        with self._connection() as connection:
            return tuple(_from_row(row) for row in connection.execute(
                "SELECT * FROM artist_combinations ORDER BY normalized_name, id"
            ))

    def create_combination(self, draft: ArtistCombinationDraft) -> ArtistCombination:
        if not isinstance(draft, ArtistCombinationDraft):
            raise ArtistCombinationError("invalid_combination", "画师组合内容无效。")
        with self._connection(write=True) as connection:
            self._check_name(connection, draft.name)
            cursor = connection.execute(
                "INSERT INTO artist_combinations(name, normalized_name, artists_json, mode) VALUES (?, ?, ?, ?)",
                (draft.name, combination_name(draft.name)[1],
                 json.dumps(draft.artists, ensure_ascii=False, separators=(",", ":")), draft.mode.value),
            )
            return ArtistCombination(int(cursor.lastrowid), draft.name, draft.artists, draft.mode)

    def rename_combination(self, current: ArtistCombination, name: str) -> ArtistCombination:
        """Rename only the exact immutable record previously shown to the user."""

        display, key = combination_name(name)
        with self._connection(write=True) as connection:
            self._require_current(connection, current)
            self._check_name(connection, display, excluding=current.combination_id)
            connection.execute("UPDATE artist_combinations SET name = ?, normalized_name = ? WHERE id = ?",
                               (display, key, current.combination_id))
            return ArtistCombination(current.combination_id, display, current.artists, current.mode)

    def delete_combination(self, current: ArtistCombination) -> None:
        with self._connection(write=True) as connection:
            self._require_current(connection, current)
            connection.execute("DELETE FROM artist_combinations WHERE id = ?", (current.combination_id,))

    @staticmethod
    def _check_name(connection: sqlite3.Connection, name: str, *, excluding: int = 0) -> None:
        if connection.execute("SELECT 1 FROM artist_combinations WHERE normalized_name = ? AND id != ?",
                              (combination_name(name)[1], excluding)).fetchone():
            raise ArtistCombinationError("duplicate_name", "已经存在同名画师组合。")

    @staticmethod
    def _require_current(connection: sqlite3.Connection, current: ArtistCombination) -> None:
        if not isinstance(current, ArtistCombination):
            raise ArtistCombinationError("invalid_combination", "画师组合内容无效。")
        row = connection.execute("SELECT * FROM artist_combinations WHERE id = ?",
                                 (current.combination_id,)).fetchone()
        if row is None or _from_row(row) != current:
            raise ArtistCombinationError("stale_combination", "画师组合已改变，请刷新后重新选择。")

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with closing(self._database.connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield connection
                if write:
                    connection.commit()
                else:
                    connection.rollback()
            except sqlite3.Error as exc:
                connection.rollback()
                raise ArtistCombinationError("storage_error", f"画师组合数据库操作失败：{exc}") from exc
            except Exception:
                connection.rollback()
                raise


def _from_row(row: sqlite3.Row) -> ArtistCombination:
    try:
        artists = json.loads(row["artists_json"])
        if not isinstance(artists, list):
            raise ValueError("artists_json must be a list")
        value = ArtistCombination(row["id"], row["name"], tuple(artists), ArtistCombinationMode(row["mode"]))
        if value.artists != tuple(artists) or value.name != row["name"] or combination_name(value.name)[1] != row["normalized_name"]:
            raise ValueError("stored combination is not canonical")
        return value
    except (ValueError, TypeError, ArtistCombinationError) as exc:
        raise ArtistCombinationError("invalid_record", f"保存的画师组合无效：{exc}") from exc
