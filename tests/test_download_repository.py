from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3

import pytest

from danbooru_multi_search.core.downloads import DownloadConflictPolicy, DownloadError, DownloadRequest, DownloadTaskState
from danbooru_multi_search.storage.downloads import DownloadRepository
from danbooru_multi_search.storage.database import Database


def repository(tmp_path):
    database = Database(tmp_path / "application.sqlite3")
    database.initialize()
    return DownloadRepository(database)


def request(identifier=321):
    return DownloadRequest(identifier, f"https://example.invalid/{identifier}.jpg", "jpg")


def test_connection_requests_zero_busy_timeout_before_wal_initialization(tmp_path, monkeypatch):
    saved = repository(tmp_path)
    connect = saved._database.connect
    values = []
    def check(*, busy_timeout_ms=5000):
        values.append(busy_timeout_ms)
        return connect(busy_timeout_ms=busy_timeout_ms)
    monkeypatch.setattr(saved._database, 'connect', check)
    with connect() as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        with pytest.raises(DownloadError, match='locked'):
            saved.create_batch((request(),), tmp_path)
        blocker.rollback()
    assert values == [0] and saved.list_batches() == ()


def test_finite_batch_is_saved_in_order_before_any_runtime_work(tmp_path):
    saved = repository(tmp_path)
    batch = saved.create_batch((request(5), request(3), request(5)), tmp_path / "中文 下载")
    reopened = DownloadRepository(Database(saved._database.path)).get_batch(batch.batch_id)
    assert reopened == batch
    assert [item.request.post_id for item in reopened.items] == [5, 3, 5]
    assert [item.position for item in reopened.items] == [0, 1, 2]
    assert len({item.item_id for item in reopened.items}) == 3
    assert not (tmp_path / "中文 下载").exists()


@pytest.mark.parametrize("values", [(), tuple(request() for _ in range(1001)), iter((request(),)), (request(), object())])
def test_invalid_batch_has_no_partial_persistent_rows(tmp_path, values):
    saved = repository(tmp_path)
    with pytest.raises(DownloadError):
        saved.create_batch(values, tmp_path)
    assert saved.list_batches() == ()


def test_insert_failure_rolls_back_whole_batch(tmp_path):
    saved = repository(tmp_path)
    with saved._database.connect() as connection:
        connection.execute("CREATE TRIGGER reject_second BEFORE INSERT ON download_items WHEN NEW.position = 1 BEGIN SELECT RAISE(ABORT, 'failure'); END")
    with pytest.raises(DownloadError):
        saved.create_batch((request(1), request(2)), tmp_path)
    assert saved.list_batches() == ()


def test_recovery_keeps_decided_target_old_partial_progress_and_cumulative_attempts(tmp_path):
    saved = repository(tmp_path)
    original = saved.create_batch((request(),), tmp_path, DownloadConflictPolicy.AUTO_NUMBER).items[0]
    current = saved.set_destination(original.item_id, original.generation, "321 (1).jpg")
    current = saved.begin_attempt(current.item_id, current.generation)
    name = f".download-{current.item_id}-{current.generation}.part"
    current = saved.register_partial(current.item_id, current.generation, name)
    current = saved.update_progress(current.item_id, current.generation, 5, 10)
    (tmp_path / name).write_bytes(b"partial")
    (tmp_path / "321 (1).jpg").write_bytes(b"complete")
    saved.recover_unfinished()
    paused = saved.get_item(current.item_id)
    assert paused.state is DownloadTaskState.AWAITING_RESUME
    assert paused.partial_name == name and paused.resolved_name == "321 (1).jpg"
    assert paused.bytes_written == 5 and paused.attempts == 1
    assert (tmp_path / name).read_bytes() == b"partial" and (tmp_path / "321 (1).jpg").read_bytes() == b"complete"
    with pytest.raises(DownloadError) as caught:
        saved.update_progress(current.item_id, current.generation, 9, 10)
    assert caught.value.code == "stale_download"
    retried = saved.prepare_retry(paused.item_id, paused.generation)
    assert retried.bytes_written == 0 and retried.total_bytes is None and retried.attempts == 1
    second = saved.begin_attempt(retried.item_id, retried.generation)
    assert second.attempts == 2 and second.generation > current.generation


@pytest.mark.parametrize("field,value", [
    ("position", 0.5), ("bytes_written", 1.5), ("attempts", 1.5), ("generation", 1.5),
    ("total_bytes", 1.5), ("total_bytes", 1),
    ("partial_name", "321.jpg"), ("partial_name", ".download-" + "0" * 32 + "-1.part"),
    ("partial_name", "future"), ("resolved_name", "other.jpg"),
])
def test_corrupt_rows_are_rejected_without_exposing_file_or_numeric_values(tmp_path, field, value):
    saved = repository(tmp_path)
    current = saved.create_batch((request(),), tmp_path).items[0]
    current = saved.begin_attempt(current.item_id, current.generation)
    saved.update_progress(current.item_id, current.generation, 2, 4)
    if value == "future":
        value = f".download-{current.item_id}-{current.generation + 1}.part"
    with saved._database.connect() as connection:
        connection.execute(f"UPDATE download_items SET {field} = ? WHERE id = ?", (value, current.item_id))
    with pytest.raises(DownloadError) as caught:
        saved.get_item(current.item_id)
    assert caught.value.code == "invalid_record"


def test_open_database_failure_uses_stable_storage_error(tmp_path, monkeypatch):
    saved = repository(tmp_path)
    def fail():
        raise sqlite3.OperationalError("database unavailable")
    monkeypatch.setattr(saved._database, "connect", fail)
    with pytest.raises(DownloadError) as caught:
        saved.list_batches()
    assert caught.value.code == "storage_error"


def test_batch_generation_guard_is_checked_inside_write_transaction(tmp_path):
    saved = repository(tmp_path)
    batch = saved.create_batch((request(1), request(2)), tmp_path)
    expected = {item.item_id: item.generation for item in batch.items}
    started = saved.begin_attempt(batch.items[0].item_id, 0)
    with pytest.raises(DownloadError) as caught:
        saved.cancel_batch(batch.batch_id, expected_generations=expected)
    assert caught.value.code == "stale_download"
    assert saved.get_item(started.item_id).state is DownloadTaskState.RUNNING
    assert saved.get_item(batch.items[1].item_id).state is DownloadTaskState.WAITING


def test_batch_summaries_aggregate_without_decoding_item_rows(tmp_path, monkeypatch):
    import danbooru_multi_search.storage.downloads as module
    saved = repository(tmp_path)
    batch = saved.create_batch(tuple(request(index + 1) for index in range(1000)), tmp_path)
    item = saved.begin_attempt(batch.items[0].item_id, 0)
    saved.update_progress(item.item_id, item.generation, 10, 20)
    monkeypatch.setattr(module, "_item", lambda _row: pytest.fail("summaries must not decode complete items"))
    summary = saved.read_batch_summaries(limit=1)[0]
    assert summary.batch_id == batch.batch_id and summary.count == 1000
    assert dict(summary.state_counts)["waiting"] == 999 and summary.bytes_written == 10
    assert summary.total_bytes is None
