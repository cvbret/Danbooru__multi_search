from __future__ import annotations

import json
import os
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest

from danbooru_multi_search.core.pools import PoolError, PoolRequestState
from danbooru_multi_search.danbooru import PoolRequestService as PublicService
from danbooru_multi_search.danbooru.client import USER_AGENT
from danbooru_multi_search.danbooru import pools as pool_module
from danbooru_multi_search.danbooru.pools import (
    PoolRequestService, build_pool_memberships_request, build_pool_request,
)
from danbooru_multi_search.tasks import (
    BoundedTaskQueue, TaskAttemptResult, TaskKind, TaskQueueConfig, TaskState,
)
from tests.test_pool_parsing import pool_mapping
from tests.test_task_queue import FakeAbortable, ManualClock, ManualScheduler
from tests.test_query_controller import ImmediateZeroScheduler


APPLICATION = QGuiApplication.instance() or QGuiApplication([])


class FakeReply(QObject):
    readyRead = Signal()
    finished = Signal()

    def __init__(self, payload=b"", *, status=200, error=QNetworkReply.NetworkError.NoError,
                 retry_after=b"", already_finished=False):
        super().__init__()
        self.payload = bytearray(payload)
        self.status = status
        self.network_error = error
        self.retry_after = retry_after
        self.finished_value = already_finished
        self.abort_count = 0

    def setReadBufferSize(self, size):
        self.buffer_size = size

    def isFinished(self):
        return self.finished_value

    def read(self, count):
        value = bytes(self.payload[:count])
        del self.payload[:count]
        return value

    def finish(self):
        self.finished_value = True
        self.finished.emit()

    def abort(self):
        self.abort_count += 1
        self.finished.emit()

    def error(self):
        return self.network_error

    def errorString(self):
        return "controlled network failure"

    def attribute(self, key):
        return self.status

    def rawHeader(self, name):
        return self.retry_after


