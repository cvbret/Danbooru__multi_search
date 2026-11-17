import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer

from danbooru_multi_search.core.pools import PoolPageReference
from danbooru_multi_search.danbooru.pool_pages import PoolPageRequestService
from danbooru_multi_search.tasks import BoundedTaskQueue, TaskQueueConfig, TaskKind, TaskState
from tests.test_pool_requests import APPLICATION, FakeReply, FakeManager
from tests.test_task_queue import ManualClock, ManualScheduler, FakeAbortable
from tests.test_pool_post_parsing import raw_post


def page_harness(*, base_url="https://example.invalid/api/?old=1#fragment", capacity=64):
    clock = ManualClock()
    scheduler = ManualScheduler(clock)
    queue = BoundedTaskQueue(TaskQueueConfig(capacity=capacity, minimum_interval_seconds=0), clock=clock, scheduler=scheduler)
    manager = FakeManager()
    service = PoolPageRequestService(base_url, queue, manager)
    value = SimpleNamespace(service=service, queue=queue, manager=manager, scheduler=scheduler, results=[])
    service.completed.connect(lambda *args: value.results.append(args))
    return value


@pytest.fixture
def pages():
    value = page_harness()
    yield value
    value.service.shutdown()
    value.queue.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_page_request_deduplicates_http_ids_and_maps_all_original_slots(pages):
    reply = FakeReply(json.dumps([raw_post(2), raw_post(9)]).encode())
    pages.manager.replies.append(reply)
    references = tuple(PoolPageReference(5, number, identifier) for number, identifier in enumerate((9, 3, 9, 2), 1))
    identity = pages.service.fetch_pages(references)
    pages.scheduler.run_ready()
    url = urlsplit(pages.manager.requests[0].url().toString())
    assert url.path == "/api/posts.json" and not url.fragment
    assert parse_qs(url.query) == {"tags": ["id:9,3,2 status:any"], "limit": ["3"], "page": ["1"]}
    reply.finish()
    operation, batch, error, code = pages.results[0]
    assert operation is identity and not error and not code
    assert [slot.reference.post_id for slot in batch.slots] == [9, 3, 9, 2]
    assert batch.slots[0].metadata is batch.slots[2].metadata
    assert batch.slots[1].metadata.state.value == "not_returned"


@pytest.mark.parametrize("action", ["cancel", "replace", "close"])
def test_accepted_notification_can_stop_old_request_before_http(pages, action):
    references = (PoolPageReference(1, 1, 1),)
    invoked = False

    def accept(operation):
        nonlocal invoked
        if invoked:
            return
        invoked = True
        if action == "cancel":
            pages.service.cancel_current(operation.request_id)
        elif action == "replace":
            pages.service.fetch_pages((PoolPageReference(1, 2, 2),))
        else:
            pages.service.shutdown()

    pages.service.accepted.connect(accept)
    pages.manager.replies.append(FakeReply(b"[]"))
    pages.service.fetch_pages(references)
    pages.scheduler.run_ready()
    assert len(pages.manager.requests) == (1 if action == "replace" else 0)
    assert not pages.results


def test_shutdown_and_late_reply_do_not_cancel_other_metadata(pages):
    abortable = FakeAbortable()
    other = pages.queue.submit(TaskKind.METADATA, lambda attempt, done: abortable, lambda result: None)
    reply = FakeReply(b"[]")
    pages.manager.replies.append(reply)
    pages.service.fetch_pages((PoolPageReference(1, 1, 1),))
    pages.scheduler.run_ready()
    pages.service.shutdown()
    reply.finish()
    assert reply.abort_count == 1 and not pages.results
    assert other.state is TaskState.RUNNING and abortable.abort_count == 0


def test_queue_rejection_is_visible_without_http():
    value = page_harness(capacity=1)
    value.queue.submit(TaskKind.MEDIA, lambda attempt, done: FakeAbortable(), lambda result: None)
    try:
        value.service.fetch_pages((PoolPageReference(1, 1, 1),))
        assert value.results[0][3] == "queue_rejected" and not value.manager.requests
    finally:
        value.service.shutdown()
        value.queue.shutdown()


def test_real_qt_local_batch_preserves_api_path_and_missing_positions(local_http_server):
    server = local_http_server(json.dumps([raw_post(2)]).encode())
    value = page_harness(base_url=server.base_url + "/api/?old=1#fragment")
    from PySide6.QtNetwork import QNetworkAccessManager
    value.service._manager = QNetworkAccessManager(value.service)
    loop = QEventLoop()
    value.service.completed.connect(lambda *args: loop.quit())
    try:
        value.service.fetch_pages((PoolPageReference(7, 1, 1), PoolPageReference(7, 2, 2)))
        value.scheduler.run_ready()
        QTimer.singleShot(3000, loop.quit)
        if not value.results:
            loop.exec()
        assert value.results[0][1].slots[0].metadata.state.value == "not_returned"
        assert server.requests[0].path.startswith("/api/posts.json?")
    finally:
        value.service.shutdown()
        value.queue.shutdown()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
