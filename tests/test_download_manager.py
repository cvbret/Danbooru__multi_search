from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest
from PySide6.QtTest import QSignalSpy

import danbooru_multi_search.app.download_manager as manager_module
from danbooru_multi_search.core.downloads import DownloadConflictPolicy, DownloadedFile, DownloadError, DownloadTaskState
from danbooru_multi_search.app.download_manager import PersistentDownloadManager
from danbooru_multi_search.tasks import BoundedTaskQueue, TaskAttemptResult, TaskKind, TaskQueueConfig
from tests.test_download_repository import repository, request
from tests.test_favorite_controller import APPLICATION, post
from tests.test_task_queue import ManualClock, ManualScheduler, FakeAbortable


@pytest.fixture(autouse=True)
def download_object_lifetimes(monkeypatch):
    """Keep Qt parents alive until their deferred children have been destroyed.

    Unlike the application session, these tests create parentless managers.
    Completed attempts still hold manager-capturing hooks until DeferredDelete;
    retain every instance, including managers constructed directly on reopen.
    """
    managers = []
    original = PersistentDownloadManager.__init__

    def initialise(manager, *args, **kwargs):
        original(manager, *args, **kwargs)
        managers.append(manager)

    monkeypatch.setattr(PersistentDownloadManager, '__init__', initialise)
    yield
    for manager in reversed(managers):
        manager.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    APPLICATION.processEvents()


class Reply(QObject):
    metaDataChanged = Signal()
    readyRead = Signal()
    finished = Signal()

    def __init__(self, scheduler):
        super().__init__()
        self.scheduler = scheduler
        self.payload = b""
        self.headers = {}
        self.status = 200
        self.abort_count = 0

    def rawHeader(self, name):
        return self.headers.get(name, b"")

    def readAll(self):
        value, self.payload = self.payload, b""
        return value

    def attribute(self, name):
        return self.status

    def error(self):
        return QNetworkReply.NetworkError.NoError

    def errorString(self):
        return ""

    def abort(self):
        self.abort_count += 1
        self.finished.emit()

    def send(self, payload=b"image", *, status=200, retry_after=None):
        self.status = status
        self.headers = {"Content-Length": str(len(payload)).encode()}
        if retry_after is not None:
            self.headers["Retry-After"] = str(retry_after).encode()
        self.metaDataChanged.emit()
        self.payload = payload
        self.readyRead.emit()
        self.finished.emit()
        self.scheduler.run_ready()


class Network:
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.requests = []
        self.replies = []

    def get(self, value):
        self.requests.append(value)
        reply = Reply(self.scheduler)
        self.replies.append(reply)
        return reply


def setup(tmp_path, *, capacity=64, media=1, retries=1):
    saved = repository(tmp_path)
    clock = ManualClock()
    scheduler = ManualScheduler(clock)
    queue = BoundedTaskQueue(TaskQueueConfig(capacity=capacity, media_concurrency=media,
        minimum_interval_seconds=0, max_retries=retries), clock=clock, scheduler=scheduler)
    network = Network(scheduler)
    manager = PersistentDownloadManager(saved, queue, network, scheduler=scheduler)
    return saved, manager, queue, scheduler, network


def works(count=3, identifier=1):
    return tuple(replace(post(identifier + index, url=f"https://example.invalid/{identifier + index}.jpg"), file_ext="jpg") for index in range(count))


def test_register_committed_pool_batch_is_idempotent_and_keeps_normal_file_pipeline(tmp_path):
    from danbooru_multi_search.core.downloads import DownloadRequest
    saved, manager, queue, scheduler, network = setup(tmp_path)
    batch = saved.create_batch((DownloadRequest(9, 'https://example.invalid/9.png', 'png', '00003'),), tmp_path / 'files')
    manager.schedule_persisted_batch(batch.batch_id)
    manager.schedule_persisted_batch(batch.batch_id)
    scheduler.run_ready()
    assert len(network.requests) == 1
    network.replies[0].send(b'original')
    assert (tmp_path / 'files' / '00003_9.png').read_bytes() == b'original'
    assert saved.get_batch(batch.batch_id).items[0].state is DownloadTaskState.COMPLETED
    manager.shutdown()