class FakeManager:
    def __init__(self):
        self.replies = []
        self.requests = []
        self.on_get = None

    def get(self, request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if self.on_get is not None:
            self.on_get()
        return reply


@pytest.fixture
def harness():
    values = []

    def create(*, capacity=64, maximum_bytes=8 * 1024 * 1024, immediate=False):
        clock = ManualClock()
        scheduler = (ImmediateZeroScheduler if immediate else ManualScheduler)(clock)
        queue = BoundedTaskQueue(
            TaskQueueConfig(capacity=capacity, minimum_interval_seconds=0, max_retries=1),
            clock=clock, scheduler=scheduler,
        )
        manager = FakeManager()
        service = PoolRequestService("https://example.invalid", task_queue=queue,
                                     network_manager=manager, maximum_response_bytes=maximum_bytes)
        value = SimpleNamespace(service=service, manager=manager, queue=queue,
                                scheduler=scheduler, outcomes=[], accepted=[])
        service.requestCompleted.connect(value.outcomes.append)
        service.requestAccepted.connect(value.accepted.append)
        values.append(value)
        return value

    yield create
    for value in values:
        value.service.shutdown()
        value.queue.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def reply_for(identifier=1, **kwargs):
    return FakeReply(json.dumps(pool_mapping(identifier)).encode(), **kwargs)


def test_request_builders_use_official_filter_and_preserve_base_path():
    request = build_pool_memberships_request("https://example.invalid/api/?old=1#fragment", 2**53 + 1, page=2, limit=7)
    url = urlsplit(request.url().toString())
    assert url.path == "/api/pools.json" and not url.fragment
    assert parse_qs(url.query) == {
        "search[post_ids_include_any]": [str(2**53 + 1)],
        "search[is_deleted]": ["false"], "page": ["2"], "limit": ["7"],
    }
    assert bytes(request.rawHeader("User-Agent")) == USER_AGENT.encode("ascii")
    assert bytes(request.rawHeader("Accept")) == b"application/json"
    assert request.attribute(QNetworkRequest.Attribute.Http2AllowedAttribute) is False
    assert request.transferTimeout() == 10000
    assert build_pool_request("https://example.invalid", 123).url().path() == "/pools/123.json"
    assert PublicService is PoolRequestService


@pytest.mark.parametrize("kwargs", [
    {"timeout_ms": True}, {"maximum_response_bytes": 0},
    {"maximum_response_bytes": 8 * 1024 * 1024 + 1},
])
def test_service_constructor_rejects_invalid_bounds_without_work(harness, kwargs):
    value = harness()
    with pytest.raises(ValueError):
        PoolRequestService("https://example.invalid", task_queue=value.queue,
                           network_manager=value.manager, **kwargs)
    assert not value.queue.has_work and not value.manager.requests
    with pytest.raises(ValueError):
        build_pool_request("file:///unavailable", 1)


@pytest.mark.parametrize("kwargs", [{"page": 0}, {"page": 1001}, {"limit": 101}, {"limit": True}])
def test_invalid_membership_request_does_not_replace_current_operation(harness, kwargs):
    value = harness()
    identity = value.service.fetch_pool(1)
    with pytest.raises(PoolError):
        value.service.fetch_memberships(3, **kwargs)
    assert value.service.current_request == identity and value.service.busy
    assert not value.manager.requests


def test_construction_is_idle_and_requests_one_metadata_pool_only(harness):
    value = harness()
    assert value.service.state == "idle" and not value.manager.requests and not value.queue.has_work
    reply = reply_for()
    value.manager.replies.append(reply)
    identity = value.service.fetch_pool(1)
    assert value.accepted == [identity] and not value.manager.requests
    value.scheduler.run_ready()
    assert value.queue.active_count_for(TaskKind.METADATA) == 1
    reply.finish()
    assert value.outcomes[0].pool.post_ids == (9, 2, 9)
    assert value.outcomes[0].identity == identity
    value.scheduler.run_ready()
    assert len(value.manager.requests) == 1 and not value.queue.has_work


def test_membership_requests_one_explicit_page_without_auto_pagination(harness):
    value = harness()
    reply = FakeReply(json.dumps([pool_mapping(3), pool_mapping(2, category="collection")]).encode())
    value.manager.replies.append(reply)
    identity = value.service.fetch_memberships(9, page=3, limit=2)
    value.scheduler.run_ready()
    reply.finish()
    assert value.outcomes[0].memberships.page == 3
    assert [pool.id for pool in value.outcomes[0].memberships.pools] == [3, 2]
    assert value.outcomes[0].identity == identity
    value.scheduler.run_ready()
    assert len(value.manager.requests) == 1


@pytest.mark.parametrize("payload,membership", [
    (pool_mapping(2), False), ([pool_mapping(post_ids=(5,))], True),
    ([pool_mapping(), pool_mapping(2)], True),
    ([dict(pool_mapping(), is_deleted=True)], True),
])
def test_unfiltered_or_mismatched_response_fails_without_data(harness, payload, membership):
    value = harness()
    reply = FakeReply(json.dumps(payload).encode())
    value.manager.replies.append(reply)
    if membership:
        value.service.fetch_memberships(9, limit=1)
    else:
        value.service.fetch_pool(1)
    value.scheduler.run_ready()
    reply.finish()
    assert value.outcomes[0].error_code == "invalid_response"
    assert value.outcomes[0].pool is None and value.outcomes[0].memberships is None


@pytest.mark.parametrize("phase,action", [
    ("accepted", "cancel"), ("accepted", "replace"), ("accepted", "close"),
    ("running", "cancel"), ("running", "replace"), ("running", "close"),
])
def test_sync_notifications_can_invalidate_request_before_network(harness, phase, action):
    value = harness()
    value.manager.replies.append(reply_for(2))
    invoked = False

    def invalidate(*args):
        nonlocal invoked
        if invoked or (phase == "running" and value.service.state != "running"):
            return
        invoked = True
        if action == "cancel":
            value.service.cancel_current()
        elif action == "replace":
            value.service.fetch_pool(2)
        else:
            value.service.shutdown()

    signal = value.service.requestAccepted if phase == "accepted" else value.service.stateChanged
    signal.connect(invalidate)
    first = value.service.fetch_pool(1)
    value.scheduler.run_ready()
    assert invoked
    if action == "replace":
        assert [request.url().path() for request in value.manager.requests] == ["/pools/2.json"]
        assert value.service.currentRequestId != first.request_id
    else:
        assert not value.manager.requests


def test_sync_completion_during_submit_retains_accepted_identity(harness, monkeypatch):
    value = harness(immediate=True)

    def synchronous_attempt(manager, request, completion, parent, *, parser, maximum_bytes):
        assert value.accepted and not value.outcomes
        completion(TaskAttemptResult.success(parser(json.dumps(pool_mapping()).encode()), http_status=200))
        return FakeAbortable()

    monkeypatch.setattr(pool_module, "_QtPoolRequestAttempt", synchronous_attempt)
    identity = value.service.fetch_pool(1)
    assert len(value.outcomes) == 1 and value.outcomes[0].identity == identity
    assert value.service.state == "success" and not value.service.busy


def test_cancel_and_shutdown_only_affect_own_task_and_ignore_late_finish(harness):
    value = harness()
    other_abort = FakeAbortable()
    other = value.queue.submit(TaskKind.METADATA, lambda attempt, done: other_abort, lambda result: None)
    old_reply, new_reply = reply_for(1), reply_for(2)
    value.manager.replies.extend([old_reply, new_reply])
    old = value.service.fetch_pool(1)
    value.scheduler.run_ready()
    new = value.service.fetch_pool(2)
    value.scheduler.run_ready()
    assert old_reply.abort_count == 1
    assert not value.service.cancel_current(old.request_id)
    old_reply.finish()
    assert value.service.current_request == new and not value.outcomes
    value.service.shutdown()
    new_reply.finish()
    assert new_reply.abort_count == 1 and not value.outcomes
    assert other.state is TaskState.RUNNING and other_abort.abort_count == 0
    assert not value.queue.is_shutdown
    with pytest.raises(RuntimeError):
        value.service.fetch_pool(3)


def test_shutdown_inside_network_get_aborts_returned_reply(harness):
    value = harness()
    reply = reply_for()
    value.manager.replies.append(reply)
    value.manager.on_get = value.service.shutdown
    value.service.fetch_pool(1)
    value.scheduler.run_ready()
    assert reply.abort_count == 1 and not value.outcomes
    assert not value.queue.has_work and not value.service.busy
    reply.finish()
    assert not value.outcomes


def test_waiting_cancel_and_queue_full_never_start_pool_http(harness):
    value = harness(capacity=1)
    other = value.queue.submit(TaskKind.MEDIA, lambda attempt, done: FakeAbortable(), lambda result: None)
    value.service.fetch_pool(1)
    assert not value.accepted and value.outcomes[-1].error_code == "queue_rejected"
    assert not value.manager.requests and other.state is TaskState.WAITING
    value.queue.cancel(other.identifier)
    identity = value.service.fetch_pool(1)
    assert value.service.cancel_current(identity.request_id)
    value.scheduler.run_ready()
    assert not value.manager.requests and value.service.state == "cancelled"


def test_retry_after_uses_shared_queue_and_exhaustion_keeps_http_reason(harness):
    value = harness()
    first = FakeReply(status=429, retry_after=b"2")
    second = FakeReply(status=503, retry_after=b"1")
    value.manager.replies.extend([first, second])
    identity = value.service.fetch_pool(1)
    value.scheduler.run_ready()
    first.finish()
    assert value.service.state == "retry_wait" and not value.outcomes
    value.scheduler.advance(1)
    assert len(value.manager.requests) == 1
    value.scheduler.advance(1)
    second.finish()
    assert len(value.manager.requests) == 2
    assert value.outcomes[0].identity == identity and value.outcomes[0].http_status == 503
    assert value.outcomes[0].error_code == "http" and "503" in value.service.errorText


@pytest.mark.parametrize("error,expected", [
    (QNetworkReply.NetworkError.TimeoutError, "timeout"),
    (QNetworkReply.NetworkError.ConnectionRefusedError, "network"),
])
def test_network_errors_keep_reason(harness, error, expected):
    value = harness()
    reply = FakeReply(error=error, status=None)
    value.manager.replies.append(reply)
    value.service.fetch_pool(1)
    value.scheduler.run_ready()
    reply.finish()
    assert value.outcomes[0].error_code == expected


def test_oversize_stream_aborts_once_and_last_success_is_retained(harness):
    value = harness(maximum_bytes=512)
    good = reply_for()
    oversized = FakeReply(b"x" * 1024)
    value.manager.replies.extend([good, oversized])
    value.service.fetch_pool(1)
    value.scheduler.run_ready()
    good.finish()
    success = value.service.last_success
    value.service.fetch_pool(2)
    value.scheduler.run_ready()
    oversized.readyRead.emit()
    oversized.finish()
    assert value.outcomes[-1].error_code == "response_too_large"
    assert oversized.abort_count == 1 and len(oversized.payload) == 511
    assert value.service.last_success is success


def test_already_finished_reply_and_completion_notification_replacement(harness):
    value = harness()
    old = reply_for(already_finished=True)
    new = reply_for(2)
    value.manager.replies.extend([old, new])

    def replace_on_success():
        if value.service.state == "success":
            value.service.fetch_pool(2)

    value.service.stateChanged.connect(replace_on_success)
    value.service.fetch_pool(1)
    value.scheduler.run_ready()
    APPLICATION.processEvents()
    value.scheduler.run_ready()
    assert value.service.current_request.target_id == 2 and not value.outcomes
    new.finish()
    # The handler replaces success again; the previous completion stays suppressed.
    assert not value.outcomes


def test_real_qt_loopback_response_parses_original_pool_positions(local_http_server):
    server = local_http_server(json.dumps(pool_mapping()).encode())
    clock = ManualClock()
    scheduler = ManualScheduler(clock)
    queue = BoundedTaskQueue(TaskQueueConfig(minimum_interval_seconds=0), clock=clock, scheduler=scheduler)
    service = PoolRequestService(server.base_url, task_queue=queue)
    loop = QEventLoop()
    service.requestCompleted.connect(lambda outcome: loop.quit())
    try:
        service.fetch_pool(1)
        scheduler.run_ready()
        QTimer.singleShot(3000, loop.quit)
        if service.last_outcome is None:
            loop.exec()
        assert service.last_outcome is not None and service.last_outcome.succeeded
        assert service.last_outcome.pool.post_ids == (9, 2, 9)
        assert len(server.requests) == 1 and server.requests[0].path == "/pools/1.json"
    finally:
        service.shutdown()
        queue.shutdown()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("status,error,expected,retry_after", [
    (429, QNetworkReply.NetworkError.TimeoutError, "http", "2"),
    (200, QNetworkReply.NetworkError.TimeoutError, "timeout", None),
    (None, QNetworkReply.NetworkError.ConnectionRefusedError, "network", None),
])
def test_pool_failure_diagnostics_keep_qt_text_and_never_parse(
    status, error, expected, retry_after
):
    from danbooru_multi_search.danbooru.pools import _QtPoolRequestAttempt

    reply = FakeReply(b"not JSON", status=status, error=error, retry_after=b" 2 ")
    manager = FakeManager()
    manager.replies.append(reply)
    results = []

    def parser(payload):
        pytest.fail("transport failures must not parse pools")

    _QtPoolRequestAttempt(
        manager, build_pool_request("https://example.invalid", 1), results.append,
        APPLICATION, parser=parser, maximum_bytes=1024,
    )
    reply.finish()
    assert len(results) == 1
    result = results[0]
    assert result.error_code == expected and result.http_status == status
    assert result.retry_after == retry_after
    assert result.error == (
        "作品池请求返回 HTTP 429。" if expected == "http"
        else "作品池请求失败：controlled network failure"
    )
