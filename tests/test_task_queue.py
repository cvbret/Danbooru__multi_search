from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Callable

import pytest

from danbooru_multi_search.tasks import (
    BoundedTaskQueue,
    InvalidTaskTransition,
    TaskAttemptResult,
    TaskKind,
    TaskQueueConfig,
    TaskQueueFullError,
    TaskRecord,
    TaskState,
    parse_retry_after,
)


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.wall_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.value

    def utcnow(self) -> datetime:
        return self.wall_time

    def advance(self, seconds: float) -> None:
        self.value += seconds
        self.wall_time += timedelta(seconds=seconds)


@dataclass(slots=True)
class ManualCall:
    due_at: float
    callback: Callable[[], None]
    active: bool = True

    def cancel(self) -> None:
        self.active = False


class ManualScheduler:
    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.calls: list[ManualCall] = []

    def call_later(
        self,
        delay_seconds: float,
        callback: Callable[[], None],
    ) -> ManualCall:
        call = ManualCall(self.clock.monotonic() + delay_seconds, callback)
        self.calls.append(call)
        return call

    def run_ready(self) -> None:
        while True:
            ready = [
                call
                for call in self.calls
                if call.active and call.due_at <= self.clock.monotonic()
            ]
            if not ready:
                return
            call = min(ready, key=lambda item: item.due_at)
            call.active = False
            call.callback()

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)
        self.run_ready()

    @property
    def active_call_count(self) -> int:
        return sum(call.active for call in self.calls)


@dataclass(slots=True)
class FakeAbortable:
    abort_count: int = 0

    def abort(self) -> None:
        self.abort_count += 1


@dataclass(slots=True)
class StartedAttempt:
    number: int
    started_at: float
    completion: Callable[[TaskAttemptResult], None]
    abortable: FakeAbortable


class ControlledStarter:
    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.attempts: list[StartedAttempt] = []

    def __call__(
        self,
        attempt_number: int,
        completion: Callable[[TaskAttemptResult], None],
    ) -> FakeAbortable:
        abortable = FakeAbortable()
        self.attempts.append(
            StartedAttempt(
                number=attempt_number,
                started_at=self.clock.monotonic(),
                completion=completion,
                abortable=abortable,
            )
        )
        return abortable


def _queue(
    config: TaskQueueConfig | None = None,
) -> tuple[BoundedTaskQueue, ManualClock, ManualScheduler]:
    clock = ManualClock()
    scheduler = ManualScheduler(clock)
    queue = BoundedTaskQueue(
        config
        or TaskQueueConfig(
            minimum_interval_seconds=0,
            base_backoff_seconds=1,
        ),
        clock=clock,
        scheduler=scheduler,
    )
    return queue, clock, scheduler


def test_task_state_model_rejects_terminal_and_invalid_transitions():
    observed: list[TaskState] = []
    record = TaskRecord("task", TaskKind.METADATA)
    record.add_listener(lambda current: observed.append(current.state))

    record.transition(TaskState.RUNNING)
    record.transition(TaskState.SUCCEEDED)

    assert record.history == (
        TaskState.WAITING,
        TaskState.RUNNING,
        TaskState.SUCCEEDED,
    )
    assert observed == [TaskState.RUNNING, TaskState.SUCCEEDED]
    with pytest.raises(InvalidTaskTransition):
        record.transition(TaskState.RUNNING)

    waiting = TaskRecord("waiting", TaskKind.MEDIA)
    with pytest.raises(InvalidTaskTransition):
        waiting.transition(TaskState.RETRY_WAIT)


def test_queue_is_bounded_and_finalizes_only_once():
    queue, _, scheduler = _queue(TaskQueueConfig(capacity=1, minimum_interval_seconds=0))
    starter = ControlledStarter(scheduler.clock)
    results = []
    record = queue.submit(TaskKind.METADATA, starter, results.append)

    with pytest.raises(TaskQueueFullError):
        queue.submit(TaskKind.MEDIA, starter, results.append)

    scheduler.run_ready()
    attempt = starter.attempts[0]
    attempt.completion(TaskAttemptResult.success("done"))
    attempt.completion(TaskAttemptResult.failure("late", http_status=503))

    assert record.state == TaskState.SUCCEEDED
    assert len(results) == 1
    assert results[0].value == "done"
    assert queue.has_work is False