def test_committed_batch_notification_shutdown_prevents_its_first_request(tmp_path):
    saved, manager, queue, scheduler, network = setup(tmp_path)
    batch = saved.create_batch((request(),), tmp_path)
    manager.batchChanged.connect(lambda _batch: manager.begin_shutdown())
    manager.schedule_persisted_batch(batch.batch_id)
    scheduler.run_ready()
    assert not network.requests
    manager.shutdown()
    assert saved.get_batch(batch.batch_id).items[0].state is DownloadTaskState.AWAITING_RESUME


def test_capacity_backpressure_keeps_all_persistent_waiting_items_and_shared_queue(tmp_path):
    saved, manager, queue, scheduler, network = setup(tmp_path, capacity=1)
    foreign = queue.submit(TaskKind.METADATA, lambda _number, _done: FakeAbortable(), lambda result: None)
    batch = manager.enqueue_batch(works(), tmp_path / "中文 下载")
    scheduler.run_ready()
    assert network.requests == [] and len(saved.get_batch(batch.batch_id).items) == 3
    queue.cancel(foreign.identifier)
    scheduler.advance(0.25)
    for index in range(3):
        assert len(network.replies) == index + 1
        network.replies[index].send()
    final = saved.get_batch(batch.batch_id)
    assert final.counts["completed"] == 3 and final.bytes_written == 15
    assert [value.url().path() for value in network.requests] == ["/1.jpg", "/2.jpg", "/3.jpg"]
    manager.shutdown()
    assert not queue.is_shutdown


def test_failed_item_does_not_stop_the_remaining_finite_batch(tmp_path):
    saved, manager, queue, scheduler, network = setup(tmp_path, media=2)
    batch = manager.enqueue_batch(works(8), tmp_path / "downloads")
    scheduler.run_ready()
    assert len(network.replies) == 2 and queue.active_count_for(TaskKind.MEDIA) == 2
    network.replies[0].send(b"missing", status=404)
    for index in range(1, 8):
        network.replies[index].send()
    final = saved.get_batch(batch.batch_id)
    assert final.counts["failed"] == 1 and final.counts["completed"] == 7
    assert final.items[0].error_code == "http"


def test_retry_after_resets_current_bytes_and_preserves_cumulative_attempts(tmp_path):
    saved, manager, queue, scheduler, network = setup(tmp_path)
    batch = manager.enqueue_download(works(1)[0], tmp_path)
    scheduler.run_ready()
    network.replies[0].send(b"busy", status=503, retry_after=2)
    waiting = saved.get_item(batch.items[0].item_id)
    assert waiting.state is DownloadTaskState.RETRY_WAIT and waiting.attempts == 1
    assert len(network.requests) == 1
    scheduler.advance(1.99)
    assert len(network.requests) == 1
    scheduler.advance(0.01)
    running = saved.get_item(waiting.item_id)
    assert running.bytes_written == 0 and running.attempts == 2 and running.generation > waiting.generation
    network.replies[1].send(b"complete")
    assert saved.get_item(running.item_id).state is DownloadTaskState.COMPLETED


@pytest.mark.parametrize("policy,expected_requests,expected_names", [
    (DownloadConflictPolicy.SKIP, 1, ["1.jpg", "1.jpg"]),
    (DownloadConflictPolicy.OVERWRITE, 2, ["1.jpg", "1.jpg"]),
    (DownloadConflictPolicy.AUTO_NUMBER, 2, ["1.jpg", "1 (1).jpg"]),
])
def test_same_target_uses_runtime_claims_and_explicit_skipped_result(tmp_path, policy, expected_requests, expected_names):
    saved, manager, queue, scheduler, network = setup(tmp_path, media=2)
    work = works(1)[0]
    batch = manager.enqueue_batch((work, work), tmp_path / "files", policy)
    scheduler.run_ready()
    assert len(network.replies) == (2 if policy is DownloadConflictPolicy.AUTO_NUMBER else 1)
    network.replies[0].send(b"first")
    if len(network.replies) > 1:
        network.replies[1].send(b"second")
    final = saved.get_batch(batch.batch_id)
    assert len(network.requests) == expected_requests
    assert [item.resolved_name for item in final.items] == expected_names
    assert final.items[1].state is (DownloadTaskState.SKIPPED if policy is DownloadConflictPolicy.SKIP else DownloadTaskState.COMPLETED)


