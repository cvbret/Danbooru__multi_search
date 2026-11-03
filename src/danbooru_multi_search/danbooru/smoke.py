"""Headless online check driven by a local Qt event loop."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QEventLoop

from danbooru_multi_search.danbooru.client import (
    DEFAULT_TIMEOUT_MS,
    DanbooruRequestOutcome,
    DanbooruSearchController,
    RequestState,
)
from danbooru_multi_search.danbooru.download import (
    DanbooruDownloadController,
    DanbooruDownloadOutcome,
    DownloadState,
)


@dataclass(frozen=True, slots=True)
class SmokeCheckResult:
    exit_code: int
    payload: dict[str, Any]


def _request_one_post(
    base_url: str,
    tags: str,
    *,
    timeout_ms: int,
) -> DanbooruRequestOutcome:
    event_loop = QEventLoop()
    outcomes: list[DanbooruRequestOutcome] = []
    controller = DanbooruSearchController(base_url, timeout_ms=timeout_ms)

    def receive(outcome: DanbooruRequestOutcome) -> None:
        outcomes.append(outcome)
        event_loop.quit()

    controller.requestCompleted.connect(receive)
    controller.search(tags)
    if not outcomes:
        event_loop.exec()
    controller.shutdown()
    if not outcomes:
        raise RuntimeError("Danbooru post request ended without an outcome.")
    return outcomes[0]


def run_danbooru_smoke(
    base_url: str,
    tags: str,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> SmokeCheckResult:
    """Request one post and return a stable JSON-compatible result."""

    if QCoreApplication.instance() is None:
        raise RuntimeError("A Qt application is required for the Danbooru smoke check.")

    outcome = _request_one_post(
        base_url,
        tags,
        timeout_ms=timeout_ms,
    )
    common: dict[str, Any] = {
        "tags": tags.strip(),
        "message": outcome.message,
    }

    if outcome.state == RequestState.SUCCESS and outcome.post is not None:
        return SmokeCheckResult(
            exit_code=0,
            payload={
                **common,
                "status": "ok",
                "post": outcome.post.to_mapping(),
            },
        )

    if outcome.state == RequestState.EMPTY:
        return SmokeCheckResult(
            exit_code=2,
            payload={**common, "status": "empty"},
        )

    return SmokeCheckResult(
        exit_code=2,
        payload={
            **common,
            "status": "error",
            "error_kind": outcome.error_kind,
            "http_status": outcome.http_status,
        },
    )


def run_danbooru_download_smoke(
    base_url: str,
    tags: str,
    download_directory: Path,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> SmokeCheckResult:
    """Request one post, download its media, and return stable JSON data."""

    if QCoreApplication.instance() is None:
        raise RuntimeError("A Qt application is required for the download smoke check.")

    normalized_tags = tags.strip()
    search_outcome = _request_one_post(
        base_url,
        normalized_tags,
        timeout_ms=timeout_ms,
    )
    if search_outcome.state != RequestState.SUCCESS or search_outcome.post is None:
        return SmokeCheckResult(
            exit_code=2,
            payload={
                "status": (
                    "empty" if search_outcome.state == RequestState.EMPTY else "error"
                ),
                "operation": "search",
                "tags": normalized_tags,
                "message": search_outcome.message,
                "error_kind": search_outcome.error_kind,
                "http_status": search_outcome.http_status,
            },
        )

    post = search_outcome.post
    event_loop = QEventLoop()
    outcomes: list[DanbooruDownloadOutcome] = []
    controller = DanbooruDownloadController(timeout_ms=timeout_ms)

    def receive(outcome: DanbooruDownloadOutcome) -> None:
        outcomes.append(outcome)
        event_loop.quit()

    controller.downloadCompleted.connect(receive)
    controller.download(post, download_directory)
    if not outcomes:
        event_loop.exec()
    controller.shutdown()
    if not outcomes:
        raise RuntimeError("Danbooru media download ended without an outcome.")

    outcome = outcomes[0]
    common: dict[str, Any] = {
        "operation": "download",
        "tags": normalized_tags,
        "post_id": post.id,
        "source_url": post.download_url,
        "message": outcome.message,
    }
    if outcome.state == DownloadState.SUCCESS and outcome.downloaded_file is not None:
        downloaded_file = outcome.downloaded_file
        return SmokeCheckResult(
            exit_code=0,
            payload={
                **common,
                "status": "ok",
                "download": {
                    "path": str(downloaded_file.path),
                    "bytes": downloaded_file.bytes_written,
                },
            },
        )

    return SmokeCheckResult(
        exit_code=2,
        payload={
            **common,
            "status": "error",
            "error_kind": outcome.error_kind,
            "http_status": outcome.http_status,
        },
    )