def test_running_listener_can_cancel_before_starter_without_leaking_queue_state():
    queue, clock, scheduler = _queue()
    starter = ControlledStarter(clock)
    results = []
    active_counts = []

    def listener(record: TaskRecord) -> None:
        if record.state == TaskState.RUNNING:
            active_counts.append(queue.active_count)
            queue.cancel(record.identifier)

    record = queue.submit(
        TaskKind.METADATA,
        starter,
        results.append,
        state_listener=listener,
    )
    scheduler.run_ready()

    assert active_counts == [1]
    assert starter.attempts == []
    assert record.state == TaskState.CANCELLED
    assert len(results) == 1
    assert queue.active_count == 0
    assert queue.waiting_count == 0
    assert queue.retry_wait_count == 0
    assert queue.has_scheduled_dispatch is False
    assert queue.has_work is False


def test_running_listener_can_shutdown_before_starter_without_leaking_state():
    queue, clock, scheduler = _queue()
    starter = ControlledStarter(clock)
    results = []

    def listener(record: TaskRecord) -> None:
        if record.state == TaskState.RUNNING:
            assert queue.active_count == 1
            queue.shutdown()

    record = queue.submit(
        TaskKind.MEDIA,
        starter,
        results.append,
        state_listener=listener,
    )
    scheduler.run_ready()

    assert starter.attempts == []
    assert record.state == TaskState.CANCELLED
    assert len(results) == 1
    assert queue.is_shutdown is True
    assert queue.active_count == 0
    assert queue.waiting_count == 0
    assert queue.retry_wait_count == 0
    assert queue.has_scheduled_dispatch is False
    assert queue.has_work is False


def test_starter_synchronous_cancel_aborts_returned_handle_and_cleans_queue():
    queue, _, scheduler = _queue()
    results = []
    returned_handle = FakeAbortable()
    record_holder: list[TaskRecord] = []

    def starter(attempt_number, completion):
        queue.cancel(record_holder[0].identifier)
        return returned_handle

    record = queue.submit(TaskKind.METADATA, starter, results.append)
    record_holder.append(record)
    scheduler.run_ready()

    assert returned_handle.abort_count == 1
    assert record.state == TaskState.CANCELLED
    assert len(results) == 1
    assert queue.active_count == 0
    assert queue.waiting_count == 0
    assert queue.retry_wait_count == 0
    assert queue.has_scheduled_dispatch is False
    assert queue.has_work is False


def test_starter_synchronous_success_does_not_abort_returned_handle():
    queue, _, scheduler = _queue()
    results = []
    returned_handle = FakeAbortable()

    def starter(attempt_number, completion):
        completion(TaskAttemptResult.success("done"))
        return returned_handle

    record = queue.submit(TaskKind.METADATA, starter, results.append)
    scheduler.run_ready()

    assert returned_handle.abort_count == 0
    assert record.state == TaskState.SUCCEEDED
    assert len(results) == 1
    assert results[0].value == "done"
    assert queue.active_count == 0
    assert queue.has_work is False


def test_metadata_and_media_concurrency_limits_are_independent():
    config = TaskQueueConfig(
        capacity=8,
        metadata_concurrency=1,
        media_concurrency=2,
        minimum_interval_seconds=0,
    )
    queue, clock, scheduler = _queue(config)
    metadata_one = ControlledStarter(clock)
    metadata_two = ControlledStarter(clock)
    media_one = ControlledStarter(clock)
    media_two = ControlledStarter(clock)
    media_three = ControlledStarter(clock)

    queue.submit(TaskKind.METADATA, metadata_one, lambda result: None)
    queue.submit(TaskKind.METADATA, metadata_two, lambda result: None)
    queue.submit(TaskKind.MEDIA, media_one, lambda result: None)
    queue.submit(TaskKind.MEDIA, media_two, lambda result: None)
    queue.submit(TaskKind.MEDIA, media_three, lambda result: None)
    scheduler.run_ready()

    assert len(metadata_one.attempts) == 1
    assert len(metadata_two.attempts) == 0
    assert len(media_one.attempts) == 1
    assert len(media_two.attempts) == 1
    assert len(media_three.attempts) == 0
    assert queue.active_count_for(TaskKind.METADATA) == 1
    assert queue.active_count_for(TaskKind.MEDIA) == 2

    metadata_one.attempts[0].completion(TaskAttemptResult.success())
    media_one.attempts[0].completion(TaskAttemptResult.failure("failed"))
    scheduler.run_ready()

    assert len(metadata_two.attempts) == 1
    assert len(media_three.attempts) == 1


def test_minimum_interval_uses_monotonic_clock_for_every_attempt():
    config = TaskQueueConfig(
        capacity=4,
        metadata_concurrency=3,
        media_concurrency=3,
        minimum_interval_seconds=2,
    )
    queue, clock, scheduler = _queue(config)
    starters = [ControlledStarter(clock) for _ in range(3)]
    for starter in starters:
        queue.submit(TaskKind.METADATA, starter, lambda result: None)

    scheduler.run_ready()
    assert [len(starter.attempts) for starter in starters] == [1, 0, 0]
    scheduler.advance(1.99)
    assert [len(starter.attempts) for starter in starters] == [1, 0, 0]
    scheduler.advance(0.01)
    scheduler.advance(2)

    assert [starter.attempts[0].started_at for starter in starters] == [0, 2, 4]