def test_cancel_entire_batch_invalidates_waiting_items_before_synchronous_callbacks(tmp_path):
    saved, manager, queue, scheduler, network = setup(tmp_path)
    batch = manager.enqueue_batch(works(5), tmp_path / "files")
    scheduler.run_ready()
    other = manager.enqueue_download(works(1, 99)[0], tmp_path / "other")
    scheduler.run_ready()
    observed = []
    def on_item(item):
        if item.batch_id == batch.batch_id and item.state is DownloadTaskState.CANCELLED:
            assert all(value.state is DownloadTaskState.CANCELLED for value in saved.get_batch(batch.batch_id).items)
            assert not manager.retry(item.item_id)
            observed.append(item.item_id)
    manager.itemChanged.connect(on_item)
    assert manager.cancel_batch(batch.batch_id)
    scheduler.run_ready()
    assert len(network.requests) == 2  # The second request belongs to the other batch.
    assert network.requests[1].url().path() == "/99.jpg"
    network.replies[0].payload = b"late"
    network.replies[0].finished.emit()
    assert not (tmp_path / "files" / "1.jpg").exists()
    network.replies[1].send()
    assert saved.get_batch(other.batch_id).terminal and observed


def test_restart_has_zero_requests_and_manual_resume_starts_from_zero_at_decided_path(tmp_path):
    saved, manager, queue, scheduler, network = setup(tmp_path)
    manager.shutdown()
    batch = saved.create_batch((request(),), tmp_path / "files", DownloadConflictPolicy.AUTO_NUMBER)
    item = saved.set_destination(batch.items[0].item_id, 0, "321 (2).jpg")
    item = saved.begin_attempt(item.item_id, item.generation)
    name = f".download-{item.item_id}-{item.generation}.part"
    saved.register_partial(item.item_id, item.generation, name)
    item.directory.mkdir()
    (item.directory / name).write_bytes(b"old partial")
    restored = PersistentDownloadManager(saved, queue, network, scheduler=scheduler)
    assert network.requests == []
    assert saved.get_item(item.item_id).state is DownloadTaskState.AWAITING_RESUME
    assert restored.resume_batch(batch.batch_id)
    scheduler.run_ready()
    assert len(network.requests) == 1 and not network.requests[0].hasRawHeader("Range")
    assert not (item.directory / name).exists()
    network.replies[0].send(b"fresh")
    final = saved.get_item(item.item_id)
    assert final.resolved_name == "321 (2).jpg" and final.attempts == 2 and final.bytes_written == 5


@pytest.mark.skipif(os.name != "nt", reason="Requires real Windows path handling")
def test_windows_long_chinese_space_directory_download_survives_reopen_and_manual_resume(tmp_path):
    from danbooru_multi_search.storage.database import Database
    from danbooru_multi_search.storage.downloads import DownloadRepository
    saved, manager, queue, scheduler, network = setup(tmp_path)
    directory = tmp_path / "中文 空格 下载"
    while len(str(directory / "321 (1).jpg").encode("utf-16-le")) // 2 <= 300:
        directory /= "长目录 with spaces 0123456789"
    assert directory.is_absolute()
    directory.mkdir(parents=True)
    original = directory / "321.JPG"
    original.write_bytes(b"previous complete image")
    restored = None
    destroyed = ()
    try:
        batch = manager.enqueue_batch(works(1, identifier=321), directory, DownloadConflictPolicy.AUTO_NUMBER)
        scheduler.run_ready()
        running = saved.get_batch(batch.batch_id).items[0]
        assert running.resolved_name == "321 (1).jpg" and running.attempts == 1
        assert len(str(running.destination).encode("utf-16-le")) // 2 > 260
        reply = network.replies[-1]
        reply.payload = b"interrupted bytes"
        reply.readyRead.emit()
        partial = directory / saved.get_item(running.item_id).partial_name
        assert partial.is_file() and len(str(partial).encode("utf-16-le")) // 2 > 260
        manager.shutdown()
        reopened = DownloadRepository(Database(saved._database.path))
        restored = PersistentDownloadManager(reopened, queue, network, scheduler=scheduler)
        paused = reopened.get_item(running.item_id)
        assert paused.state is DownloadTaskState.AWAITING_RESUME and paused.directory == directory
        assert paused.resolved_name == running.resolved_name and len(network.requests) == 1
        assert restored.resume_batch(batch.batch_id)
        scheduler.run_ready()
        assert len(network.requests) == 2 and not network.requests[-1].hasRawHeader("Range")
        assert reopened.get_item(running.item_id).bytes_written == 0
        payload = b"complete original-format payload"
        network.replies[-1].send(payload)
        final = reopened.get_item(running.item_id)
        assert final.state is DownloadTaskState.COMPLETED and final.attempts == 2
        assert final.directory == directory and final.resolved_name == "321 (1).jpg"
        assert final.destination.read_bytes() == payload and final.bytes_written == len(payload)
        assert original.read_bytes() == b"previous complete image"
        assert not partial.exists() and not tuple(directory.glob("*.part"))
        attempts = tuple(manager.findChildren(QObject)) + tuple(restored.findChildren(QObject))
        destroyed = tuple(QSignalSpy(attempt.destroyed) for attempt in attempts)
    finally:
        manager.shutdown()
        if restored is not None:
            restored.shutdown()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APPLICATION.processEvents()
    assert len(destroyed) == 2 and all(spy.count() == 1 for spy in destroyed)
    assert manager.objectName() == restored.objectName() == ""


