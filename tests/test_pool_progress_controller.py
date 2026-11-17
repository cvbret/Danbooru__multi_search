from dataclasses import replace

import pytest

from danbooru_multi_search.core.pool_progress import PoolProgressError
from danbooru_multi_search.core.pools import DanbooruPool, PoolCategory
from danbooru_multi_search.storage.pool_progress import PoolProgressRepository
from danbooru_multi_search.ui.pool_progress import PoolProgressController
from tests.test_pool_reader import reader, pool, slots
from tests.test_pool_progress import progress


@pytest.fixture
def saved_reader(reader):
    value = reader
    store = PoolProgressRepository(value.repository._database)
    saved = store.save(progress(source="https://example.invalid"), expected=None)
    coordinator = PoolProgressController(store, value.controller, "https://example.invalid/?private=key")
    value.progress_store, value.progress_controller, value.saved_progress = store, coordinator, saved
    yield value
    coordinator.begin_shutdown()
    coordinator.finish_shutdown()


def open_saved(value, *, ids=(1, 2, 9, 4, 5)):
    metadata = pool(ids=ids)
    assert value.controller.open_pool(metadata, slots(metadata, range(1, len(ids) + 1)))
    value.controller.setActive(True)


def layout(value, width=800, height=600):
    reader = value.controller
    plan = reader.preparePosition(width, height, reader.readingSession, reader.revision)
    assert plan["valid"]
    reader.confirmPosition(reader.readingSession, reader.revision)
    return plan


def test_restore_waits_for_layout_and_same_page_scroll_saves_without_changed(saved_reader):
    value = saved_reader
    open_saved(value)
    reader = value.controller
    assert reader.currentPage == 3 and not reader.position_ready
    reader.rememberScroll(3, 0)
    assert not value.progress_controller.flush()
    assert value.progress_store.get(value.saved_progress.source, 7) == value.saved_progress
    assert layout(value)["offset"] == 123
    reader.rememberScroll(3, 321)
    assert value.progress_controller._timer.isActive()
    assert value.progress_controller.flush()
    assert value.progress_store.get(value.saved_progress.source, 7).offset == 321


def test_read_failure_never_saves_first_page_and_explicit_retry_restores(saved_reader, monkeypatch):
    value = saved_reader
    original = value.progress_store.get
    def fail(*args):
        raise PoolProgressError("storage_error", "simulated read failure")
    with monkeypatch.context() as failed:
        failed.setattr(value.progress_store, "get", fail)
        open_saved(value)
        assert value.controller.currentPage == 1
        layout(value)
        value.controller.restartFromBeginning(value.controller.revision)
        layout(value)
        assert not value.progress_controller.flush()
        assert "不会覆盖" in value.controller.progressStatus
        assert original(value.saved_progress.source, 7) == value.saved_progress
    value.controller.retryProgress()
    assert value.controller.currentPage == 3
    assert layout(value)["offset"] == 123


def test_save_failure_keeps_confirmed_record_and_retry_uses_current_position(saved_reader, monkeypatch):
    value = saved_reader
    open_saved(value)
    layout(value)
    value.controller.goToPage(4, value.controller.revision)
    layout(value)
    def fail(*args, **kwargs):
        raise PoolProgressError("storage_error", "disk full")
    with monkeypatch.context() as failed:
        failed.setattr(value.progress_store, "save", fail)
        assert not value.progress_controller.flush()
        assert "disk full" in value.controller.progressStatus
        assert value.progress_store.get(value.saved_progress.source, 7) == value.saved_progress
    value.controller.retryProgress()
    assert value.progress_store.get(value.saved_progress.source, 7).page_number == 4
    assert not value.controller.progressStatus


def test_stale_save_keeps_newer_record_until_explicit_reread(saved_reader):
    value = saved_reader
    open_saved(value)
    layout(value)
    newer = value.progress_store.save(replace(value.saved_progress, page_number=5, post_id=5), expected=value.saved_progress)
    value.controller.goToPage(4, value.controller.revision)
    layout(value)
    assert not value.progress_controller.flush()
    assert value.progress_store.get(newer.source, 7) == newer
    value.controller.retryProgress()
    assert value.controller.currentPage == 5
    layout(value)


def test_empty_pool_and_old_session_callbacks_cannot_overwrite_positions(saved_reader):
    value = saved_reader
    open_saved(value)
    layout(value)
    old = value.progress_controller._session
    empty = DanbooruPool(7, "empty", PoolCategory.SERIES, ())
    value.controller.open_pool(empty)
    value.controller.setActive(True)
    layout(value)
    assert value.progress_store.get(value.saved_progress.source, 7) == value.saved_progress
    another = DanbooruPool(8, "other", PoolCategory.SERIES, (10, 11))
    value.controller.open_pool(another, slots(another, (1, 2)))
    value.controller.setActive(True)
    assert not value.progress_controller._save(old, value.saved_progress)
    value.progress_controller._layout_ready(800, 600, old.number)
    assert value.controller.currentPage == 1 and value.controller.scrollOffset == 0
    assert value.progress_store.get(value.saved_progress.source, 8) is None


def test_two_phase_close_captures_without_write_lock_and_saves_after_release(saved_reader, monkeypatch):
    value = saved_reader
    open_saved(value)
    layout(value)
    value.controller.goToPage(4, value.controller.revision)
    layout(value)
    writer = value.progress_store._database.connect()
    writer.execute("BEGIN IMMEDIATE")
    calls = []
    original = value.progress_store.save
    def capture(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(value.progress_store, "save", capture)
    try:
        value.progress_controller.begin_shutdown()
        value.controller.shutdown()
        assert calls == [] and not value.progress_controller._timer.isActive()
        assert not value.progress_controller.flush()
    finally:
        writer.rollback()
        writer.close()
    value.progress_controller.finish_shutdown()
    assert len(calls) == 1
    assert value.progress_store.get(value.saved_progress.source, 7).page_number == 4
    assert value.progress_controller._closed


def test_retry_clearing_error_cannot_save_default_page_during_sync_hide(saved_reader, monkeypatch):
    value = saved_reader
    original = value.progress_store.get
    with monkeypatch.context() as failed:
        failed.setattr(value.progress_store, "get", lambda *args: (_ for _ in ()).throw(PoolProgressError("storage_error", "read failure")))
        open_saved(value)
        layout(value)
    assert value.controller.currentPage == 1 and value.controller.progressStatus
    fired = []
    def hide_on_clear():
        if not value.controller.progressStatus and not fired:
            fired.append(True)
            value.controller.setActive(False)
    value.controller.changed.connect(hide_on_clear)
    value.controller.retryProgress()
    assert fired and not value.controller.active and not value.controller.position_ready
    assert not value.progress_controller.flush()
    assert original(value.saved_progress.source, 7) == value.saved_progress


def test_resize_records_new_layout_only_after_position_is_confirmed(saved_reader):
    value = saved_reader
    open_saved(value)
    layout(value)
    value.controller.rememberScroll(3, 234)
    value.progress_controller.flush()
    before = value.progress_store.get(value.saved_progress.source, 7)
    value.controller.changeViewport(700, 400, value.controller.readingSession)
    assert value.controller.viewport_size == (700, 400) and not value.controller.position_ready
    value.controller.rememberScroll(3, 999)
    assert not value.progress_controller.flush()
    assert value.progress_store.get(before.source, 7) == before
    layout(value, 700, 400)
    value.controller.rememberScroll(3, 12)
    assert value.progress_controller.flush()
    after = value.progress_store.get(before.source, 7)
    assert (after.viewport_width, after.viewport_height, after.offset) == (700, 400, 12)
