from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QGuiApplication

import danbooru_multi_search.danbooru.download as download_module
from danbooru_multi_search.danbooru import (
    USER_AGENT,
    DanbooruDownloadController,
    DanbooruDownloadOutcome,
    DanbooruPost,
    DownloadErrorKind,
    DownloadState,
)
from danbooru_multi_search.tasks import TaskQueueConfig


APPLICATION = QGuiApplication.instance() or QGuiApplication([])


def test_original_public_download_values_reexport_pure_core_objects():
    from danbooru_multi_search.core import downloads
    from danbooru_multi_search import danbooru
    for name in ("DanbooruDownloadOutcome", "DownloadedFile", "DownloadErrorKind", "DownloadState"):
        assert getattr(download_module, name) is getattr(downloads, name)
        assert getattr(danbooru, name) is getattr(downloads, name)


def _post(source_url: str, *, file_ext: str = "jpg") -> DanbooruPost:
    return DanbooruPost(
        id=321,
        preview_url=None,
        large_file_url=None,
        file_url=source_url,
        score=19,
        fav_count=8,
        rating="g",
        width=1200,
        height=800,
        file_ext=file_ext,
    )


def _wait_for_download(
    controller: DanbooruDownloadController,
    post: DanbooruPost,
    download_directory: Path,
) -> DanbooruDownloadOutcome:
    event_loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    outcomes: list[DanbooruDownloadOutcome] = []

    def receive(outcome: DanbooruDownloadOutcome) -> None:
        outcomes.append(outcome)
        event_loop.quit()

    controller.downloadCompleted.connect(receive)
    timer.timeout.connect(event_loop.quit)
    controller.download(post, download_directory)
    if not outcomes:
        timer.start(3_000)
        event_loop.exec()
    timer.stop()

    assert outcomes, "Danbooru media download did not finish before the deadline"
    return outcomes[0]


def _part_files(directory: Path) -> list[Path]:
    return list(directory.glob("*.part")) + list(directory.glob(".*.part"))


def test_download_writes_an_atomic_complete_file_with_required_headers(
    local_http_server,
    tmp_path,
):
    payload = b"local-test-image"
    server = local_http_server(payload)
    controller = DanbooruDownloadController(
        timeout_ms=1_000,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )

    outcome = _wait_for_download(
        controller,
        _post(f"{server.base_url}/image.jpg"),
        tmp_path,
    )
    controller.shutdown()

    expected_path = tmp_path / "321.jpg"
    assert outcome.state == DownloadState.SUCCESS
    assert outcome.succeeded is True
    assert outcome.downloaded_file is not None
    assert outcome.downloaded_file.path == expected_path
    assert outcome.downloaded_file.bytes_written == len(payload)
    assert expected_path.read_bytes() == payload
    assert _part_files(tmp_path) == []
    assert len(server.requests) == 1
    assert server.requests[0].path == "/image.jpg"
    assert server.requests[0].headers["User-Agent"] == USER_AGENT
    assert server.requests[0].headers["Accept"].startswith("image/*")


def test_http_error_leaves_no_complete_or_temporary_file(
    local_http_server,
    tmp_path,
):
    server = local_http_server(b"not found", status=404)
    controller = DanbooruDownloadController(
        timeout_ms=1_000,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )

    outcome = _wait_for_download(controller, _post(server.base_url), tmp_path)

    assert outcome.state == DownloadState.ERROR
    assert outcome.error_kind == DownloadErrorKind.HTTP
    assert outcome.http_status == 404
    assert not (tmp_path / "321.jpg").exists()
    assert _part_files(tmp_path) == []


def test_empty_response_leaves_no_complete_file(local_http_server, tmp_path):
    server = local_http_server(b"")
    controller = DanbooruDownloadController(
        timeout_ms=1_000,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )

    outcome = _wait_for_download(controller, _post(server.base_url), tmp_path)

    assert outcome.error_kind == DownloadErrorKind.EMPTY_RESPONSE
    assert not (tmp_path / "321.jpg").exists()
    assert _part_files(tmp_path) == []


def test_content_length_over_limit_is_rejected_without_a_file(
    local_http_server,
    tmp_path,
):
    server = local_http_server(b"12345")
    controller = DanbooruDownloadController(
        maximum_bytes=4,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )

    outcome = _wait_for_download(controller, _post(server.base_url), tmp_path)

    assert outcome.error_kind == DownloadErrorKind.RESPONSE_TOO_LARGE
    assert not (tmp_path / "321.jpg").exists()
    assert _part_files(tmp_path) == []


def test_actual_bytes_over_limit_are_rejected_without_content_length(
    local_http_server,
    tmp_path,
):
    server = local_http_server(b"12345", include_content_length=False)
    controller = DanbooruDownloadController(
        maximum_bytes=4,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )

    outcome = _wait_for_download(controller, _post(server.base_url), tmp_path)

    assert outcome.error_kind == DownloadErrorKind.RESPONSE_TOO_LARGE
    assert not (tmp_path / "321.jpg").exists()
    assert _part_files(tmp_path) == []