def test_resume_cleanup_failure_keeps_whole_batch_awaiting_resume(tmp_path, monkeypatch):
    saved, manager, queue, scheduler, network = setup(tmp_path)
    manager.shutdown()
    batch = saved.create_batch((request(1), request(2)), tmp_path)
    restored = PersistentDownloadManager(saved, queue, network, scheduler=scheduler)
    original = manager_module.discard_owned_partial
    calls = []
    def fail_second(item):
        calls.append(item.item_id)
        if item.item_id == batch.items[1].item_id:
            raise DownloadError("file_error", "second cleanup failed")
        original(item)
    monkeypatch.setattr(manager_module, "discard_owned_partial", fail_second)
    assert not restored.resume_batch(batch.batch_id)
    assert all(item.state is DownloadTaskState.AWAITING_RESUME for item in saved.get_batch(batch.batch_id).items)
    assert restored._pending == [] and network.requests == [] and "cleanup failed" in restored.error
    monkeypatch.setattr(manager_module, "discard_owned_partial", original)
    assert restored.resume_batch(batch.batch_id)
    scheduler.run_ready()
    assert len(network.requests) == 1


def test_file_commit_then_database_failure_keeps_file_and_stops_following_requests(tmp_path, monkeypatch):
    saved, manager, queue, scheduler, network = setup(tmp_path)
    original = saved.finish
    def fail(item_id, generation, state, **kwargs):
        if state is DownloadTaskState.COMPLETED:
            raise DownloadError("storage_error", "completion database failure")
        return original(item_id, generation, state, **kwargs)
    monkeypatch.setattr(saved, "finish", fail)
    batch = manager.enqueue_batch(works(), tmp_path / "files")
    scheduler.run_ready()
    network.replies[0].send(b"complete")
    assert (tmp_path / "files" / "1.jpg").read_bytes() == b"complete"
    assert len(network.requests) == 1 and manager._halted and "database failure" in manager.error
    assert saved.get_item(batch.items[0].item_id).state is not DownloadTaskState.COMPLETED


def test_running_notification_can_cancel_before_any_request(tmp_path):
    saved, manager, queue, scheduler, network = setup(tmp_path)
    manager.itemChanged.connect(lambda item: manager.cancel(item.item_id) if item.state is DownloadTaskState.RUNNING else None)
    batch = manager.enqueue_download(works(1)[0], tmp_path)
    scheduler.run_ready()
    assert network.requests == [] and saved.get_batch(batch.batch_id).counts["cancelled"] == 1


