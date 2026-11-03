"""Bounded, rate-limited asynchronous task queue with cancellation."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from typing import Callable, Protocol

from danbooru_multi_search.tasks.model import (
    TaskAttemptResult,
    TaskFinalResult,
    TaskKind,
    TaskRecord,
    TaskState,
)
from danbooru_multi_search.tasks.retry import (
    RETRYABLE_HTTP_STATUSES,
    bounded_retry_delay,
)
from danbooru_multi_search.tasks.scheduling import Clock, ScheduledCall, Scheduler


class Abortable(Protocol):
    def abort(self) -> None: ...


AttemptCallback = Callable[[TaskAttemptResult], None]
TaskStarter = Callable[[int, AttemptCallback], Abortable | None]
TaskCompletionCallback = Callable[[TaskFinalResult], None]


class TaskQueueFullError(RuntimeError):
    pass


class TaskQueueShutdownError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TaskQueueConfig:
    """Queue limits; maximum_retry_delay_seconds caps fallback backoff only."""

    capacity: int = 64
    metadata_concurrency: int = 2
    media_concurrency: int = 4
    minimum_interval_seconds: float = 0.25
    max_retries: int = 2
    base_backoff_seconds: float = 0.5
    maximum_retry_delay_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("capacity must be greater than zero")
        if self.metadata_concurrency <= 0 or self.media_concurrency <= 0:
            raise ValueError("concurrency limits must be greater than zero")
        if self.minimum_interval_seconds < 0:
            raise ValueError("minimum_interval_seconds cannot be negative")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.base_backoff_seconds < 0:
            raise ValueError("base_backoff_seconds cannot be negative")
        if self.maximum_retry_delay_seconds < 0:
            raise ValueError("maximum_retry_delay_seconds cannot be negative")

    def concurrency_for(self, kind: TaskKind) -> int:
        return (
            self.metadata_concurrency
            if kind == TaskKind.METADATA
            else self.media_concurrency
        )


@dataclass(slots=True)
class _TaskEntry:
    record: TaskRecord
    starter: TaskStarter
    completion: TaskCompletionCallback
    ready_at: float
    abortable: Abortable | None = None
    finalized: bool = False


class BoundedTaskQueue:
    def __init__(
        self,
        config: TaskQueueConfig,
        *,
        clock: Clock,
        scheduler: Scheduler,
    ) -> None:
        self.config = config
        self._clock = clock
        self._scheduler = scheduler
        self._identifier_counter = count(1)
        self._entries: dict[str, _TaskEntry] = {}
        self._pending: list[str] = []
        self._active: dict[TaskKind, set[str]] = {
            TaskKind.METADATA: set(),
            TaskKind.MEDIA: set(),
        }
        self._last_started_at: float | None = None
        self._dispatch_call: ScheduledCall | None = None
        self._dispatch_due_at: float | None = None
        self._dispatching = False
        self._dispatch_requested = False
        self._shutdown = False

    @property
    def is_shutdown(self) -> bool:
        return self._shutdown

    @property
    def waiting_count(self) -> int:
        return self._count_pending(TaskState.WAITING)

    @property
    def retry_wait_count(self) -> int:
        return self._count_pending(TaskState.RETRY_WAIT)

    @property
    def active_count(self) -> int:
        return sum(len(task_ids) for task_ids in self._active.values())

    def active_count_for(self, kind: TaskKind) -> int:
        return len(self._active[kind])

    @property
    def has_scheduled_dispatch(self) -> bool:
        return self._dispatch_call is not None and self._dispatch_call.active

    @property
    def has_work(self) -> bool:
        return bool(self._entries)

    def submit(
        self,
        kind: TaskKind,
        starter: TaskStarter,
        completion: TaskCompletionCallback,
        *,
        state_listener: Callable[[TaskRecord], None] | None = None,
    ) -> TaskRecord:
        if self._shutdown:
            raise TaskQueueShutdownError("The task queue has been shut down.")
        if len(self._entries) >= self.config.capacity:
            raise TaskQueueFullError("The task queue is full.")

        task_id = f"task-{next(self._identifier_counter)}"
        record = TaskRecord(identifier=task_id, kind=kind)
        if state_listener is not None:
            record.add_listener(state_listener)
        entry = _TaskEntry(
            record=record,
            starter=starter,
            completion=completion,
            ready_at=self._clock.monotonic(),
        )
        self._entries[task_id] = entry
        self._pending.append(task_id)
        if state_listener is not None:
            state_listener(record)
        self._request_dispatch()
        return record

    def cancel(self, task_id: str, reason: str = "Task was cancelled.") -> bool:
        entry = self._entries.get(task_id)
        if entry is None or entry.record.terminal or entry.finalized:
            return False

        self._remove_pending(task_id)
        if entry.record.state == TaskState.RUNNING:
            self._active[entry.record.kind].discard(task_id)
        abortable = entry.abortable
        entry.abortable = None
        entry.record.transition(TaskState.CANCELLED, reason)

        if abortable is not None:
            try:
                abortable.abort()
            except Exception:
                pass

        self._finalize(
            entry,
            TaskFinalResult(
                task_id=task_id,
                state=TaskState.CANCELLED,
                attempts=entry.record.attempts,
                error=reason,
                error_code="cancelled",
            ),
        )
        self._reset_dispatch()
        return True

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self._cancel_dispatch_call()
        for task_id in tuple(self._entries):
            self.cancel(task_id, "Application is shutting down.")
        self._pending.clear()

    def _request_dispatch(self) -> None:
        if self._shutdown:
            return
        if self._dispatching:
            self._dispatch_requested = True
            return
        self._schedule_dispatch(0.0)

    def _schedule_dispatch(self, delay_seconds: float) -> None:
        if self._shutdown or not self._pending:
            return
        delay = max(0.0, delay_seconds)
        due_at = self._clock.monotonic() + delay
        if (
            self._dispatch_call is not None
            and self._dispatch_call.active
            and self._dispatch_due_at is not None
            and self._dispatch_due_at <= due_at
        ):
            return
        self._cancel_dispatch_call()
        self._dispatch_due_at = due_at
        self._dispatch_call = self._scheduler.call_later(
            delay,
            self._dispatch_from_timer,
        )

    def _dispatch_from_timer(self) -> None:
        self._dispatch_call = None
        self._dispatch_due_at = None
        self._dispatch()

    def _dispatch(self) -> None:
        if self._shutdown:
            return
        if self._dispatching:
            self._dispatch_requested = True
            return

        self._dispatching = True
        self._dispatch_requested = False
        try:
            while True:
                candidate, next_ready_at = self._next_candidate()
                if candidate is None:
                    if next_ready_at is not None:
                        self._schedule_dispatch(
                            max(0.0, next_ready_at - self._clock.monotonic())
                        )
                    break
                self._start(candidate)
        finally:
            self._dispatching = False

        if self._dispatch_requested:
            self._dispatch_requested = False
            self._request_dispatch()

    def _next_candidate(self) -> tuple[_TaskEntry | None, float | None]:
        now = self._clock.monotonic()
        next_interval_at = (
            now
            if self._last_started_at is None
            else self._last_started_at + self.config.minimum_interval_seconds
        )
        earliest_ready_at: float | None = None

        for task_id in tuple(self._pending):
            entry = self._entries.get(task_id)
            if entry is None or entry.record.terminal:
                self._remove_pending(task_id)
                continue
            kind = entry.record.kind
            if len(self._active[kind]) >= self.config.concurrency_for(kind):
                continue

            ready_at = max(entry.ready_at, next_interval_at)
            if ready_at <= now:
                return entry, None
            if earliest_ready_at is None or ready_at < earliest_ready_at:
                earliest_ready_at = ready_at
        return None, earliest_ready_at

    def _start(self, entry: _TaskEntry) -> None:
        record = entry.record
        self._remove_pending(record.identifier)
        record.attempts += 1
        attempt_number = record.attempts
        self._active[record.kind].add(record.identifier)
        record.transition(TaskState.RUNNING)

        current = self._entries.get(record.identifier)
        if (
            self._shutdown
            or current is not entry
            or record.state != TaskState.RUNNING
            or record.attempts != attempt_number
        ):
            self._active[record.kind].discard(record.identifier)
            return

        self._last_started_at = self._clock.monotonic()

        def completed(result: TaskAttemptResult) -> None:
            self._attempt_completed(record.identifier, attempt_number, result)

        try:
            abortable = entry.starter(attempt_number, completed)
        except Exception as exc:
            self._attempt_completed(
                record.identifier,
                attempt_number,
                TaskAttemptResult.failure(
                    f"Task could not start: {exc}",
                    error_code="start_error",
                ),
            )
            return

        current = self._entries.get(record.identifier)
        if (
            current is entry
            and record.state == TaskState.RUNNING
            and record.attempts == attempt_number
        ):
            entry.abortable = abortable
        elif record.state == TaskState.CANCELLED and abortable is not None:
            try:
                abortable.abort()
            except Exception:
                pass

    def _attempt_completed(
        self,
        task_id: str,
        attempt_number: int,
        result: TaskAttemptResult,
    ) -> None:
        entry = self._entries.get(task_id)
        if (
            entry is None
            or entry.finalized
            or entry.record.state != TaskState.RUNNING
            or entry.record.attempts != attempt_number
        ):
            return

        record = entry.record
        self._active[record.kind].discard(task_id)
        entry.abortable = None

        if result.succeeded:
            record.transition(TaskState.SUCCEEDED)
            self._finalize(
                entry,
                TaskFinalResult(
                    task_id=task_id,
                    state=TaskState.SUCCEEDED,
                    attempts=record.attempts,
                    value=result.value,
                    http_status=result.http_status,
                ),
            )
            self._request_dispatch()
            return

        retryable = result.http_status in RETRYABLE_HTTP_STATUSES
        if retryable and record.attempts <= self.config.max_retries:
            retry_number = record.attempts
            delay = bounded_retry_delay(
                result.retry_after,
                retry_number=retry_number,
                now_utc=self._clock.utcnow(),
                base_backoff_seconds=self.config.base_backoff_seconds,
                maximum_delay_seconds=self.config.maximum_retry_delay_seconds,
            )
            entry.ready_at = self._clock.monotonic() + delay
            record.transition(TaskState.RETRY_WAIT, result.error)
            self._pending.append(task_id)
            self._request_dispatch()
            return

        retry_exhausted = retryable and record.attempts > self.config.max_retries
        error = result.error or "Task failed."
        if retry_exhausted:
            error = f"{error} Retry limit reached after {record.attempts} attempts."
        record.transition(TaskState.FAILED, error)
        self._finalize(
            entry,
            TaskFinalResult(
                task_id=task_id,
                state=TaskState.FAILED,
                attempts=record.attempts,
                error=error,
                error_code=result.error_code,
                http_status=result.http_status,
                retry_exhausted=retry_exhausted,
            ),
        )
        self._request_dispatch()

    def _finalize(self, entry: _TaskEntry, result: TaskFinalResult) -> None:
        if entry.finalized:
            return
        entry.finalized = True
        task_id = entry.record.identifier
        self._remove_pending(task_id)
        self._active[entry.record.kind].discard(task_id)
        self._entries.pop(task_id, None)
        entry.completion(result)

    def _reset_dispatch(self) -> None:
        self._cancel_dispatch_call()
        if not self._shutdown and self._pending:
            self._request_dispatch()

    def _cancel_dispatch_call(self) -> None:
        if self._dispatch_call is not None:
            self._dispatch_call.cancel()
        self._dispatch_call = None
        self._dispatch_due_at = None

    def _remove_pending(self, task_id: str) -> None:
        try:
            self._pending.remove(task_id)
        except ValueError:
            pass

    def _count_pending(self, state: TaskState) -> int:
        return sum(
            1
            for task_id in self._pending
            if task_id in self._entries
            and self._entries[task_id].record.state == state
        )
