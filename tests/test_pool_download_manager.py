from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal

from danbooru_multi_search.core.downloads import DownloadError
from danbooru_multi_search.app.pool_downloads import PoolDownloadManager
from danbooru_multi_search.danbooru.pool_pages import PoolPageRequest
from tests.test_pool_download_repository import build, create, pool, batch
from tests.test_favorite_controller import APPLICATION
from tests.test_task_queue import ManualClock, ManualScheduler


class Pages(QObject):
    accepted = Signal(object)
    completed = Signal(object, object, str, str)

    def __init__(self, value, *, immediate=True, missing=()):
        super().__init__()
        self.value, self.immediate, self.missing = value, immediate, missing
        self.requests = []
        self.cancelled = self.closed = False

    def fetch_pages(self, refs):
        request = PoolPageRequest(str(len(self.requests)), refs)
        self.requests.append(request)
        self.cancelled = False
        self.accepted.emit(request)
        if not self.cancelled and self.immediate:
            self.completed.emit(request, batch(self.value, refs, missing=self.missing), '', '')
        return request

    def cancel_current(self, *args):
        self.cancelled = True

    def shutdown(self):
        self.closed = True
        self.cancel_current()


class Downloads(QObject):
    errorOccurred = Signal(str)
    closed = halted = False
    error = ''

    def __init__(self):
        super().__init__()
        self.batches = []

    def schedule_persisted_batch(self, key):
        if key not in self.batches:
            self.batches.append(key)


def harness(tmp_path, count=1003, *, immediate=True, missing=()):
    database, repository, downloads = build(tmp_path)
    value = pool(count)
    pages, files = Pages(value, immediate=immediate, missing=missing), Downloads()
    scheduler = ManualScheduler(ManualClock())
    manager = PoolDownloadManager(repository, pages, files, 'https://example.invalid', scheduler=scheduler)
    return SimpleNamespace(database=database, repo=repository, downloads=downloads, value=value,
                           pages=pages, files=files, scheduler=scheduler, manager=manager)


def start(value, tmp_path):
    from danbooru_multi_search.core.downloads import DownloadConflictPolicy
    return value.manager.start(value.value, tmp_path, DownloadConflictPolicy.SKIP)


def test_entire_25000_position_pool_crosses_all_1000_boundaries_with_bounded_requests(tmp_path):
    value = harness(tmp_path, 25000, missing=(1001,))
    plan = start(value, tmp_path)
    assert not value.pages.requests
    value.scheduler.run_ready()
    assert value.repo.get(plan.plan_id).state.value == 'ready'
    assert len(value.pages.requests) == 625 and all(len(r.references) <= 40 for r in value.pages.requests)
    assert len(value.files.batches) == 25
    assert value.repo.summaries()[0].enqueued == 24999
    last = value.downloads.get_batch(value.files.batches[-1])
    assert len(last.items) == 1000 and last.items[-1].request.filename == '25000_25000.png'
    second = value.downloads.get_batch(value.files.batches[1])
    assert second.items[0].request.filename == '01002_1002.png'


def test_stop_in_committed_notification_never_builds_following_batch_and_resume_does_not_duplicate(tmp_path):
    value = harness(tmp_path)
    plan = start(value, tmp_path)
    def stop(key):
        if value.files.batches and value.manager.active:
            value.manager.stop(key, value.manager.active.generation)
    value.manager.changed.connect(stop)
    value.scheduler.run_ready()
    assert len(value.files.batches) == 1 and len(value.pages.requests) == 25
    value.manager.changed.disconnect(stop)
    current = value.repo.get(plan.plan_id)
    assert value.manager.resume(plan.plan_id, current.generation)
    value.scheduler.run_ready()
    assert len(value.files.batches) == 2 and value.repo.summaries()[0].enqueued == 1003


@pytest.mark.parametrize('action', ['stop', 'close'])
def test_accepted_and_late_completion_cannot_start_after_stop_or_close(tmp_path, action):
    value = harness(tmp_path, immediate=False)
    plan = start(value, tmp_path)
    value.scheduler.run_ready()
    request = value.pages.requests[0]
    if action == 'stop':
        assert value.manager.stop(plan.plan_id, plan.generation)
    else:
        value.manager.begin_shutdown()
    value.pages.completed.emit(request, batch(value.value, request.references), '', '')
    value.scheduler.run_ready()
    assert len(value.pages.requests) == 1 and value.downloads.list_batches() == ()
    if action == 'close':
        value.manager.finish_shutdown()
        assert value.manager._closed


def test_network_failure_reopen_is_zero_network_and_continues_saved_snapshot(tmp_path):
    value = harness(tmp_path, 3, immediate=False)
    plan = start(value, tmp_path)
    value.scheduler.run_ready()
    value.pages.completed.emit(value.pages.requests[0], None, '服务器返回错误', 'http')
    assert value.manager.active is None and value.repo.get(plan.plan_id).state.value == 'paused'
    value.manager.begin_shutdown()
    value.manager.finish_shutdown()
    pages = Pages(value.value)
    reopened = PoolDownloadManager(value.repo, pages, value.files, value.manager.source, scheduler=value.scheduler)
    assert not pages.requests
    current = value.repo.get(plan.plan_id)
    assert reopened.resume(plan.plan_id, current.generation)
    value.scheduler.run_ready()
    assert len(pages.requests) == 1 and len(value.files.batches) == 1


