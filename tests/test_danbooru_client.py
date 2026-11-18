from __future__ import annotations

import json
import os
from urllib.parse import parse_qs, urlsplit

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QNetworkReply

from danbooru_multi_search.danbooru import (
    USER_AGENT,
    DanbooruRequestOutcome,
    DanbooruSearchController,
    RequestErrorKind,
    RequestState,
)
from danbooru_multi_search.danbooru.client import (
    QtPostsRequestAttempt,
    build_posts_request,
)
from danbooru_multi_search.danbooru.parsing import DanbooruParseError
from danbooru_multi_search.tasks import TaskQueueConfig


APPLICATION = QGuiApplication.instance() or QGuiApplication([])


class FakeReply(QObject):
    finished = Signal()

    def __init__(self, payload: bytes, *, finish_on_abort: bool = True) -> None:
        super().__init__()
        self.payload = payload
        self.finish_on_abort = finish_on_abort
        self.abort_count = 0
        self.network_error = QNetworkReply.NetworkError.NoError

    def abort(self) -> None:
        self.abort_count += 1
        self.network_error = QNetworkReply.NetworkError.OperationCanceledError
        if self.finish_on_abort:
            self.finished.emit()

    def finish(self) -> None:
        self.finished.emit()

    def readAll(self) -> bytes:
        return self.payload

    def error(self):
        return self.network_error

    def errorString(self) -> str:
        return "cancelled" if self.abort_count else ""

    def attribute(self, attribute):
        return 200

    def rawHeader(self, header_name: str) -> bytes:
        return b""


class FakeNetworkManager:
    def __init__(self, replies: list[FakeReply]) -> None:
        self.replies = replies
        self.requests = []

    def get(self, request):
        self.requests.append(request)
        return self.replies.pop(0)


def test_posts_attempt_uses_injected_response_parser():
    reply = FakeReply(b"custom payload")
    manager = FakeNetworkManager([reply])
    results = []
    received_payloads = []

    def parser(payload: bytes):
        received_payloads.append(payload)
        return ("parsed",)

    QtPostsRequestAttempt(
        manager,
        build_posts_request("https://example.invalid", "cat"),
        results.append,
        APPLICATION,
        parser=parser,
    )
    reply.finish()

    assert received_payloads == [b"custom payload"]
    assert len(results) == 1
    assert results[0].succeeded is True
    assert results[0].value == ("parsed",)


def test_posts_attempt_maps_injected_parser_error_to_invalid_response():
    reply = FakeReply(b"invalid strict page")
    manager = FakeNetworkManager([reply])
    results = []

    def parser(payload: bytes):
        raise DanbooruParseError("strict tag fields are missing")

    QtPostsRequestAttempt(
        manager,
        build_posts_request("https://example.invalid", "cat"),
        results.append,
        APPLICATION,
        parser=parser,
    )
    reply.finish()

    assert len(results) == 1
    assert results[0].succeeded is False
    assert results[0].error_code == RequestErrorKind.INVALID_RESPONSE
    assert results[0].error == "strict tag fields are missing"


def _post_mapping() -> dict[str, object]:
    return {
        "id": 321,
        "preview_file_url": "https://cdn.example/preview.jpg",
        "large_file_url": "https://cdn.example/large.jpg",
        "file_url": "https://cdn.example/file.jpg",
        "score": 19,
        "fav_count": 8,
        "rating": "g",
        "image_width": 1200,
        "image_height": 800,
        "file_ext": "jpg",
    }


def _wait_for_outcome(
    controller: DanbooruSearchController,
    tags: str = "cat girl",
) -> DanbooruRequestOutcome:
    event_loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    outcomes: list[DanbooruRequestOutcome] = []

    def receive(outcome: DanbooruRequestOutcome) -> None:
        outcomes.append(outcome)
        event_loop.quit()

    controller.requestCompleted.connect(receive)
    timer.timeout.connect(event_loop.quit)
    controller.search(tags)
    if not outcomes:
        timer.start(3_000)
        event_loop.exec()
    timer.stop()

    assert outcomes, "QtNetwork request did not complete within the test deadline"
    return outcomes[0]


def test_controller_requests_one_post_with_required_headers_and_parameters(
    local_http_server,
):
    server = local_http_server(
        json.dumps([_post_mapping()]).encode("utf-8")
    )
    controller = DanbooruSearchController(server.base_url, timeout_ms=1_000)

    outcome = _wait_for_outcome(controller)

    assert outcome.state == RequestState.SUCCESS
    assert outcome.succeeded is True
    assert outcome.post is not None
    assert outcome.post.id == 321
    assert controller.imageUrl == "https://cdn.example/preview.jpg"
    assert len(server.requests) == 1

    request = server.requests[0]
    parsed_url = urlsplit(request.path)
    assert parsed_url.path == "/posts.json"
    assert parse_qs(parsed_url.query) == {
        "tags": ["cat girl"],
        "page": ["1"],
        "limit": ["1"],
    }
    assert request.headers["User-Agent"] == USER_AGENT
    assert request.headers["Accept"] == "application/json"


def test_controller_reports_empty_results(local_http_server):
    server = local_http_server(b"[]")
    controller = DanbooruSearchController(server.base_url, timeout_ms=1_000)

    outcome = _wait_for_outcome(controller)

    assert outcome.state == RequestState.EMPTY
    assert outcome.post is None
    assert "没有找到" in outcome.message
    assert controller.imageUrl == ""


