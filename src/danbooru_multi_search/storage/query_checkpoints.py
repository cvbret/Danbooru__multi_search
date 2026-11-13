"""Revision-checked checkpoint ownership, attached to history transactions."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import sqlite3
from uuid import uuid4

from danbooru_multi_search.danbooru.query_checkpoint import CheckpointError, CheckpointRecord, QueryCheckpoint, source_identity
from danbooru_multi_search.storage.checkpoint_serialization import CHECKPOINT_VERSION, decode_checkpoint, encode_checkpoint
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.query_serialization import encode_query_definition


@dataclass(frozen=True, slots=True)
class CheckpointCleanupIdentity:
    history_id: int
    owner: str
    view_revision: int


class QueryCheckpointRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    @contextmanager
    def _connection(self, *, write=False):
        connection = None
        try:
            connection = self._database.connect(busy_timeout_ms=0)
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit() if write else connection.rollback()
        except (sqlite3.Error, OSError) as exc:
            if connection is not None:
                connection.rollback()
            raise CheckpointError("storage_error", f"检查点保存或读取失败：{exc}") from exc
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def get_for_history(self, history_id: int) -> CheckpointRecord | None:
        if type(history_id) is not int or history_id < 1:
            raise CheckpointError("invalid_identifier", "历史编号无效。")
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM query_checkpoints WHERE history_id = ?", (history_id,)).fetchone()
            return _read(row) if row is not None else None

    @staticmethod
    def require_current(connection: sqlite3.Connection, expected: CheckpointRecord) -> None:
        row = connection.execute("SELECT * FROM query_checkpoints WHERE history_id = ?", (expected.history_id,)).fetchone()
        if row is None or _read(row) != expected:
            raise CheckpointError("stale_checkpoint", "检查点归属或修订已改变，请重新选择历史。")

    def save_in_transaction(self, connection: sqlite3.Connection, history_id: int, owner: str, view_revision: int,
                            checkpoint: QueryCheckpoint, *, expected: CheckpointRecord | None = None,
                            source_expected: CheckpointRecord | None = None) -> CheckpointRecord:
        """Use the caller's history transaction; never open a second writer."""
        if expected is not None:
            self.require_current(connection, expected)
            if expected.history_id != history_id or expected.owner != owner or expected.view_revision != view_revision:
                raise CheckpointError("stale_checkpoint", "检查点更新归属不匹配。")
        if source_expected is not None:
            self.require_current(connection, source_expected)
        history = connection.execute("SELECT definition_json FROM recent_queries WHERE id = ?", (history_id,)).fetchone()
        if history is None or history[0] != encode_query_definition(checkpoint.definition):
            raise CheckpointError("stale_checkpoint", "历史已经删除或查询条件已改变。")
        existing = connection.execute("SELECT revision FROM query_checkpoints WHERE history_id = ?", (history_id,)).fetchone()
        revision = existing[0] + 1 if existing is not None else 0
        value = CheckpointRecord(history_id, owner, view_revision, revision, checkpoint)
        connection.execute("""INSERT INTO query_checkpoints(history_id, owner, view_revision, revision, version, source, checkpoint_json)
            VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(history_id) DO UPDATE SET
            owner=excluded.owner, view_revision=excluded.view_revision, revision=excluded.revision,
            version=excluded.version, source=excluded.source, checkpoint_json=excluded.checkpoint_json""",
            (history_id, owner, view_revision, revision, CHECKPOINT_VERSION, checkpoint.source, encode_checkpoint(checkpoint)))
        return value

    def claim(self, expected: CheckpointRecord, source: str) -> CheckpointRecord:
        with self._connection(write=True) as connection:
            self.require_current(connection, expected)
            if expected.checkpoint.source != source_identity(source):
                raise CheckpointError("source_changed", "当前 API 来源与检查点不同，不能续扫。")
            reason = expected.checkpoint.execution.resume_reason
            if reason:
                raise CheckpointError("cannot_resume", reason)
            # Claim changes the owner in the same transaction that checks the old revision.
            value = CheckpointRecord(expected.history_id, uuid4().hex, 0, expected.revision + 1, expected.checkpoint)
            connection.execute("UPDATE query_checkpoints SET owner = ?, view_revision = 0, revision = ? WHERE history_id = ?", (value.owner, value.revision, value.history_id))
            return value

    @staticmethod
    def _cleanup_identities(connection: sqlite3.Connection) -> tuple[CheckpointCleanupIdentity, ...]:
        return tuple(CheckpointCleanupIdentity(*tuple(row)) for row in connection.execute(
            "SELECT history_id, owner, view_revision FROM query_checkpoints ORDER BY history_id"))

    def capture_cleanup(self) -> tuple[CheckpointCleanupIdentity, ...]:
        with self._connection() as connection:
            return self._cleanup_identities(connection)

    def clear_checkpoints(self, *, expected: tuple[CheckpointCleanupIdentity, ...] | None = None) -> int:
        with self._connection(write=True) as connection:
            if expected is not None and self._cleanup_identities(connection) != expected:
                raise CheckpointError("stale_cleanup", "检查点归属已改变，请重新打开清理确认。")
            count = connection.execute("SELECT COUNT(*) FROM query_checkpoints").fetchone()[0]
            connection.execute("DELETE FROM query_checkpoints")
            return count


def _read(row: sqlite3.Row) -> CheckpointRecord:
    try:
        if type(row["version"]) is not int or row["version"] != CHECKPOINT_VERSION:
            raise CheckpointError("unsupported_checkpoint", "检查点版本不受支持，不能续扫。")
        checkpoint = decode_checkpoint(row["checkpoint_json"])
        if row["source"] != checkpoint.source:
            raise ValueError("source mismatch")
        return CheckpointRecord(row["history_id"], row["owner"], row["view_revision"], row["revision"], checkpoint)
    except CheckpointError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise CheckpointError("invalid_checkpoint", f"检查点记录损坏，不能续扫：{exc}") from exc
