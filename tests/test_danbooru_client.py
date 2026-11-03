from __future__ import annotations

import json
import os
from urllib.parse import parse_qs, urlsplit

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QGuiApplication

from danbooru_multi_search.danbooru import (
    USER_AGENT,
    DanbooruRequestOutcome,
    DanbooruSearchController,
    RequestErrorKind,
    RequestState,
)


APPLICATION = QGuiApplication.instance() or QGuiApplication([])


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