def test_retry_after_seconds_releases_slot_and_retry_obeys_interval():
    config = TaskQueueConfig(
        capacity=3,
        metadata_concurrency=1,
        media_concurrency=1,
        minimum_interval_seconds=2,
        max_retries=1,
        base_backoff_seconds=1,
        maximum_retry_delay_seconds=10,
    )
    queue, clock, scheduler = _queue(config)
    starter = ControlledStarter(clock)
    results = []
    record = queue.submit(TaskKind.METADATA, starter, results.append)
    scheduler.run_ready()

    starter.attempts[0].completion(
        TaskAttemptResult.failure(
            "limited",
            error_code="http",
            http_status=429,
            retry_after="1",
        )
    )

    assert record.state == TaskState.RETRY_WAIT
    assert queue.active_count == 0
    assert queue.retry_wait_count == 1
    scheduler.advance(1)
    assert len(starter.attempts) == 1
    scheduler.advance(1)
    assert len(starter.attempts) == 2
    assert starter.attempts[1].started_at == 2

    starter.attempts[1].completion(TaskAttemptResult.success("ok"))
    assert record.state == TaskState.SUCCEEDED
    assert results[0].attempts == 2


def test_retry_after_http_date_and_expired_date_parsing():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    future = format_datetime(now + timedelta(seconds=12), usegmt=True)
    past = format_datetime(now - timedelta(seconds=12), usegmt=True)

    assert parse_retry_after(
        "7",
        now_utc=now,
    ) == 7
    assert parse_retry_after(
        "999",
        now_utc=now,
    ) == 999
    assert parse_retry_after(
        future,
        now_utc=now,
    ) == 12
    assert parse_retry_after(
        past,
        now_utc=now,
    ) == 0
    assert parse_retry_after(
        "invalid",
        now_utc=now,
    ) is None


@pytest.mark.parametrize(
    ("retry_after", "expected_delay"),
    [
        ("Thu, 01 Jan 2026 00:00:06 GMT", 6),
        ("invalid", 3),
    ],
)
def test_http_date_and_invalid_retry_after_drive_scheduled_retry(
    retry_after,
    expected_delay,
):
    config = TaskQueueConfig(
        minimum_interval_seconds=0,
        max_retries=1,
        base_backoff_seconds=3,
        maximum_retry_delay_seconds=20,
    )
    queue, clock, scheduler = _queue(config)
    starter = ControlledStarter(clock)
    queue.submit(TaskKind.METADATA, starter, lambda result: None)
    scheduler.run_ready()

    starter.attempts[0].completion(
        TaskAttemptResult.failure(
            "busy",
            http_status=503,
            retry_after=retry_after,
        )
    )
    scheduler.advance(expected_delay - 0.01)
    assert len(starter.attempts) == 1
    scheduler.advance(0.01)

    assert len(starter.attempts) == 2
    assert starter.attempts[1].started_at == expected_delay


@pytest.mark.parametrize(
    ("retry_after", "expected_delay", "configured_maximum"),
    [
        ("45", 45, 30),
        ("Thu, 01 Jan 2026 00:00:40 GMT", 40, 2),
    ],
)
def test_valid_retry_after_is_not_capped_by_fallback_maximum(
    retry_after,
    expected_delay,
    configured_maximum,
):
    config = TaskQueueConfig(
        minimum_interval_seconds=0,
        max_retries=1,
        base_backoff_seconds=1,
        maximum_retry_delay_seconds=configured_maximum,
    )
    queue, clock, scheduler = _queue(config)
    starter = ControlledStarter(clock)
    queue.submit(TaskKind.METADATA, starter, lambda result: None)
    scheduler.run_ready()

    starter.attempts[0].completion(
        TaskAttemptResult.failure(
            "busy",
            http_status=503,
            retry_after=retry_after,
        )
    )
    scheduler.advance(expected_delay - 0.01)
    assert len(starter.attempts) == 1
    scheduler.advance(0.01)

    assert len(starter.attempts) == 2
    assert starter.attempts[1].started_at == expected_delay


