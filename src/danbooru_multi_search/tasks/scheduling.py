"""Clock and task scheduling interfaces independent of an event loop."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from importlib import import_module
from typing import Any, Callable, Protocol


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def utcnow(self) -> datetime: ...


class ScheduledCall(Protocol):
    @property
    def active(self) -> bool: ...

    def cancel(self) -> None: ...


class Scheduler(Protocol):
    def call_later(
        self,
        delay_seconds: float,
        callback: Callable[[], None],
    ) -> ScheduledCall: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def utcnow(self) -> datetime:
        return datetime.now(timezone.utc)


# Preserve the former implementation import path without loading Qt for callers
# that only need the scheduling interfaces or SystemClock.
def __getattr__(name: str) -> Any:
    if name not in {"QtScheduler", "QtScheduledCall"}:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(
        import_module("danbooru_multi_search.tasks.qt_scheduling"), name
    )
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | {"QtScheduler", "QtScheduledCall"})
