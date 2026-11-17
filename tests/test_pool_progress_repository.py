from dataclasses import replace
import sqlite3
import time

import pytest

from danbooru_multi_search.core.pool_progress import PoolProgressError
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.pool_progress import PoolProgressRepository
from tests.test_pool_progress import progress


def repository(tmp_path):
    database = Database(tmp_path / "reading.sqlite3")
    database.initialize()
    return PoolProgressRepository(database)


def test_reading_progress_restarts_and_keeps_source_pool_identity(tmp_path):
    store = repository(tmp_path)
    assert store.get("https://example.invalid/api/", 7) is None
    saved = store.save(progress(), expected=None)
    reopened = PoolProgressRepository(Database(store._database.path))
    assert reopened.get(saved.source, 7) == saved
    assert reopened.get("https://other.invalid/api", 7) is None
    assert reopened.get(saved.source, 8) is None
    assert reopened.save(replace(saved, page_number=4), expected=saved).revision == 1


def test_first_insert_and_later_updates_reject_stale_confirmed_records(tmp_path):
    store = repository(tmp_path)
    other = PoolProgressRepository(store._database)
    first = store.save(progress(), expected=None)
    with pytest.raises(PoolProgressError, match="其他操作"):
        other.save(progress(page_number=1), expected=None)
    second = other.save(replace(first, page_number=5), expected=first)
    with pytest.raises(PoolProgressError):
        store.save(replace(first, page_number=2), expected=first)
    assert store.get(first.source, 7) == second


def test_busy_writer_returns_immediately_without_changing_confirmed_position(tmp_path):
    store = repository(tmp_path)
    first = store.save(progress(), expected=None)
    connection = store._database.connect()
    connection.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        with pytest.raises(PoolProgressError, match="locked"):
            store.save(replace(first, page_number=4), expected=first)
        assert time.monotonic() - started < 0.5
        assert store.get(first.source, 7) == first
    finally:
        connection.rollback()
        connection.close()


def test_commit_failure_rolls_back_and_keeps_old_record(tmp_path, monkeypatch):
    store = repository(tmp_path)
    first = store.save(progress(), expected=None)
    original = store._database.connect

    class FailCommit:
        def __init__(self):
            self.connection = original(busy_timeout_ms=0)
        def execute(self, *args):
            return self.connection.execute(*args)
        def commit(self):
            raise sqlite3.OperationalError("disk is full")
        def close(self):
            self.connection.close()

    with monkeypatch.context() as changed:
        changed.setattr(store._database, "connect", lambda **kwargs: FailCommit())
        with pytest.raises(PoolProgressError, match="disk is full"):
            store.save(replace(first, page_number=4), expected=first)
    assert store.get(first.source, 7) == first


def test_corrupted_record_is_a_read_error_instead_of_missing_progress(tmp_path):
    store = repository(tmp_path)
    saved = store.save(progress(), expected=None)
    with store._database.connect() as connection:
        connection.execute("UPDATE pool_reading_progress SET updated_at='broken'")
    with pytest.raises(PoolProgressError, match="损坏"):
        store.get(saved.source, 7)