def test_synchronous_completion_is_bound_to_persistent_identity_before_submit_returns(tmp_path, monkeypatch):
    saved, manager, queue, scheduler, network = setup(tmp_path)
    handles = []
    def immediate(_manager, _request, destination, source, _maximum, completion, _parent, **kwargs):
        destination.write_bytes(b"sync")
        completion(TaskAttemptResult.success(DownloadedFile(destination, source, 4)))
        handle = FakeAbortable()
        handles.append(handle)
        return handle
    monkeypatch.setattr(manager_module, "start_media_download_attempt", immediate)
    batch = manager.enqueue_batch(works(5), tmp_path / "files")
    scheduler.run_ready()
    assert saved.get_batch(batch.batch_id).counts["completed"] == 5
    assert all(handle.abort_count == 0 for handle in handles)
    assert manager._entries == {} and not queue.has_work


def test_shutdown_cancels_only_owned_media_and_old_reply_cannot_commit(tmp_path):
    saved, manager, queue, scheduler, network = setup(tmp_path)
    foreign_handle = FakeAbortable()
    foreign = queue.submit(TaskKind.METADATA, lambda _number, _done: foreign_handle, lambda _result: None)
    batch = manager.enqueue_batch(works(), tmp_path / "files")
    scheduler.run_ready()
    reply = network.replies[0]
    manager.begin_shutdown()
    assert reply.abort_count == 1 and foreign_handle.abort_count == 0
    manager.shutdown()
    manager.shutdown()
    reply.payload = b"late"
    reply.finished.emit()
    assert len(network.requests) == 1 and manager.closed and not queue.is_shutdown
    assert all(item.state is DownloadTaskState.AWAITING_RESUME for item in saved.get_batch(batch.batch_id).items)
    assert not list((tmp_path / "files").glob("*.jpg"))
    queue.cancel(foreign.identifier)


def test_close_from_progress_prevents_commit_and_no_late_ui_result(tmp_path):
    saved, manager, queue, scheduler, network = setup(tmp_path)
    published = []
    def changed(item):
        published.append(item.state)
        if item.bytes_written > 0:
            manager.shutdown()
    manager.itemChanged.connect(changed)
    batch = manager.enqueue_download(works(1)[0], tmp_path)
    scheduler.run_ready()
    network.replies[0].send(b"partial")
    assert manager.closed and DownloadTaskState.COMPLETED not in published
    assert saved.get_item(batch.items[0].item_id).state is DownloadTaskState.AWAITING_RESUME
    assert not (tmp_path / "1.jpg").exists()


def test_failed_overwrite_can_be_manually_retried_without_losing_the_old_file(tmp_path, monkeypatch):
    import danbooru_multi_search.storage.download_files as files
    saved, manager, queue, scheduler, network = setup(tmp_path)
    target = tmp_path / "1.jpg"
    target.write_bytes(b"previous complete")
    original = files.os.replace
    def fail(*args):
        raise OSError("simulated replace failure")
    monkeypatch.setattr(files.os, "replace", fail)
    batch = manager.enqueue_download(works(1)[0], tmp_path, DownloadConflictPolicy.OVERWRITE)
    scheduler.run_ready()
    network.replies[0].send(b"new data")
    item = saved.get_item(batch.items[0].item_id)
    assert item.state is DownloadTaskState.FAILED and item.error_code == "file_error"
    assert target.read_bytes() == b"previous complete" and not list(tmp_path.glob("*.part"))
    monkeypatch.setattr(files.os, "replace", original)
    assert manager.retry(item.item_id)
    scheduler.run_ready()
    network.replies[1].send(b"retried data")
    final = saved.get_item(item.item_id)
    assert final.state is DownloadTaskState.COMPLETED and final.attempts == 2
    assert target.read_bytes() == b"retried data"


def test_progress_database_failure_aborts_network_and_prevents_next_request(tmp_path, monkeypatch):
    saved, manager, queue, scheduler, network = setup(tmp_path)
    original = saved.update_progress
    def fail(item_id, generation, written, total):
        if written:
            raise DownloadError("storage_error", "progress database failure")
        return original(item_id, generation, written, total)
    monkeypatch.setattr(saved, "update_progress", fail)
    batch = manager.enqueue_batch(works(), tmp_path / "files")
    scheduler.run_ready()
    network.replies[0].send(b"partial")
    assert manager._halted and "progress database failure" in manager.error
    assert len(network.requests) == 1 and network.replies[0].abort_count >= 1
    assert not list((tmp_path / "files").glob("*.jpg"))
    assert saved.get_item(batch.items[0].item_id).state is not DownloadTaskState.COMPLETED