def test_controller_reports_http_errors(local_http_server):
    server = local_http_server(b'{"error":"unavailable"}', status=503)
    controller = DanbooruSearchController(server.base_url, timeout_ms=1_000)

    outcome = _wait_for_outcome(controller)

    assert outcome.state == RequestState.ERROR
    assert outcome.error_kind == RequestErrorKind.HTTP
    assert outcome.http_status == 503
    assert "503" in controller.errorText


def test_controller_reports_transfer_timeout(local_http_server):
    server = local_http_server(
        json.dumps([_post_mapping()]).encode("utf-8"),
        delay=0.25,
    )
    controller = DanbooruSearchController(server.base_url, timeout_ms=30)

    outcome = _wait_for_outcome(controller)

    assert outcome.state == RequestState.ERROR
    assert outcome.error_kind == RequestErrorKind.TIMEOUT
    assert "超时" in outcome.message


def test_controller_reports_network_errors(local_http_server):
    server = local_http_server(b"", disconnect=True)
    controller = DanbooruSearchController(server.base_url, timeout_ms=500)

    outcome = _wait_for_outcome(controller)

    assert outcome.state == RequestState.ERROR
    assert outcome.error_kind == RequestErrorKind.NETWORK
    assert "网络错误" in outcome.message


@pytest.mark.parametrize(
    "response_body",
    [
        b"invalid-json",
        json.dumps([{**_post_mapping(), "score": "high"}]).encode("utf-8"),
    ],
)
def test_controller_reports_invalid_json_and_fields(
    local_http_server,
    response_body,
):
    server = local_http_server(response_body)
    controller = DanbooruSearchController(server.base_url, timeout_ms=1_000)

    outcome = _wait_for_outcome(controller)

    assert outcome.state == RequestState.ERROR
    assert outcome.error_kind == RequestErrorKind.INVALID_RESPONSE
    assert controller.errorText


def test_controller_cancels_waiting_request_before_network_start():
    reply = FakeReply(json.dumps([_post_mapping()]).encode("utf-8"))
    manager = FakeNetworkManager([reply])
    controller = DanbooruSearchController(
        "https://example.invalid",
        network_manager=manager,
    )
    outcomes = []
    controller.requestCompleted.connect(outcomes.append)

    controller.search("cat")
    controller.cancelCurrentRequest()
    APPLICATION.processEvents()

    assert manager.requests == []
    assert controller.state == RequestState.CANCELLED
    assert len(outcomes) == 1
    assert outcomes[0].error_kind == RequestErrorKind.CANCELLED


def test_active_cancel_aborts_reply_and_late_finish_cannot_override_new_search():
    old_reply = FakeReply(
        json.dumps([_post_mapping()]).encode("utf-8"),
        finish_on_abort=False,
    )
    new_post = {**_post_mapping(), "id": 999}
    new_reply = FakeReply(json.dumps([new_post]).encode("utf-8"))
    manager = FakeNetworkManager([old_reply, new_reply])
    controller = DanbooruSearchController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )
    outcomes = []
    controller.requestCompleted.connect(outcomes.append)

    controller.search("old")
    APPLICATION.processEvents()
    assert len(manager.requests) == 1
    controller.cancelCurrentRequest()
    assert old_reply.abort_count == 1
    assert outcomes[-1].state == RequestState.CANCELLED

    controller.search("new")
    APPLICATION.processEvents()
    assert len(manager.requests) == 2
    new_reply.finish()
    assert outcomes[-1].state == RequestState.SUCCESS
    assert outcomes[-1].post is not None
    assert outcomes[-1].post.id == 999

    old_reply.finish()
    assert len(outcomes) == 2
    assert controller.state == RequestState.SUCCESS
    assert "#999" in controller.postSummary


def test_controller_shutdown_is_idempotent_and_aborts_active_reply():
    reply = FakeReply(
        json.dumps([_post_mapping()]).encode("utf-8"),
        finish_on_abort=False,
    )
    manager = FakeNetworkManager([reply])
    controller = DanbooruSearchController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )
    outcomes = []
    controller.requestCompleted.connect(outcomes.append)

    controller.search("cat")
    APPLICATION.processEvents()
    controller.shutdown()
    controller.shutdown()

    assert reply.abort_count == 1
    assert len(outcomes) == 1
    assert outcomes[0].state == RequestState.CANCELLED
    reply.finish()
    assert len(outcomes) == 1


@pytest.mark.parametrize("status,error,expected,retry_after", [
    (429, QNetworkReply.NetworkError.TimeoutError, "http", "2"),
    (200, QNetworkReply.NetworkError.TimeoutError, "timeout", None),
    (None, QNetworkReply.NetworkError.ConnectionRefusedError, "network", None),
])
def test_posts_failure_diagnostics_keep_results_and_never_call_parser(
    monkeypatch, status, error, expected, retry_after
):
    reply = FakeReply(b"not JSON")
    monkeypatch.setattr(reply, "attribute", lambda attribute: status)
    monkeypatch.setattr(reply, "error", lambda: error)
    monkeypatch.setattr(reply, "errorString", lambda: "original network details")
    monkeypatch.setattr(reply, "rawHeader", lambda name: b" 2 ")
    results = []

    def parser(payload):
        pytest.fail("transport failures must not enter the response parser")

    QtPostsRequestAttempt(
        FakeNetworkManager([reply]), build_posts_request("https://example.invalid", "cat"),
        results.append, APPLICATION, parser=parser,
    )
    reply.finish()
    assert len(results) == 1
    result = results[0]
    assert result.error_code == expected and result.http_status == status
    assert result.retry_after == retry_after
    assert result.error == {
        "http": "Danbooru 返回 HTTP 429。",
        "timeout": "Danbooru 请求超时。",
        "network": "Danbooru 网络错误：original network details",
    }[expected]