def test_transfer_timeout_and_network_failure_have_stable_errors(
    local_http_server,
    tmp_path,
):
    slow_server = local_http_server(b"image", delay=0.2)
    timeout_controller = DanbooruDownloadController(
        timeout_ms=20,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )

    timeout_outcome = _wait_for_download(
        timeout_controller,
        _post(slow_server.base_url),
        tmp_path / "timeout",
    )

    disconnected_server = local_http_server(b"", disconnect=True)
    network_controller = DanbooruDownloadController(
        timeout_ms=500,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )
    network_outcome = _wait_for_download(
        network_controller,
        _post(disconnected_server.base_url),
        tmp_path / "network",
    )

    assert timeout_outcome.error_kind == DownloadErrorKind.TIMEOUT
    assert network_outcome.error_kind == DownloadErrorKind.NETWORK
    assert list(tmp_path.rglob("*.jpg")) == []
    assert list(tmp_path.rglob("*.part")) == []


def test_file_and_path_validation_fail_before_network_access(
    local_http_server,
    tmp_path,
):
    server = local_http_server(b"image")
    destination_file = tmp_path / "not-a-directory"
    destination_file.write_text("occupied", encoding="utf-8")
    controller = DanbooruDownloadController(
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )

    file_outcome = _wait_for_download(
        controller,
        _post(server.base_url),
        destination_file,
    )
    invalid_extension_outcome = _wait_for_download(
        controller,
        _post(server.base_url, file_ext="../jpg"),
        tmp_path / "safe",
    )

    assert file_outcome.error_kind == DownloadErrorKind.FILE
    assert invalid_extension_outcome.error_kind == DownloadErrorKind.INVALID_REQUEST
    assert server.requests == []
    assert not (tmp_path / "jpg").exists()


def test_missing_original_url_does_not_fallback_to_mismatched_sample(
    local_http_server,
    tmp_path,
):
    server = local_http_server(b"jpeg sample")
    post = DanbooruPost(
        id=321,
        preview_url=f"{server.base_url}/preview.jpg",
        large_file_url=f"{server.base_url}/sample.jpg",
        file_url=None,
        score=19,
        fav_count=8,
        rating="g",
        width=1200,
        height=800,
        file_ext="webm",
    )
    download_directory = tmp_path / "downloads"
    controller = DanbooruDownloadController(
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )

    outcome = _wait_for_download(controller, post, download_directory)

    assert post.download_url == ""
    assert outcome.state == DownloadState.ERROR
    assert outcome.error_kind == DownloadErrorKind.INVALID_REQUEST
    assert "original file URL" in outcome.message
    assert server.requests == []
    assert not download_directory.exists()


def test_atomic_replace_failure_preserves_existing_file_and_removes_temporary(
    local_http_server,
    monkeypatch,
    tmp_path,
):
    server = local_http_server(b"new image")
    destination = tmp_path / "321.jpg"
    destination.write_bytes(b"previous complete image")

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(download_module.os, "replace", fail_replace)
    controller = DanbooruDownloadController(
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )

    outcome = _wait_for_download(controller, _post(server.base_url), tmp_path)

    assert outcome.error_kind == DownloadErrorKind.FILE
    assert destination.read_bytes() == b"previous complete image"
    assert _part_files(tmp_path) == []


def test_cancel_active_download_removes_temporary_file(
    local_http_server,
    tmp_path,
):
    server = local_http_server(b"delayed image", delay=0.25)
    controller = DanbooruDownloadController(
        timeout_ms=1_000,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )
    outcomes: list[DanbooruDownloadOutcome] = []
    controller.downloadCompleted.connect(outcomes.append)

    controller.download(_post(server.base_url), tmp_path)
    APPLICATION.processEvents()
    assert _part_files(tmp_path)

    controller.cancelCurrentDownload()

    assert len(outcomes) == 1
    assert outcomes[0].state == DownloadState.CANCELLED
    assert not (tmp_path / "321.jpg").exists()
    assert _part_files(tmp_path) == []


def test_application_shutdown_is_idempotent_and_cleans_active_download(
    local_http_server,
    tmp_path,
):
    server = local_http_server(b"delayed image", delay=0.25)
    controller = DanbooruDownloadController(
        timeout_ms=1_000,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )
    outcomes: list[DanbooruDownloadOutcome] = []
    controller.downloadCompleted.connect(outcomes.append)

    controller.download(_post(server.base_url), tmp_path)
    APPLICATION.processEvents()
    APPLICATION.aboutToQuit.emit()
    controller.shutdown()
    controller.shutdown()

    assert len(outcomes) == 1
    assert outcomes[0].state == DownloadState.CANCELLED
    assert not (tmp_path / "321.jpg").exists()
    assert _part_files(tmp_path) == []