def test_batch_cleanup_error_still_aborts_all_active_members(tmp_path, monkeypatch):
    saved, manager, queue, scheduler, network = setup(tmp_path, media=2)
    batch = manager.enqueue_batch(works(4), tmp_path / "files")
    scheduler.run_ready()
    original = manager_module.discard_owned_partial
    def fail_waiting(item):
        if item.item_id == batch.items[2].item_id:
            raise DownloadError("file_error", "waiting partial cleanup failed")
        original(item)
    monkeypatch.setattr(manager_module, "discard_owned_partial", fail_waiting)
    assert not manager.cancel_batch(batch.batch_id)
    assert all(reply.abort_count == 1 for reply in network.replies)
    assert saved.get_batch(batch.batch_id).counts["cancelled"] == 4
    assert len(network.requests) == 2 and "cleanup failed" in manager.error


def test_storage_error_notification_cannot_reenter_resume_before_failure_cleanup_finishes(tmp_path, monkeypatch):
    saved, manager, queue, scheduler, network = setup(tmp_path)
    original = saved.finish
    def fail(item_id, generation, state, **kwargs):
        if state is DownloadTaskState.COMPLETED:
            raise DownloadError("storage_error", "finish failed")
        return original(item_id, generation, state, **kwargs)
    monkeypatch.setattr(saved, "finish", fail)
    batch = manager.enqueue_batch(works(), tmp_path / "files")
    scheduler.run_ready()
    reentry = []
    manager.errorOccurred.connect(lambda _error: reentry.append(manager.resume_batch(batch.batch_id)))
    network.replies[0].send()
    assert reentry == [False] and manager._halted
    assert len(network.requests) == 1


def test_new_batch_database_failure_stops_existing_pending_requests(tmp_path, monkeypatch):
    saved, manager, queue, scheduler, network = setup(tmp_path)
    manager.enqueue_batch(works(), tmp_path / "files")
    scheduler.run_ready()
    def fail(*args):
        raise DownloadError("storage_error", "batch save failed")
    monkeypatch.setattr(saved, "create_batch", fail)
    with pytest.raises(DownloadError):
        manager.enqueue_download(works(1, 99)[0], tmp_path / "other")
    assert manager._halted and network.replies[0].abort_count == 1
    scheduler.run_ready()
    assert len(network.requests) == 1


def test_old_item_ui_generation_cannot_cancel_or_retry_a_new_attempt(tmp_path, monkeypatch):
    saved, manager, queue, scheduler, network = setup(tmp_path)
    batch = manager.enqueue_download(works(1)[0], tmp_path)
    scheduler.run_ready()
    running = saved.get_item(batch.items[0].item_id)
    assert not manager.cancel(running.item_id, expected_generation=running.generation - 1)
    assert network.replies[0].abort_count == 0
    assert manager.cancel(running.item_id, expected_generation=running.generation)
    cancelled = saved.get_item(running.item_id)
    original = manager_module.discard_owned_partial
    monkeypatch.setattr(manager_module, "discard_owned_partial", lambda _item: pytest.fail("stale retry must not clean any file"))
    assert not manager.retry(cancelled.item_id, expected_generation=running.generation)
    monkeypatch.setattr(manager_module, "discard_owned_partial", original)
    assert manager.retry(cancelled.item_id, expected_generation=cancelled.generation)
    scheduler.run_ready()
    assert not manager.cancel(cancelled.item_id, expected_generation=cancelled.generation)
    assert network.replies[1].abort_count == 0


def test_batch_ui_guard_ignores_progress_but_rejects_another_execution_generation(tmp_path):
    saved, manager, queue, scheduler, network = setup(tmp_path)
    batch = manager.enqueue_batch(works(), tmp_path)
    old = {item.item_id: item.generation for item in batch.items}
    scheduler.run_ready()
    assert not manager.cancel_batch(batch.batch_id, expected_generations=old)
    current = saved.get_batch(batch.batch_id)
    expected = {item.item_id: item.generation for item in current.items}
    saved.update_progress(current.items[0].item_id, current.items[0].generation, 3, 10)
    assert manager.cancel_batch(batch.batch_id, expected_generations=expected)
    assert saved.get_batch(batch.batch_id).counts["cancelled"] == 3
