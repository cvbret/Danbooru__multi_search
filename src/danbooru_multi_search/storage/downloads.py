"""Persistent finite download batches and generation-checked state changes."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from danbooru_multi_search.core.downloads import (
    MAX_DOWNLOAD_BATCH_ITEMS, DOWNLOAD_TERMINAL_STATES, DownloadBatchSnapshot,
    DownloadConflictPolicy, DownloadError, DownloadItemSnapshot, DownloadRequest, DownloadTaskState, filename_key,
)
from danbooru_multi_search.storage.database import Database


@dataclass(frozen=True, slots=True)
class DownloadBatchSummary:
    """Aggregate read projection; obtaining it never decodes all item rows."""
    batch_id: str
    count: int
    state_counts: tuple[tuple[str, int], ...]
    bytes_written: int
    total_bytes: int | None

    @classmethod
    def from_batch(cls, batch: DownloadBatchSnapshot) -> "DownloadBatchSummary":
        return cls(batch.batch_id, len(batch.items), tuple(batch.counts.items()), batch.bytes_written, batch.total_bytes)


class DownloadRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create_batch(self, requests: Sequence[DownloadRequest], directory: Path,
                     policy: DownloadConflictPolicy = DownloadConflictPolicy.SKIP) -> DownloadBatchSnapshot:
        if (not isinstance(requests, Sequence) or isinstance(requests, (str, bytes))
                or not 1 <= len(requests) <= MAX_DOWNLOAD_BATCH_ITEMS):
            raise DownloadError("invalid_batch", "下载批量必须明确包含 1 至 1,000 项。")
        requests = tuple(requests)
        if not all(isinstance(value, DownloadRequest) for value in requests):
            raise DownloadError("invalid_request", "批量下载包含无效作品。")
        if not isinstance(directory, Path) or not directory.is_absolute():
            raise DownloadError("invalid_directory", "下载目录必须使用绝对路径。")
        if not isinstance(policy, DownloadConflictPolicy):
            raise DownloadError("invalid_policy", "下载重名策略无效。")
        with self._connection(write=True) as connection:
            return self.create_batch_in_transaction(connection, requests, directory, policy)

    def create_batch_in_transaction(
        self, connection: sqlite3.Connection, requests: Sequence[DownloadRequest], directory: Path,
        policy: DownloadConflictPolicy,
    ) -> DownloadBatchSnapshot:
        """Insert within a caller-owned transaction; the caller commits related facts."""
        if not connection.in_transaction:
            raise DownloadError("invalid_batch", "创建下载批次需要已开始的事务。")
        if (not isinstance(requests, Sequence) or not 1 <= len(requests) <= MAX_DOWNLOAD_BATCH_ITEMS
                or not all(isinstance(value, DownloadRequest) for value in requests)):
            raise DownloadError("invalid_batch", "下载批量必须包含 1 至 1,000 个有效原文件请求。")
        if not isinstance(directory, Path) or not directory.is_absolute():
            raise DownloadError("invalid_directory", "下载目录必须使用绝对路径。")
        if not isinstance(policy, DownloadConflictPolicy):
            raise DownloadError("invalid_policy", "下载重名策略无效。")
        identifier = uuid4().hex
        connection.execute("INSERT INTO download_batches(id, created_at) VALUES (?, ?)",
                           (identifier, datetime.now(timezone.utc).isoformat()))
        for position, request in enumerate(requests):
            connection.execute(
                """INSERT INTO download_items(id, batch_id, position, post_id, source_url,
                       file_ext, label, directory, policy, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting')""",
                (uuid4().hex, identifier, position, request.post_id, request.source_url,
                 request.file_ext, request.label, str(directory), policy.value),
            )
        return self._batch(connection, identifier)

    def get_item(self, item_id: str) -> DownloadItemSnapshot:
        with self._connection() as connection:
            return _item(self._require(connection, item_id))

    def get_batch(self, batch_id: str) -> DownloadBatchSnapshot:
        with self._connection() as connection:
            return self._batch(connection, batch_id)

    def list_batches(self, *, limit: int = 100, offset: int = 0) -> tuple[str, ...]:
        if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
            raise DownloadError("invalid_pagination", "下载记录分页范围无效。")
        with self._connection() as connection:
            return tuple(row[0] for row in connection.execute(
                "SELECT id FROM download_batches ORDER BY created_at DESC, id LIMIT ? OFFSET ?", (limit, offset)))

    def read_batch_summaries(self, *, limit: int = 50, offset: int = 0) -> tuple[DownloadBatchSummary, ...]:
        if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
            raise DownloadError("invalid_pagination", "下载记录分页范围无效。")
        states = tuple(DownloadTaskState)
        counts = ", ".join(f"SUM(CASE WHEN di.state = '{state.value}' THEN 1 ELSE 0 END) AS {state.value}" for state in states)
        with self._connection() as connection:
            rows = connection.execute(f"""SELECT db.id, COUNT(di.id) AS count,
                COALESCE(SUM(di.bytes_written), 0) AS written,
                CASE WHEN COUNT(di.total_bytes) = COUNT(di.id) THEN SUM(di.total_bytes) ELSE NULL END AS total,
                SUM(CASE WHEN typeof(di.bytes_written) != 'integer' OR di.bytes_written < 0
                    OR (di.total_bytes IS NOT NULL AND (typeof(di.total_bytes) != 'integer'
                        OR di.total_bytes < di.bytes_written)) THEN 1 ELSE 0 END) AS invalid,
                {counts} FROM download_batches db JOIN download_items di ON di.batch_id = db.id
                GROUP BY db.id ORDER BY db.created_at DESC, db.id LIMIT ? OFFSET ?""", (limit, offset)).fetchall()
            if any(row["invalid"] for row in rows):
                raise DownloadError("invalid_record", "下载进度记录无效，无法显示批次摘要。")
            return tuple(DownloadBatchSummary(row["id"], row["count"], tuple((state.value, row[state.value]) for state in states),
                                              row["written"], row["total"]) for row in rows)

    def list_items(self, *, states: Sequence[DownloadTaskState] | None = None) -> tuple[DownloadItemSnapshot, ...]:
        with self._connection() as connection:
            where, parameters = "", ()
            if states is not None:
                if not states:
                    return ()
                if not all(isinstance(state, DownloadTaskState) for state in states):
                    raise DownloadError("invalid_state", "下载状态无效。")
                where = "WHERE di.state IN (" + ",".join("?" for _ in states) + ")"
                parameters = tuple(state.value for state in states)
            rows = connection.execute(
                f"""SELECT di.* FROM download_items di JOIN download_batches db ON di.batch_id = db.id
                    {where} ORDER BY db.created_at, db.id, di.position""", parameters)
            return tuple(_item(row) for row in rows)

    def recover_unfinished(self, *, expected_generations: dict[str, int] | None = None) -> dict[str, int]:
        """Recover records only; neither network nor file operations occur here."""
        with self._connection(write=True) as connection:
            if expected_generations is not None:
                for identifier, generation in expected_generations.items():
                    self._current(connection, identifier, generation)
            connection.execute("""UPDATE download_items SET state = 'awaiting_resume', generation = generation + 1,
                message = '应用已重新启动，等待手动从头恢复。'
                WHERE state IN ('waiting', 'running', 'retry_wait')""")
            return {identifier: self._require(connection, identifier)["generation"] for identifier in (expected_generations or {})}

    def begin_attempt(self, item_id: str, expected_generation: int) -> DownloadItemSnapshot:
        with self._connection(write=True) as connection:
            current = self._current(connection, item_id, expected_generation)
            if current.state not in {DownloadTaskState.WAITING, DownloadTaskState.RETRY_WAIT}:
                raise DownloadError("invalid_state", "此下载当前不能开始。")
            connection.execute("""UPDATE download_items SET state = 'running', generation = generation + 1,
                attempts = attempts + 1, bytes_written = 0, total_bytes = NULL, partial_name = NULL,
                error_code = '', message = '' WHERE id = ?""", (item_id,))
            return _item(self._require(connection, item_id))

    def set_destination(self, item_id: str, expected_generation: int, name: str) -> DownloadItemSnapshot:
        _basename(name)
        return self._update(item_id, expected_generation, {"resolved_name": name})

    def register_partial(self, item_id: str, expected_generation: int, name: str) -> DownloadItemSnapshot:
        if name != f".download-{item_id}-{expected_generation}.part":
            raise DownloadError("invalid_partial", "下载临时文件不属于当前尝试。")
        return self._update(item_id, expected_generation, {"partial_name": name})

    def update_progress(self, item_id: str, expected_generation: int, written: int,
                        total: int | None) -> DownloadItemSnapshot:
        if type(written) is not int or not 0 <= written <= 2**63 - 1:
            raise DownloadError("invalid_progress", "下载进度无效。")
        if total is not None and (type(total) is not int or not written <= total <= 2**63 - 1):
            raise DownloadError("invalid_progress", "下载总字节数无效。")
        return self._update(item_id, expected_generation, {"bytes_written": written, "total_bytes": total})

    def set_retry_wait(self, item_id: str, expected_generation: int, message: str) -> DownloadItemSnapshot:
        return self._update(item_id, expected_generation, {"state": "retry_wait", "message": message})

    def finish(self, item_id: str, expected_generation: int, state: DownloadTaskState, *,
               error_code: str = "", message: str = "", clear_partial: bool = True) -> DownloadItemSnapshot:
        if not isinstance(state, DownloadTaskState) or state not in DOWNLOAD_TERMINAL_STATES:
            raise DownloadError("invalid_state", "下载最终状态无效。")
        values = {"state": state.value, "error_code": error_code, "message": message}
        if clear_partial:
            values["partial_name"] = None
        return self._update(item_id, expected_generation, values)

    def cancel(self, item_id: str, expected_generation: int) -> DownloadItemSnapshot:
        with self._connection(write=True) as connection:
            current = self._current(connection, item_id, expected_generation)
            if current.state in DOWNLOAD_TERMINAL_STATES:
                return current
            connection.execute("""UPDATE download_items SET state = 'cancelled', generation = generation + 1,
                error_code = 'cancelled', message = '用户已取消下载。' WHERE id = ?""", (item_id,))
            return _item(self._require(connection, item_id))

    def cancel_batch(self, batch_id: str, *, expected_generations: dict[str, int] | None = None) -> DownloadBatchSnapshot:
        """Invalidate every unfinished item before any runtime cancellation signal."""
        with self._connection(write=True) as connection:
            self._check_batch_generations(connection, batch_id, expected_generations)
            connection.execute("""UPDATE download_items SET state = 'cancelled', generation = generation + 1,
                error_code = 'cancelled', message = '用户已取消整个下载批量。'
                WHERE batch_id = ? AND state NOT IN ('completed', 'skipped', 'failed', 'cancelled')""", (batch_id,))
            return self._batch(connection, batch_id)

    def prepare_retry(self, item_id: str, expected_generation: int) -> DownloadItemSnapshot:
        with self._connection(write=True) as connection:
            current = self._current(connection, item_id, expected_generation)
            if current.state not in {DownloadTaskState.FAILED, DownloadTaskState.CANCELLED, DownloadTaskState.AWAITING_RESUME}:
                raise DownloadError("invalid_state", "只有失败、取消或等待恢复的下载可以手动重试。")
            connection.execute("""UPDATE download_items SET state = 'waiting', generation = generation + 1,
                bytes_written = 0, total_bytes = NULL, error_code = '', message = '' WHERE id = ?""", (item_id,))
            return _item(self._require(connection, item_id))

    def prepare_batch_resume(self, batch_id: str, expected: tuple[DownloadItemSnapshot, ...], *,
                             expected_generations: dict[str, int] | None = None) -> tuple[DownloadItemSnapshot, ...]:
        """Resume all previously inspected interrupted records in one transaction."""
        with self._connection(write=True) as connection:
            self._check_batch_generations(connection, batch_id, expected_generations)
            current = tuple(item for item in self._batch(connection, batch_id).items if item.state is DownloadTaskState.AWAITING_RESUME)
            if current != expected:
                raise DownloadError("stale_download", "下载批量已改变，请重新选择恢复。")
            connection.execute("""UPDATE download_items SET state = 'waiting', generation = generation + 1,
                bytes_written = 0, total_bytes = NULL, error_code = '', message = ''
                WHERE batch_id = ? AND state = 'awaiting_resume'""", (batch_id,))
            identifiers = {item.item_id for item in expected}
            return tuple(item for item in self._batch(connection, batch_id).items if item.item_id in identifiers)

    def _check_batch_generations(self, connection: sqlite3.Connection, batch_id: str,
                                 expected: dict[str, int] | None) -> None:
        batch = self._batch(connection, batch_id)
        if expected is not None and (any(type(value) is not int for value in expected.values())
                or {item.item_id: item.generation for item in batch.items} != expected):
            raise DownloadError("stale_download", "下载批次已经改变，请重新选择操作。")

    def pause(self, item_id: str, expected_generation: int) -> DownloadItemSnapshot:
        return self._update(item_id, expected_generation, {"state": "awaiting_resume", "generation": expected_generation + 1,
                            "message": "应用关闭，等待手动从头恢复。"})

    def note_cleanup_error(self, item_id: str, expected_generation: int, message: str) -> DownloadItemSnapshot:
        with self._connection(write=True) as connection:
            self._current(connection, item_id, expected_generation)
            connection.execute("UPDATE download_items SET error_code = 'file_cleanup_error', message = ? WHERE id = ?",
                               (message, item_id))
            return _item(self._require(connection, item_id))

    def _update(self, item_id: str, expected_generation: int, values: dict) -> DownloadItemSnapshot:
        with self._connection(write=True) as connection:
            current = self._current(connection, item_id, expected_generation)
            if current.state in DOWNLOAD_TERMINAL_STATES:
                raise DownloadError("stale_download", "下载已经结束。")
            if values.get("state") == "completed" and (not current.resolved_name or current.bytes_written <= 0):
                raise DownloadError("invalid_state", "尚未保存有效下载结果。")
            connection.execute("UPDATE download_items SET " + ", ".join(f"{name} = ?" for name in values) + " WHERE id = ?",
                               (*values.values(), item_id))
            return _item(self._require(connection, item_id))

    @staticmethod
    def _require(connection: sqlite3.Connection, item_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM download_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise DownloadError("unknown_download", "找不到下载任务。")
        return row

    def _current(self, connection: sqlite3.Connection, item_id: str, generation: int) -> DownloadItemSnapshot:
        current = _item(self._require(connection, item_id))
        if type(generation) is not int or current.generation != generation:
            raise DownloadError("stale_download", "下载操作已经失效。")
        return current

    def _batch(self, connection: sqlite3.Connection, batch_id: str) -> DownloadBatchSnapshot:
        if not connection.execute("SELECT 1 FROM download_batches WHERE id = ?", (batch_id,)).fetchone():
            raise DownloadError("unknown_batch", "找不到下载批量。")
        return DownloadBatchSnapshot(batch_id, tuple(_item(row) for row in connection.execute(
            "SELECT * FROM download_items WHERE batch_id = ? ORDER BY position", (batch_id,))))

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with closing(self._open()) as connection:
            try:
                # A concurrent import must produce an explicit storage failure,
                # rather than blocking the GUI thread on SQLite's busy timeout.
                connection.execute("PRAGMA busy_timeout = 0")
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield connection
                if write:
                    connection.commit()
                else:
                    connection.rollback()
            except sqlite3.Error as exc:
                connection.rollback()
                raise DownloadError("storage_error", f"下载记录保存或读取失败：{exc}") from exc
            except Exception:
                connection.rollback()
                raise

    def _open(self) -> sqlite3.Connection:
        try:
            return self._database.connect(busy_timeout_ms=0)
        except (sqlite3.Error, OSError) as exc:
            raise DownloadError("storage_error", f"无法打开下载记录数据库：{exc}") from exc


def _basename(value: str) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 255 or value in {".", ".."} or "/" in value or "\\" in value or Path(value).name != value:
        raise DownloadError("invalid_filename", "下载文件名不能包含目录。")


def _item(row: sqlite3.Row) -> DownloadItemSnapshot:
    try:
        request = DownloadRequest(row["post_id"], row["source_url"], row["file_ext"], row["label"])
        directory = Path(row["directory"])
        if not directory.is_absolute() or not re.fullmatch(r"[a-f0-9]{32}", row["id"]):
            raise ValueError("invalid persistent download identity or directory")
        for field in ("position", "bytes_written", "attempts", "generation"):
            if type(row[field]) is not int or not 0 <= row[field] <= 2**63 - 1:
                raise ValueError(f"{field} must be a non-negative SQLite integer")
        if row["total_bytes"] is not None and (type(row["total_bytes"]) is not int
                or not row["bytes_written"] <= row["total_bytes"] <= 2**63 - 1):
            raise ValueError("total_bytes must be an integer not smaller than bytes_written")
        for field in ("resolved_name", "partial_name"):
            if row[field] is not None:
                _basename(row[field])
        if row["resolved_name"] is not None and filename_key(row["resolved_name"]) != filename_key(request.filename):
            base = Path(request.filename)
            pattern = re.escape(filename_key(base.stem)) + r" \([1-9][0-9]{0,5}\)" + re.escape(filename_key(base.suffix))
            if row["policy"] != "auto_number" or re.fullmatch(pattern, filename_key(row["resolved_name"])) is None:
                raise ValueError("resolved filename does not belong to this download")
        if row["partial_name"] is not None:
            match = re.fullmatch(r"\.download-" + re.escape(row["id"]) + r"-([1-9][0-9]{0,18})\.part", row["partial_name"])
            if match is None or int(match.group(1)) > row["generation"]:
                raise ValueError("partial file does not belong to an existing attempt of this download")
        if row["state"] == "completed" and (not row["resolved_name"] or row["bytes_written"] == 0):
            raise ValueError("completed download has no valid file result")
        return DownloadItemSnapshot(row["id"], row["batch_id"], row["position"], request, directory,
            DownloadConflictPolicy(row["policy"]), DownloadTaskState(row["state"]), row["resolved_name"],
            row["partial_name"], row["bytes_written"], row["total_bytes"], row["attempts"], row["generation"],
            row["error_code"], row["message"])
    except (DownloadError, ValueError, TypeError) as exc:
        raise DownloadError("invalid_record", f"下载记录无效：{exc}") from exc