def test_missing_retry_after_uses_capped_backoff_and_n_retries_mean_n_plus_one():
    config = TaskQueueConfig(
        capacity=2,
        metadata_concurrency=1,
        media_concurrency=1,
        minimum_interval_seconds=0,
        max_retries=2,
        base_backoff_seconds=2,
        maximum_retry_delay_seconds=3,
    )
    queue, clock, scheduler = _queue(config)
    starter = ControlledStarter(clock)
    results = []
    record = queue.submit(TaskKind.METADATA, starter, results.append)
    scheduler.run_ready()

    starter.attempts[0].completion(
        TaskAttemptResult.failure("busy", error_code="http", http_status=503)
    )
    scheduler.advance(1.99)
    assert len(starter.attempts) == 1
    scheduler.advance(0.01)

    starter.attempts[1].completion(
        TaskAttemptResult.failure("busy", error_code="http", http_status=503)
    )
    scheduler.advance(2.99)
    assert len(starter.attempts) == 2
    scheduler.advance(0.01)

    starter.attempts[2].completion(
        TaskAttemptResult.failure("busy", error_code="http", http_status=503)
    )

    assert len(starter.attempts) == 3
    assert [item.started_at for item in starter.attempts] == [0, 2, 5]
    assert record.state == TaskState.FAILED
    assert len(results) == 1
    assert results[0].retry_exhausted is True
    assert results[0].attempts == 3


def test_only_429_and_503_are_retried():
    queue, clock, scheduler = _queue(
        TaskQueueConfig(
            minimum_interval_seconds=0,
            max_retries=3,
            base_backoff_seconds=0,
        )
    )
    starter = ControlledStarter(clock)
    results = []
    record = queue.submit(TaskKind.METADATA, starter, results.append)
    scheduler.run_ready()

    starter.attempts[0].completion(
        TaskAttemptResult.failure("server error", http_status=500)
    )
    scheduler.run_ready()

    assert record.state == TaskState.FAILED
    assert len(starter.attempts) == 1
    assert len(results) == 1


def test_cancelling_waiting_active_and_retry_tasks_prevents_late_restart():
    config = TaskQueueConfig(
        capacity=4,
        metadata_concurrency=1,
        media_concurrency=1,
        minimum_interval_seconds=0,
        max_retries=2,
        base_backoff_seconds=10,
    )
    queue, clock, scheduler = _queue(config)
    first = ControlledStarter(clock)
    waiting = ControlledStarter(clock)
    retrying = ControlledStarter(clock)
    final_results = []

    first_record = queue.submit(TaskKind.METADATA, first, final_results.append)
    waiting_record = queue.submit(
        TaskKind.METADATA,
        waiting,
        final_results.append,
    )
    retry_record = queue.submit(TaskKind.MEDIA, retrying, final_results.append)
    scheduler.run_ready()
    retrying.attempts[0].completion(
        TaskAttemptResult.failure("limited", http_status=429)
    )

    assert queue.cancel(waiting_record.identifier) is True
    assert queue.cancel(first_record.identifier) is True
    assert queue.cancel(retry_record.identifier) is True
    assert first.attempts[0].abortable.abort_count == 1
    assert waiting.attempts == []

    first.attempts[0].completion(TaskAttemptResult.success("late"))
    scheduler.advance(20)

    assert first_record.state == TaskState.CANCELLED
    assert waiting_record.state == TaskState.CANCELLED
    assert retry_record.state == TaskState.CANCELLED
    assert len(retrying.attempts) == 1
    assert len(final_results) == 3
    assert queue.has_work is False


def test_shutdown_is_idempotent_and_clears_work_timers_and_active_handles():
    config = TaskQueueConfig(
        capacity=4,
        metadata_concurrency=1,
        media_concurrency=1,
        minimum_interval_seconds=0,
        max_retries=2,
        base_backoff_seconds=10,
    )
    queue, clock, scheduler = _queue(config)
    active = ControlledStarter(clock)
    waiting = ControlledStarter(clock)
    retrying = ControlledStarter(clock)
    results = []

    queue.submit(TaskKind.METADATA, active, results.append)
    queue.submit(TaskKind.METADATA, waiting, results.append)
    queue.submit(TaskKind.MEDIA, retrying, results.append)
    scheduler.run_ready()
    retrying.attempts[0].completion(
        TaskAttemptResult.failure("limited", http_status=503)
    )

    queue.shutdown()
    queue.shutdown()
    scheduler.advance(30)

    assert active.attempts[0].abortable.abort_count == 1
    assert waiting.attempts == []
    assert len(retrying.attempts) == 1
    assert len(results) == 3
    assert queue.waiting_count == 0
    assert queue.retry_wait_count == 0
    assert queue.active_count == 0
    assert queue.has_scheduled_dispatch is False
    assert scheduler.active_call_count == 0
    assert queue.has_work is False
