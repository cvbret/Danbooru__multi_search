"""Bounded asynchronous task queue, state and retry utilities."""

from importlib import import_module
from typing import Any


# Public names are grouped by their owning module so importing the package
# does not load network, Qt, or storage implementations before they are used.
_EXPORT_GROUPS = {
    "danbooru_multi_search.tasks.model": (
        "InvalidTaskTransition",
        "TaskAttemptResult",
        "TaskFinalResult",
        "TaskKind",
        "TaskRecord",
        "TaskState",
    ),
    "danbooru_multi_search.tasks.queue": (
        "BoundedTaskQueue",
        "TaskQueueConfig",
        "TaskQueueFullError",
        "TaskQueueShutdownError",
    ),
    "danbooru_multi_search.tasks.retry": (
        "RETRYABLE_HTTP_STATUSES",
        "bounded_retry_delay",
        "parse_retry_after",
    ),
    "danbooru_multi_search.tasks.scheduling": (
        "Clock",
        "ScheduledCall",
        "Scheduler",
        "SystemClock",
    ),
    "danbooru_multi_search.tasks.qt_scheduling": (
        "QtScheduler",
    ),
}
_EXPORTS = {
    name: module
    for module, names in _EXPORT_GROUPS.items()
    for name in names
}

__all__ = [
    "RETRYABLE_HTTP_STATUSES",
    "BoundedTaskQueue",
    "Clock",
    "InvalidTaskTransition",
    "QtScheduler",
    "ScheduledCall",
    "Scheduler",
    "SystemClock",
    "TaskAttemptResult",
    "TaskFinalResult",
    "TaskKind",
    "TaskQueueConfig",
    "TaskQueueFullError",
    "TaskQueueShutdownError",
    "TaskRecord",
    "TaskState",
    "bounded_retry_delay",
    "parse_retry_after",
]


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