def test_locked_preparation_pauses_without_network_then_closes_after_writer_finishes(tmp_path):
    value = harness(tmp_path, 3, immediate=False)
    plan = start(value, tmp_path)
    value.scheduler.run_ready()
    with value.database.connect() as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        value.manager.begin_shutdown()
        assert value.pages.closed
        blocker.rollback()
    value.manager.finish_shutdown()
    assert value.repo.get(plan.plan_id).state.value == 'paused'
    assert value.downloads.list_batches() == ()


def test_metadata_save_busy_pauses_immediately_and_user_can_retry_same_uncommitted_window(tmp_path):
    value = harness(tmp_path, 3, immediate=False)
    plan = start(value, tmp_path)
    value.scheduler.run_ready()
    request = value.pages.requests[0]
    with value.database.connect() as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        value.pages.completed.emit(request, batch(value.value, request.references), '', '')
        assert value.manager.active is None and 'locked' in value.manager.error
        assert not value.files.batches
        blocker.rollback()
    current = value.repo.get(plan.plan_id)
    value.pages.immediate = True
    assert value.manager.resume(current.plan_id, current.generation)
    value.scheduler.run_ready()
    assert len(value.files.batches) == 1


def test_stop_between_atomic_batch_commit_and_registration_resumes_without_stranded_waiting_or_duplicates(tmp_path, monkeypatch):
    value = harness(tmp_path, 3)
    original = value.repo.commit_group
    def commit_then_stop(plan, first):
        result = original(plan, first)
        value.manager.stop(plan.plan_id, plan.generation)
        return result
    monkeypatch.setattr(value.repo, 'commit_group', commit_then_stop)
    plan = start(value, tmp_path)
    value.scheduler.run_ready()
    assert len(value.downloads.list_batches()) == 1 and not value.files.batches
    current = value.repo.get(plan.plan_id)
    assert value.manager.resume(current.plan_id, current.generation)
    value.scheduler.run_ready()
    assert len(value.files.batches) == len(value.downloads.list_batches()) == 1
    assert value.repo.get(plan.plan_id).state.value == 'ready'


def test_actual_metadata_service_uses_shared_queue_and_original_slot_mapping(tmp_path):
    import json
    from danbooru_multi_search.danbooru.pool_pages import PoolPageRequestService
    from tests.test_pool_page_requests import page_harness
    from tests.test_pool_requests import FakeReply
    from tests.test_pool_post_parsing import raw_post
    from danbooru_multi_search.tasks import TaskKind
    from tests.test_task_queue import FakeAbortable
    db, repo, downloads = build(tmp_path)
    pages = page_harness()
    files = Downloads()
    manager = PoolDownloadManager(repo, pages.service, files, 'https://example.invalid', scheduler=pages.scheduler)
    external = pages.queue.submit(TaskKind.METADATA, lambda _attempt, _done: FakeAbortable(), lambda _result: None)
    value = pool(ids=(9, 8, 9))
    reply = FakeReply(json.dumps([raw_post(8, is_deleted=True), raw_post(9)]).encode())
    pages.manager.replies.append(reply)
    from danbooru_multi_search.core.downloads import DownloadConflictPolicy
    plan = manager.start(value, tmp_path, DownloadConflictPolicy.SKIP)
    pages.scheduler.run_ready()
    assert len(pages.manager.requests) == 1
    reply.finish()
    pages.scheduler.run_ready()
    assert repo.get(plan.plan_id).state.value == 'ready' and len(files.batches) == 1
    batch = downloads.get_batch(files.batches[0])
    assert [item.request.filename for item in batch.items] == ['00001_9.jpg', '00003_9.jpg']
    manager.begin_shutdown()
    manager.finish_shutdown()
    assert not pages.queue.is_shutdown and not external.terminal
    pages.queue.shutdown()


def test_restart_does_not_start_linked_files_until_existing_batch_is_manually_resumed(tmp_path):
    from tests.test_download_manager import setup
    from danbooru_multi_search.app.download_manager import PersistentDownloadManager
    from danbooru_multi_search.storage.pool_downloads import PoolDownloadRepository
    from danbooru_multi_search.core.downloads import DownloadTaskState
    downloads, old, queue, scheduler, network = setup(tmp_path)
    repo = PoolDownloadRepository(downloads._database)
    value = pool(1)
    plan = create(repo, value, tmp_path)
    repo.record_metadata(plan, batch(value, value.pages))
    committed = repo.commit_group(plan, 1)
    old.shutdown()
    files = PersistentDownloadManager(downloads, queue, network, scheduler=scheduler)
    pages = Pages(value)
    manager = PoolDownloadManager(repo, pages, files, plan.source, scheduler=scheduler)
    scheduler.run_ready()
    assert not network.requests and not pages.requests
    assert downloads.get_batch(committed.batch_id).items[0].state is DownloadTaskState.AWAITING_RESUME
    current = repo.get(plan.plan_id)
    assert manager.resume(current.plan_id, current.generation)
    scheduler.run_ready()
    assert not network.requests and not pages.requests and len(downloads.list_batches()) == 1
    assert files.resume_batch(committed.batch_id)
    scheduler.run_ready()
    assert len(network.requests) == 1
    network.replies[0].send(b'original')
    assert (plan.directory / '00001_1.png').read_bytes() == b'original'
    manager.begin_shutdown()
    manager.finish_shutdown()
    files.shutdown()
