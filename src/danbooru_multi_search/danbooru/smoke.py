"""Headless online check driven by a local Qt event loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QCoreApplication, QEventLoop

from danbooru_multi_search.danbooru.client import (
    DEFAULT_TIMEOUT_MS,
    DanbooruRequestOutcome,
    DanbooruSearchController,
    RequestState,
)


@dataclass(frozen=True, slots=True)
class SmokeCheckResult:
    exit_code: int
    payload: dict[str, Any]


def run_danbooru_smoke(
    base_url: str,
    tags: str,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> SmokeCheckResult:
    """Request one post and return a stable JSON-compatible result."""

    if QCoreApplication.instance() is None:
        raise RuntimeError("A Qt application is required for the Danbooru smoke check.")

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

    outcome = outcomes[0]
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
