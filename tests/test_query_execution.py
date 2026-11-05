from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import parse_qs, urlsplit

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QNetworkReply

from danbooru_multi_search.danbooru import (
    DanbooruMultiBranchExecutor,
    DanbooruPost,
    QueryExecutionErrorCode,
    QueryExecutionAccepted,
    QueryExecutionBound,
    QueryBranchPosition,
    QueryExecutionLimits,
    QueryExecutionState,
    QueryLimitReason,
    QueryPlan,
    RemoteOrder,
    build_rank_query_plan,
    build_latest_query_plan,
    build_standard_query_plan,
    parse_query_expression,
)
from danbooru_multi_search.tasks import (
    BoundedTaskQueue,
    TaskAttemptResult,
    TaskKind,
    TaskQueueConfig,
)


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.wall_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.value

    def utcnow(self) -> datetime:
        return self.wall_time

    def advance_only(self, seconds: float) -> None:
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
        self.clock.advance_only(seconds)
        self.run_ready()

    @property
    def active_call_count(self) -> int:
        return sum(call.active for call in self.calls)


class SynchronousDeadlineScheduler:
    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.returned_call: ManualCall | None = None

    def call_later(self, delay_seconds: float, callback) -> ManualCall:
        call = ManualCall(self.clock.monotonic() + delay_seconds, callback)
        self.returned_call = call
        self.clock.advance_only(delay_seconds)
        call.active = False
        callback()
        return call


@dataclass(slots=True)
class FakeAbortable:
    abort_count: int = 0

    def abort(self) -> None:
        self.abort_count += 1


@dataclass(slots=True)
class PageAttempt:
    tags: str
    page: int
    limit: int
    attempt_number: int
    completion: Callable[[TaskAttemptResult], None]
    abortable: FakeAbortable


class ControlledPageFactory:
    def __init__(self) -> None:
        self.attempts: list[PageAttempt] = []
        self.synchronous_results: list[TaskAttemptResult] = []
        self.raise_error: Exception | None = None

    def __call__(self, request, attempt_number, completion):
        if self.raise_error is not None:
            raise self.raise_error
        query = parse_qs(
            urlsplit(request.url().toString()).query,
            keep_blank_values=True,
        )
        abortable = FakeAbortable()
        attempt = PageAttempt(
            tags=query["tags"][0],
            page=int(query["page"][0]),
            limit=int(query["limit"][0]),
            attempt_number=attempt_number,
            completion=completion,
            abortable=abortable,
        )
        self.attempts.append(attempt)
        if self.synchronous_results:
            completion(self.synchronous_results.pop(0))
        return abortable


class FakeReply(QObject):
    finished = Signal()

    def __init__(self, payload: bytes) -> None:
        super().__init__()
        self.payload = payload
        self.abort_count = 0

    def abort(self) -> None:
        self.abort_count += 1

    def readAll(self) -> bytes:
        return self.payload

    def error(self):
        return QNetworkReply.NetworkError.NoError

    def errorString(self) -> str:
        return ""

    def attribute(self, attribute):
        return 200

    def rawHeader(self, header_name) -> bytes:
        return b""


class FakeNetworkManager:
    def __init__(self, reply: FakeReply) -> None:
        self.reply = reply
        self.requests = []

    def get(self, request):
        self.requests.append(request)
        return self.reply


@dataclass(slots=True)
class Harness:
    executor: DanbooruMultiBranchExecutor
    queue: BoundedTaskQueue
    clock: ManualClock
    scheduler: ManualScheduler
    factory: ControlledPageFactory
    snapshots: list

    def dispatch(self) -> None:
        self.scheduler.run_ready()

    def succeed(self, posts: tuple[DanbooruPost, ...], *, dispatch=True) -> None:
        self.factory.attempts[-1].completion(TaskAttemptResult.success(posts))
        if dispatch:
            self.dispatch()

    def fail(
        self,
        message: str,
        *,
        error_code: str = "http",
        http_status: int | None = 500,
        retry_after: str | None = None,
        dispatch=True,
    ) -> None:
        self.factory.attempts[-1].completion(
            TaskAttemptResult.failure(
                message,
                error_code=error_code,
                http_status=http_status,
                retry_after=retry_after,
            )
        )
        if dispatch:
            self.dispatch()


def _post(
    post_id: int,
    *tags: str,
    score: int | None = 0,
) -> DanbooruPost:
    return DanbooruPost(
        id=post_id,
        preview_url=f"https://example.invalid/{post_id}.jpg",
        large_file_url=None,
        file_url=f"https://example.invalid/{post_id}-full.jpg",
        score=score,
        fav_count=0,
        rating="g",
        width=10,
        height=10,
        file_ext="jpg",
        general_tags=tuple(tags),
    )


def _plan(source: str):
    return build_standard_query_plan(parse_query_expression(source))


def _harness(
    plan,
    *,
    limits: QueryExecutionLimits | None = None,
    config: TaskQueueConfig | None = None,
    auto_start: bool = True,
) -> Harness:
    clock = ManualClock()
    scheduler = ManualScheduler(clock)
    queue = BoundedTaskQueue(
        config
        or TaskQueueConfig(
            capacity=64,
            metadata_concurrency=1,
            media_concurrency=1,
            minimum_interval_seconds=0,
            max_retries=0,
            base_backoff_seconds=0,
        ),
        clock=clock,
        scheduler=scheduler,
    )
    factory = ControlledPageFactory()
    executor = DanbooruMultiBranchExecutor(
        "https://example.invalid",
        task_queue=queue,
        clock=clock,
        scheduler=scheduler,
        attempt_factory=factory,
    )
    snapshots = []
    executor.snapshotChanged.connect(snapshots.append)
    harness = Harness(executor, queue, clock, scheduler, factory, snapshots)
    if auto_start:
        executor.start(plan, limits)
        harness.dispatch()
    return harness


def _request_positions(harness: Harness) -> list[tuple[str, int]]:
    return [(attempt.tags, attempt.page) for attempt in harness.factory.attempts]


def test_three_branches_rotate_strictly_and_skip_short_and_empty_branches():
    harness = _harness(
        _plan("a OR b OR c"),
        limits=QueryExecutionLimits(page_size=2, branch_page_limit=3),
    )

    harness.succeed((_post(1, "a"), _post(2, "a")))
    harness.succeed((_post(3, "b"), _post(4, "b")))
    harness.succeed(())
    harness.succeed((_post(5, "a"),))
    harness.succeed(())

    assert _request_positions(harness) == [
        ("a", 1),
        ("b", 1),
        ("c", 1),
        ("a", 2),
        ("b", 2),
    ]
    assert harness.executor.snapshot.state == QueryExecutionState.COMPLETE
    progress = harness.executor.snapshot.branch_progress
    assert [(item.successful_pages, item.natural_end) for item in progress] == [
        (2, True),
        (2, True),
        (1, True),
    ]


def test_full_tenth_page_is_page_limited_but_other_branches_finish():
    harness = _harness(
        _plan("a OR b"),
        limits=QueryExecutionLimits(
            page_size=1,
            candidate_limit=100,
            branch_page_limit=2,
        ),
    )

    harness.succeed((_post(1, "a"),))
    harness.succeed((_post(2, "b"),))
    harness.succeed((_post(3, "a"),))
    harness.succeed(())

    snapshot = harness.executor.snapshot
    assert _request_positions(harness) == [
        ("a", 1),
        ("b", 1),
        ("a", 2),
        ("b", 2),
    ]
    assert snapshot.state == QueryExecutionState.PARTIAL_LIMITED
    assert snapshot.limit_reason == QueryLimitReason.PAGE_LIMIT
    assert snapshot.branch_progress[0].page_limited is True
    assert snapshot.branch_progress[1].natural_end is True


def test_last_allowed_short_page_finishes_naturally():
    harness = _harness(
        _plan("cat"),
        limits=QueryExecutionLimits(page_size=2, branch_page_limit=2),
    )
    harness.succeed((_post(1, "cat"), _post(2, "cat")))
    harness.succeed((_post(3, "cat"),))

    assert harness.executor.snapshot.state == QueryExecutionState.COMPLETE
    assert harness.executor.snapshot.branch_progress[0].successful_pages == 2
    assert harness.executor.snapshot.branch_progress[0].natural_end is True


@pytest.mark.parametrize(
    ("tenth_page", "expected_state", "expected_reason"),
    [
        ((), QueryExecutionState.COMPLETE, None),
        (
            (_post(10, "cat"),),
            QueryExecutionState.PARTIAL_LIMITED,
            QueryLimitReason.PAGE_LIMIT,
        ),
    ],
)
def test_default_tenth_page_short_or_full_boundary(
    tenth_page,
    expected_state,
    expected_reason,
):
    harness = _harness(
        _plan("cat"),
        limits=QueryExecutionLimits(page_size=1, branch_page_limit=10),
    )
    for post_id in range(1, 10):
        harness.succeed((_post(post_id, "cat"),))
    harness.succeed(tenth_page)

    snapshot = harness.executor.snapshot
    assert snapshot.state == expected_state
    assert snapshot.limit_reason == expected_reason
    assert snapshot.branch_progress[0].successful_pages == 10


def test_oversized_page_is_atomic_invalid_response_failure():
    harness = _harness(
        _plan("cat"),
        limits=QueryExecutionLimits(page_size=1),
    )
    harness.succeed((_post(1, "cat"), _post(2, "cat")))

    snapshot = harness.executor.snapshot
    assert snapshot.state == QueryExecutionState.FAILED
    assert snapshot.failure.error_code == QueryExecutionErrorCode.INVALID_RESPONSE
    assert snapshot.counts.remote_rows == 0
    assert snapshot.counts.unique_candidates == 0
    assert snapshot.branch_progress[0].successful_pages == 0


def test_default_qt_attempt_uses_complete_tag_parser_for_m2_pages():
    raw = {
        "id": 1,
        "preview_file_url": "https://example.invalid/1.jpg",
        "large_file_url": None,
        "file_url": "https://example.invalid/1-full.jpg",
        "score": 1,
        "fav_count": 1,
        "rating": "g",
        "image_width": 10,
        "image_height": 10,
        "file_ext": "jpg",
    }
    reply = FakeReply(json.dumps([raw]).encode("utf-8"))
    manager = FakeNetworkManager(reply)
    clock = ManualClock()
    scheduler = ManualScheduler(clock)
    queue = BoundedTaskQueue(
        TaskQueueConfig(minimum_interval_seconds=0, max_retries=0),
        clock=clock,
        scheduler=scheduler,
    )
    executor = DanbooruMultiBranchExecutor(
        "https://example.invalid",
        network_manager=manager,
        task_queue=queue,
        clock=clock,
        scheduler=scheduler,
    )
    executor.start(_plan("cat"))
    scheduler.run_ready()
    reply.finished.emit()

    snapshot = executor.snapshot
    assert len(manager.requests) == 1
    assert snapshot.state == QueryExecutionState.FAILED
    assert snapshot.failure.error_code == QueryExecutionErrorCode.INVALID_RESPONSE
    assert snapshot.counts.remote_rows == 0


def test_dedup_keeps_first_metadata_and_full_ast_is_evaluated_once(monkeypatch):
    import danbooru_multi_search.danbooru.query_execution as execution_module

    real_evaluator = execution_module.evaluate_post_query
    evaluated_ids: list[int] = []

    def counting_evaluator(expression, post):
        evaluated_ids.append(post.id)
        return real_evaluator(expression, post)

    monkeypatch.setattr(execution_module, "evaluate_post_query", counting_evaluator)
    harness = _harness(
        _plan("(cat OR dog) AND base"),
        limits=QueryExecutionLimits(page_size=3),
    )
    first = _post(1, "dog", "base", score=10)
    duplicate_changed = _post(1, "cat", "base", score=99)
    nonmatch = _post(2, "cat")
    harness.succeed((first, duplicate_changed, nonmatch))
    harness.succeed((duplicate_changed, _post(3, "dog", "base")))
    harness.succeed(())

    snapshot = harness.executor.snapshot
    assert snapshot.state == QueryExecutionState.COMPLETE
    assert snapshot.counts.remote_rows == 5
    assert snapshot.counts.unique_candidates == 3
    assert snapshot.counts.local_matches == 2
    assert [post.id for post in snapshot.posts] == [1, 3]
    assert snapshot.posts[0].score == 10
    assert evaluated_ids == [1, 2, 3]


def test_later_duplicate_cannot_turn_first_nonmatch_into_match():
    harness = _harness(
        _plan("cat"),
        limits=QueryExecutionLimits(page_size=2),
    )
    harness.succeed((_post(1, "dog", score=1), _post(1, "cat", score=99)))
    harness.succeed(())

    snapshot = harness.executor.snapshot
    assert snapshot.counts.remote_rows == 2
    assert snapshot.counts.unique_candidates == 1
    assert snapshot.counts.local_matches == 0
    assert snapshot.posts == ()


def test_local_evaluation_error_rolls_back_entire_page(monkeypatch):
    import danbooru_multi_search.danbooru.query_execution as execution_module

    calls = 0

    def failing_evaluator(expression, post):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("cannot evaluate")
        return True

    monkeypatch.setattr(execution_module, "evaluate_post_query", failing_evaluator)
    harness = _harness(
        _plan("cat"),
        limits=QueryExecutionLimits(page_size=3),
    )
    harness.succeed((_post(1, "cat"), _post(2, "cat")))

    snapshot = harness.executor.snapshot
    assert snapshot.state == QueryExecutionState.FAILED
    assert snapshot.failure.error_code == QueryExecutionErrorCode.INVALID_RESPONSE
    assert snapshot.counts.remote_rows == 0
    assert snapshot.counts.unique_candidates == 0
    assert snapshot.counts.local_matches == 0
    assert snapshot.posts == ()
    assert snapshot.branch_progress[0].successful_pages == 0


def test_tagless_rank_plan_matches_every_candidate_in_returned_order():
    harness = _harness(build_rank_query_plan(None))
    harness.succeed((_post(3, "dog"), _post(1, "cat")))

    snapshot = harness.executor.snapshot
    assert snapshot.state == QueryExecutionState.COMPLETE
    assert [post.id for post in snapshot.posts] == [3, 1]
    assert snapshot.counts.local_matches == 2
    assert harness.factory.attempts[0].tags == "order:rank"


def test_candidate_limit_stops_at_cap_counts_whole_page_and_beats_page_limit():
    harness = _harness(
        _plan("cat"),
        limits=QueryExecutionLimits(
            page_size=3,
            candidate_limit=2,
            branch_page_limit=1,
        ),
    )
    harness.succeed(tuple(_post(index, "cat") for index in (1, 2, 3)))

    snapshot = harness.executor.snapshot
    assert snapshot.state == QueryExecutionState.PARTIAL_LIMITED
    assert snapshot.limit_reason == QueryLimitReason.CANDIDATE_LIMIT
    assert snapshot.counts.remote_rows == 3
    assert snapshot.counts.unique_candidates == 2
    assert snapshot.counts.local_matches == 2
    assert snapshot.branch_progress[0].successful_pages == 1


def test_exact_candidate_cap_can_complete_on_final_short_page():
    harness = _harness(
        _plan("cat"),
        limits=QueryExecutionLimits(page_size=3, candidate_limit=2),
    )
    harness.succeed((_post(1, "cat"), _post(2, "cat")))

    assert harness.executor.snapshot.state == QueryExecutionState.COMPLETE
    assert harness.executor.snapshot.counts.unique_candidates == 2


def test_exact_candidate_cap_with_unscanned_page_is_candidate_limited():
    harness = _harness(
        _plan("cat"),
        limits=QueryExecutionLimits(page_size=2, candidate_limit=2),
    )
    harness.succeed((_post(1, "cat"), _post(2, "cat")))

    assert harness.executor.snapshot.state == QueryExecutionState.PARTIAL_LIMITED
    assert harness.executor.snapshot.limit_reason == QueryLimitReason.CANDIDATE_LIMIT


@pytest.mark.parametrize("expected_count", [999, 1_000])
def test_large_candidate_counts_complete_when_every_branch_is_short(expected_count):
    tags = [f"tag_{index}" for index in range(6)]
    harness = _harness(
        _plan(" OR ".join(tags)),
        limits=QueryExecutionLimits(page_size=200, candidate_limit=1_000),
    )
    sizes = [199, 199, 199, 199, 199, expected_count - 995]
    next_id = 1
    for tag, size in zip(tags, sizes, strict=True):
        posts = tuple(_post(post_id, tag) for post_id in range(next_id, next_id + size))
        next_id += size
        harness.succeed(posts)

    snapshot = harness.executor.snapshot
    assert snapshot.state == QueryExecutionState.COMPLETE
    assert snapshot.counts.remote_rows == expected_count
    assert snapshot.counts.unique_candidates == expected_count
    assert snapshot.counts.local_matches == expected_count


def test_one_thousand_and_first_candidate_is_not_added_but_row_is_counted():
    tags = [f"tag_{index}" for index in range(5)]
    harness = _harness(
        _plan(" OR ".join(tags)),
        limits=QueryExecutionLimits(page_size=200, candidate_limit=1_000),
    )
    next_id = 1
    for tag, size in zip(tags, [200, 200, 200, 200, 199], strict=True):
        posts = tuple(_post(post_id, tag) for post_id in range(next_id, next_id + size))
        next_id += size
        harness.succeed(posts)
    harness.succeed((_post(next_id, tags[0]), _post(next_id + 1, tags[0])))

    snapshot = harness.executor.snapshot
    assert snapshot.state == QueryExecutionState.PARTIAL_LIMITED
    assert snapshot.limit_reason == QueryLimitReason.CANDIDATE_LIMIT
    assert snapshot.counts.remote_rows == 1_001
    assert snapshot.counts.unique_candidates == 1_000
    assert snapshot.counts.local_matches == 1_000


def test_callback_before_time_boundary_is_applied_but_boundary_discards_page():
    before = _harness(
        _plan("cat"),
        limits=QueryExecutionLimits(active_time_limit_seconds=1),
    )
    before.clock.advance_only(0.999)
    before.succeed((_post(1, "cat"),))
    assert before.executor.snapshot.state == QueryExecutionState.COMPLETE
    assert before.executor.snapshot.counts.unique_candidates == 1

    at_boundary = _harness(
        _plan("cat"),
        limits=QueryExecutionLimits(active_time_limit_seconds=1),
    )
    at_boundary.clock.advance_only(1)
    at_boundary.succeed((_post(2, "cat"),))
    snapshot = at_boundary.executor.snapshot
    assert snapshot.state == QueryExecutionState.PARTIAL_LIMITED
    assert snapshot.limit_reason == QueryLimitReason.TIME_LIMIT
    assert snapshot.counts.remote_rows == 0
    assert snapshot.branch_progress[0].successful_pages == 0


def test_default_fifty_nine_point_nine_nine_nine_and_sixty_second_boundary():
    before = _harness(_plan("cat"))
    before.clock.advance_only(59.999)
    before.succeed((_post(1, "cat"),))
    assert before.executor.snapshot.state == QueryExecutionState.COMPLETE

    boundary = _harness(_plan("cat"))
    boundary.clock.advance_only(60)
    boundary.succeed((_post(2, "cat"),))
    assert boundary.executor.snapshot.state == QueryExecutionState.PARTIAL_LIMITED
    assert boundary.executor.snapshot.limit_reason == QueryLimitReason.TIME_LIMIT
    assert boundary.executor.snapshot.counts.remote_rows == 0


def test_failure_callback_at_time_boundary_is_discarded_for_time_limit():
    harness = _harness(
        _plan("cat"),
        limits=QueryExecutionLimits(active_time_limit_seconds=1),
    )
    harness.clock.advance_only(1)
    harness.fail("server", http_status=500)

    snapshot = harness.executor.snapshot
    assert snapshot.state == QueryExecutionState.PARTIAL_LIMITED
    assert snapshot.limit_reason == QueryLimitReason.TIME_LIMIT
    assert snapshot.failure is None


def test_time_limit_wins_when_atomic_page_processing_also_hits_other_limits(
    monkeypatch,
):
    import danbooru_multi_search.danbooru.query_execution as execution_module

    harness = _harness(
        _plan("cat"),
        limits=QueryExecutionLimits(
            page_size=2,
            candidate_limit=1,
            branch_page_limit=1,
            active_time_limit_seconds=1,
        ),
    )
    real_evaluator = execution_module.evaluate_post_query

    def advancing_evaluator(expression, post):
        harness.clock.advance_only(1)
        return real_evaluator(expression, post)

    monkeypatch.setattr(execution_module, "evaluate_post_query", advancing_evaluator)
    harness.succeed((_post(1, "cat"), _post(2, "cat")))

    snapshot = harness.executor.snapshot
    assert snapshot.state == QueryExecutionState.PARTIAL_LIMITED
    assert snapshot.limit_reason == QueryLimitReason.TIME_LIMIT
    assert snapshot.counts.remote_rows == 2
    assert snapshot.counts.unique_candidates == 1
    assert snapshot.branch_progress[0].successful_pages == 1


def test_waiting_active_and_page_gap_cancellation_are_terminal_and_preserve_data():
    waiting = _harness(_plan("cat"), auto_start=False)
    waiting.executor.start(_plan("cat"))
    assert waiting.executor.snapshot.state == QueryExecutionState.WAITING
    assert waiting.executor.cancel() is True
    waiting.dispatch()
    assert waiting.factory.attempts == []
    assert waiting.executor.snapshot.state == QueryExecutionState.PARTIAL_CANCELLED

    active = _harness(_plan("cat"))
    assert active.executor.cancel() is True
    assert active.factory.attempts[0].abortable.abort_count == 1
    assert active.executor.snapshot.state == QueryExecutionState.PARTIAL_CANCELLED

    gap = _harness(
        _plan("cat"),
        limits=QueryExecutionLimits(page_size=1),
    )
    gap.succeed((_post(1, "cat"),), dispatch=False)
    assert gap.executor.snapshot.state == QueryExecutionState.RUNNING
    assert gap.executor.cancel() is True
    gap.dispatch()
    assert len(gap.factory.attempts) == 1
    assert gap.executor.snapshot.counts.local_matches == 1
    assert gap.executor.snapshot.state == QueryExecutionState.PARTIAL_CANCELLED


def test_waiting_snapshot_listener_can_cancel_before_attempt_factory_runs():
    harness = _harness(_plan("cat"), auto_start=False)

    def cancel_waiting(snapshot) -> None:
        if snapshot.state == QueryExecutionState.WAITING:
            harness.executor.cancel()

    harness.executor.snapshotChanged.connect(cancel_waiting)
    harness.executor.start(_plan("cat"))
    harness.dispatch()

    assert harness.factory.attempts == []
    assert harness.executor.snapshot.state == QueryExecutionState.PARTIAL_CANCELLED


def test_running_snapshot_listener_can_replace_request_before_factory_runs():
    harness = _harness(_plan("old"), auto_start=False)
    replaced = False

    def replace_running(snapshot) -> None:
        nonlocal replaced
        if snapshot.state == QueryExecutionState.RUNNING and not replaced:
            replaced = True
            harness.executor.start(_plan("new"))

    harness.executor.snapshotChanged.connect(replace_running)
    harness.executor.start(_plan("old"))
    harness.dispatch()

    assert replaced is True
    assert _request_positions(harness) == [("new", 1)]
    harness.succeed((_post(2, "new"),))
    assert harness.executor.snapshot.state == QueryExecutionState.COMPLETE
    assert [post.id for post in harness.executor.snapshot.posts] == [2]


def test_user_cancel_during_retry_wait_prevents_retry():
    harness = _harness(
        _plan("cat"),
        config=TaskQueueConfig(
            minimum_interval_seconds=0,
            max_retries=1,
            base_backoff_seconds=10,
        ),
    )
    harness.fail("busy", http_status=503, retry_after="10", dispatch=False)
    assert harness.executor.snapshot.state == QueryExecutionState.RETRY_WAIT
    assert harness.executor.cancel() is True
    harness.scheduler.advance(20)

    assert len(harness.factory.attempts) == 1
    assert harness.executor.snapshot.state == QueryExecutionState.PARTIAL_CANCELLED


def test_deadline_cancels_retry_wait_and_prevents_automatic_retry():
    harness = _harness(
        _plan("cat"),
        limits=QueryExecutionLimits(active_time_limit_seconds=1),
        config=TaskQueueConfig(
            minimum_interval_seconds=0,
            max_retries=1,
            base_backoff_seconds=5,
        ),
    )
    harness.fail(
        "busy",
        http_status=503,
        retry_after="5",
        dispatch=False,
    )
    assert harness.executor.snapshot.state == QueryExecutionState.RETRY_WAIT
    harness.scheduler.advance(1)

    assert harness.executor.snapshot.state == QueryExecutionState.PARTIAL_LIMITED
    assert harness.executor.snapshot.limit_reason == QueryLimitReason.TIME_LIMIT
    assert len(harness.factory.attempts) == 1


def test_shared_queue_waiting_time_counts_toward_deadline():
    clock = ManualClock()
    scheduler = ManualScheduler(clock)
    queue = BoundedTaskQueue(
        TaskQueueConfig(
            capacity=2,
            metadata_concurrency=1,
            minimum_interval_seconds=0,
        ),
        clock=clock,
        scheduler=scheduler,
    )
    blocker = queue.submit(
        TaskKind.METADATA,
        lambda attempt, completion: FakeAbortable(),
        lambda result: None,
    )
    scheduler.run_ready()
    factory = ControlledPageFactory()
    executor = DanbooruMultiBranchExecutor(
        "https://example.invalid",
        task_queue=queue,
        clock=clock,
        scheduler=scheduler,
        attempt_factory=factory,
    )
    executor.start(
        _plan("cat"),
        QueryExecutionLimits(active_time_limit_seconds=1),
    )
    assert executor.snapshot.state == QueryExecutionState.WAITING

    scheduler.advance(1)

    assert factory.attempts == []
    assert executor.snapshot.state == QueryExecutionState.PARTIAL_LIMITED
    assert executor.snapshot.limit_reason == QueryLimitReason.TIME_LIMIT
    queue.cancel(blocker.identifier)


def test_queue_automatic_retry_uses_same_page_without_counting_failed_attempt():
    harness = _harness(
        _plan("cat"),
        config=TaskQueueConfig(
            minimum_interval_seconds=0,
            max_retries=1,
            base_backoff_seconds=0,
        ),
    )
    harness.fail("busy", http_status=503)
    assert _request_positions(harness) == [("cat", 1), ("cat", 1)]
    harness.succeed((_post(1, "cat"),))

    assert harness.executor.snapshot.state == QueryExecutionState.COMPLETE
    assert harness.executor.snapshot.branch_progress[0].successful_pages == 1


def test_429_retry_exhaustion_stays_on_page_then_reports_rate_limit():
    harness = _harness(
        _plan("cat"),
        config=TaskQueueConfig(
            minimum_interval_seconds=0,
            max_retries=1,
            base_backoff_seconds=0,
        ),
    )
    harness.fail("limited", http_status=429)
    harness.fail("limited again", http_status=429)

    assert _request_positions(harness) == [("cat", 1), ("cat", 1)]
    assert harness.executor.snapshot.state == QueryExecutionState.FAILED
    assert (
        harness.executor.snapshot.failure.error_code
        == QueryExecutionErrorCode.RATE_LIMITED
    )


def test_manual_retry_repeats_exact_failure_and_resumes_round_robin():
    harness = _harness(
        _plan("a OR b"),
        limits=QueryExecutionLimits(page_size=2),
    )
    harness.succeed((_post(1, "a"),))
    harness.fail("server", http_status=500)

    failed = harness.executor.snapshot
    assert failed.state == QueryExecutionState.PARTIAL_FAILED
    assert failed.failure.position.branch_index == 1
    assert failed.failure.position.page == 1
    assert failed.failure.api_tags == "b"
    assert harness.executor.retry_failed_page() is True
    harness.dispatch()
    harness.succeed((_post(2, "b"),))

    assert _request_positions(harness) == [("a", 1), ("b", 1), ("b", 1)]
    assert harness.executor.snapshot.state == QueryExecutionState.COMPLETE
    assert [post.id for post in harness.executor.snapshot.posts] == [1, 2]


def test_zero_match_natural_completion_and_zero_match_failure_states():
    complete = _harness(
        _plan("cat"),
        limits=QueryExecutionLimits(page_size=2),
    )
    complete.succeed((_post(1, "dog"),))
    assert complete.executor.snapshot.state == QueryExecutionState.COMPLETE
    assert complete.executor.snapshot.counts.unique_candidates == 1
    assert complete.executor.snapshot.counts.local_matches == 0

    failed = _harness(
        _plan("cat"),
        limits=QueryExecutionLimits(page_size=1),
    )
    failed.succeed((_post(2, "dog"),))
    failed.fail("server", http_status=500)
    assert failed.executor.snapshot.state == QueryExecutionState.FAILED
    assert failed.executor.snapshot.counts.unique_candidates == 1
    assert failed.executor.snapshot.counts.local_matches == 0


def test_page_limited_branch_does_not_hide_later_request_failure():
    harness = _harness(
        _plan("a OR b"),
        limits=QueryExecutionLimits(page_size=1, branch_page_limit=1),
    )
    harness.succeed((_post(1, "a"),))
    harness.fail("server", http_status=500)

    snapshot = harness.executor.snapshot
    assert snapshot.state == QueryExecutionState.PARTIAL_FAILED
    assert snapshot.limit_reason is None
    assert snapshot.failure.position.branch_index == 1
    assert snapshot.branch_progress[0].page_limited is True


def test_cancel_in_failure_state_is_noop_and_preserves_manual_retry():
    harness = _harness(_plan("cat"))
    harness.fail("server", http_status=500)
    failed = harness.executor.snapshot

    assert harness.executor.cancel() is False
    assert harness.executor.snapshot == failed
    assert harness.executor.retry_failed_page() is True


def test_failure_listener_can_retry_synchronously_without_old_state_overwrite():
    harness = _harness(_plan("cat"))
    retried = False

    def retry_failure(snapshot) -> None:
        nonlocal retried
        if snapshot.state == QueryExecutionState.FAILED and not retried:
            retried = True
            assert harness.executor.retry_failed_page() is True

    harness.executor.snapshotChanged.connect(retry_failure)
    harness.fail("server", http_status=500)
    harness.dispatch()

    assert retried is True
    assert _request_positions(harness) == [("cat", 1), ("cat", 1)]
    harness.succeed((_post(1, "cat"),))
    assert harness.executor.snapshot.state == QueryExecutionState.COMPLETE


def test_shutdown_from_failure_notification_preserves_terminal_snapshot():
    harness = _harness(_plan("cat"))

    def close_failure(snapshot) -> None:
        if snapshot.state == QueryExecutionState.FAILED:
            harness.executor.shutdown()

    harness.executor.snapshotChanged.connect(close_failure)
    harness.fail("server", http_status=500)

    snapshot = harness.executor.snapshot
    assert harness.executor.closed is True
    assert snapshot.state == QueryExecutionState.FAILED
    assert snapshot.failure.error_code == QueryExecutionErrorCode.HTTP
    assert harness.executor.retry_failed_page() is False


def test_manual_failure_pause_does_not_consume_active_time():
    harness = _harness(
        _plan("cat"),
        limits=QueryExecutionLimits(active_time_limit_seconds=1),
    )
    harness.clock.advance_only(0.5)
    harness.fail("server", http_status=500)
    assert harness.executor.snapshot.state == QueryExecutionState.FAILED

    harness.scheduler.advance(100)
    assert harness.executor.retry_failed_page() is True
    harness.dispatch()
    harness.clock.advance_only(0.499)
    harness.succeed((_post(1, "cat"),))
    assert harness.executor.snapshot.state == QueryExecutionState.COMPLETE


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (TaskAttemptResult.failure("auth", http_status=401), "authentication"),
        (TaskAttemptResult.failure("auth", http_status=403), "authentication"),
        (TaskAttemptResult.failure("rate", http_status=429), "rate_limited"),
        (TaskAttemptResult.failure("service", http_status=503), "http"),
        (TaskAttemptResult.failure("http", http_status=500), "http"),
        (TaskAttemptResult.failure("timeout", error_code="timeout"), "timeout"),
        (TaskAttemptResult.failure("network", error_code="network"), "network"),
        (
            TaskAttemptResult.failure("bad", error_code="invalid_response"),
            "invalid_response",
        ),
    ],
)
def test_final_failures_have_stable_classification(result, expected):
    harness = _harness(_plan("cat"))
    harness.factory.attempts[-1].completion(result)

    snapshot = harness.executor.snapshot
    assert snapshot.state == QueryExecutionState.FAILED
    assert snapshot.failure.error_code.value == expected


def test_attempt_factory_exception_is_request_start_failure():
    harness = _harness(_plan("cat"), auto_start=False)
    harness.factory.raise_error = RuntimeError("cannot start")
    harness.executor.start(_plan("cat"))
    harness.dispatch()

    assert harness.executor.snapshot.state == QueryExecutionState.FAILED
    assert (
        harness.executor.snapshot.failure.error_code
        == QueryExecutionErrorCode.REQUEST_START
    )


def test_first_page_queue_rejection_is_retryable_at_exact_position():
    clock = ManualClock()
    scheduler = ManualScheduler(clock)
    queue = BoundedTaskQueue(
        TaskQueueConfig(capacity=1, minimum_interval_seconds=0),
        clock=clock,
        scheduler=scheduler,
    )
    blocker = queue.submit(
        TaskKind.METADATA,
        lambda attempt, completion: FakeAbortable(),
        lambda result: None,
    )
    factory = ControlledPageFactory()
    executor = DanbooruMultiBranchExecutor(
        "https://example.invalid",
        task_queue=queue,
        clock=clock,
        scheduler=scheduler,
        attempt_factory=factory,
    )
    executor.start(_plan("cat"))

    snapshot = executor.snapshot
    assert snapshot.state == QueryExecutionState.FAILED
    assert snapshot.counts.unique_candidates == 0
    assert snapshot.failure.error_code == QueryExecutionErrorCode.QUEUE_REJECTED
    assert snapshot.failure.position.branch_index == 0
    assert snapshot.failure.position.page == 1

    queue.cancel(blocker.identifier)
    assert executor.retry_failed_page() is True
    scheduler.run_ready()
    factory.attempts[-1].completion(TaskAttemptResult.success((_post(1, "cat"),)))
    assert executor.snapshot.state == QueryExecutionState.COMPLETE


def test_already_closed_shared_queue_rejects_first_page_at_exact_position():
    harness = _harness(_plan("cat"), auto_start=False)
    accepted: list[QueryExecutionAccepted] = []
    harness.executor.firstPageAccepted.connect(accepted.append)
    harness.queue.shutdown()

    harness.executor.start(_plan("cat"))

    snapshot = harness.executor.snapshot
    assert snapshot.state == QueryExecutionState.FAILED
    assert snapshot.failure.error_code == QueryExecutionErrorCode.QUEUE_REJECTED
    assert snapshot.failure.position.branch_index == 0
    assert snapshot.failure.position.page == 1
    assert snapshot.counts.remote_rows == 0
    assert accepted == []


def test_external_shared_queue_cancel_is_visible_queue_rejection():
    harness = _harness(_plan("cat"))
    task_id = next(iter(harness.queue._entries))
    harness.queue.cancel(task_id, "external cancellation")

    snapshot = harness.executor.snapshot
    assert snapshot.state == QueryExecutionState.FAILED
    assert snapshot.failure.error_code == QueryExecutionErrorCode.QUEUE_REJECTED


def test_new_execution_aborts_and_isolates_old_success_and_failure():
    harness = _harness(_plan("old"))
    old = harness.factory.attempts[-1]
    old_id = harness.executor.snapshot.execution_id
    new_id = harness.executor.start(_plan("new"))
    harness.dispatch()

    assert new_id == old_id + 1
    assert old.abortable.abort_count == 1
    old.completion(TaskAttemptResult.success((_post(1, "old"),)))
    old.completion(TaskAttemptResult.failure("late", http_status=500))
    harness.factory.attempts[-1].completion(
        TaskAttemptResult.success((_post(2, "new"),))
    )

    snapshot = harness.executor.snapshot
    assert snapshot.execution_id == new_id
    assert snapshot.state == QueryExecutionState.COMPLETE
    assert [post.id for post in snapshot.posts] == [2]


def test_new_execution_cancels_old_automatic_retry_wait_without_restart():
    harness = _harness(
        _plan("old"),
        config=TaskQueueConfig(
            minimum_interval_seconds=0,
            max_retries=1,
            base_backoff_seconds=10,
        ),
    )
    harness.fail("busy", http_status=503, retry_after="10", dispatch=False)
    assert harness.executor.snapshot.state == QueryExecutionState.RETRY_WAIT

    harness.executor.start(_plan("new"))
    harness.dispatch()
    harness.scheduler.advance(20)

    assert _request_positions(harness) == [("old", 1), ("new", 1)]


def test_synchronous_completion_waits_for_task_identity_and_finishes_once():
    harness = _harness(_plan("cat"), auto_start=False)
    harness.factory.synchronous_results.append(
        TaskAttemptResult.success((_post(1, "cat"),))
    )
    harness.executor.start(_plan("cat"))
    harness.dispatch()

    terminal = [
        item
        for item in harness.snapshots
        if item.state == QueryExecutionState.COMPLETE
    ]
    assert len(terminal) == 1
    assert terminal[0].counts.local_matches == 1


def test_synchronous_full_page_completion_continues_without_recursive_start():
    harness = _harness(
        _plan("cat"),
        limits=QueryExecutionLimits(page_size=1),
        auto_start=False,
    )
    harness.factory.synchronous_results.extend(
        [
            TaskAttemptResult.success((_post(1, "cat"),)),
            TaskAttemptResult.success(()),
        ]
    )
    harness.executor.start(
        _plan("cat"),
        QueryExecutionLimits(page_size=1),
    )
    harness.dispatch()

    assert _request_positions(harness) == [("cat", 1), ("cat", 2)]
    assert harness.executor.snapshot.state == QueryExecutionState.COMPLETE


def test_page_snapshot_listener_cancel_prevents_next_page_submission():
    harness = _harness(
        _plan("cat"),
        limits=QueryExecutionLimits(page_size=1),
    )

    def cancel_after_page(snapshot) -> None:
        if (
            snapshot.state == QueryExecutionState.RUNNING
            and snapshot.counts.remote_rows == 1
        ):
            harness.executor.cancel()

    harness.executor.snapshotChanged.connect(cancel_after_page)
    harness.succeed((_post(1, "cat"),))

    assert len(harness.factory.attempts) == 1
    assert harness.executor.snapshot.state == QueryExecutionState.PARTIAL_CANCELLED


def test_page_snapshot_listener_new_execution_prevents_old_next_page():
    harness = _harness(
        _plan("old"),
        limits=QueryExecutionLimits(page_size=1),
    )
    replaced = False

    def replace_after_page(snapshot) -> None:
        nonlocal replaced
        if (
            not replaced
            and snapshot.state == QueryExecutionState.RUNNING
            and snapshot.counts.remote_rows == 1
        ):
            replaced = True
            harness.executor.start(_plan("new"))

    harness.executor.snapshotChanged.connect(replace_after_page)
    harness.succeed((_post(1, "old"),))

    assert replaced is True
    assert _request_positions(harness) == [("old", 1), ("new", 1)]
    harness.succeed((_post(2, "new"),))
    assert harness.executor.snapshot.state == QueryExecutionState.COMPLETE
    assert [post.id for post in harness.executor.snapshot.posts] == [2]


def test_late_success_after_user_cancel_does_not_replace_cancelled_snapshot():
    harness = _harness(_plan("cat"))
    attempt = harness.factory.attempts[-1]
    assert harness.executor.cancel() is True
    cancelled = harness.executor.snapshot

    attempt.completion(TaskAttemptResult.success((_post(1, "cat"),)))

    assert harness.executor.snapshot == cancelled
    assert harness.executor.snapshot.counts.remote_rows == 0


def test_shutdown_is_idempotent_preserves_partial_and_leaves_shared_queue_open():
    harness = _harness(_plan("cat"))
    attempt = harness.factory.attempts[-1]
    harness.executor.shutdown()
    harness.executor.shutdown()

    assert attempt.abortable.abort_count == 1
    assert harness.executor.snapshot.state == QueryExecutionState.PARTIAL_CANCELLED
    assert harness.executor.closed is True
    assert harness.queue.is_shutdown is False
    assert harness.executor.retry_failed_page() is False
    with pytest.raises(RuntimeError):
        harness.executor.start(_plan("dog"))


def test_shutdown_of_owned_queue_closes_queue_but_keeps_finished_snapshot():
    clock = ManualClock()
    scheduler = ManualScheduler(clock)
    factory = ControlledPageFactory()
    executor = DanbooruMultiBranchExecutor(
        "https://example.invalid",
        clock=clock,
        scheduler=scheduler,
        attempt_factory=factory,
    )
    executor.start(_plan("cat"))
    scheduler.run_ready()
    factory.attempts[-1].completion(TaskAttemptResult.success((_post(1, "cat"),)))
    finished = executor.snapshot

    executor.shutdown()
    executor.shutdown()

    assert executor.snapshot == finished
    assert executor._task_queue.is_shutdown is True


def test_public_snapshots_and_limits_are_immutable():
    limits = QueryExecutionLimits(page_size=2)
    harness = _harness(_plan("cat"), limits=limits)
    snapshot = harness.executor.snapshot

    with pytest.raises(FrozenInstanceError):
        limits.page_size = 3
    with pytest.raises(FrozenInstanceError):
        snapshot.state = QueryExecutionState.COMPLETE


def test_invalid_start_arguments_preserve_current_snapshot():
    harness = _harness(_plan("cat"))
    before = harness.executor.snapshot
    bound: list[QueryExecutionBound] = []
    harness.executor.executionBound.connect(bound.append)

    with pytest.raises(TypeError):
        harness.executor.start("not a plan")
    with pytest.raises(TypeError):
        harness.executor.start(_plan("dog"), 0)

    valid = _plan("dog")
    inconsistent = QueryPlan(
        expression=valid.expression,
        dnf_branches=valid.dnf_branches,
        remote_branches=valid.remote_branches,
        rating=valid.rating,
        remote_order=RemoteOrder.RANK,
    )
    with pytest.raises(ValueError):
        harness.executor.start(inconsistent)

    assert harness.executor.snapshot == before
    assert bound == []


def test_synchronous_deadline_does_not_restore_a_stale_timer_handle():
    clock = ManualClock()
    queue_scheduler = ManualScheduler(clock)
    queue = BoundedTaskQueue(
        TaskQueueConfig(minimum_interval_seconds=0),
        clock=clock,
        scheduler=queue_scheduler,
    )
    scheduler = SynchronousDeadlineScheduler(clock)
    executor = DanbooruMultiBranchExecutor(
        "https://example.invalid",
        task_queue=queue,
        clock=clock,
        scheduler=scheduler,
        attempt_factory=ControlledPageFactory(),
    )

    executor.start(_plan("cat"))

    assert executor.snapshot.state == QueryExecutionState.PARTIAL_LIMITED
    assert executor.snapshot.limit_reason == QueryLimitReason.TIME_LIMIT
    assert executor._session.deadline_call is None
    assert scheduler.returned_call is not None
    assert scheduler.returned_call.active is False


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), 0, -1, 60.1, 10**1000],
)
def test_active_time_limit_requires_finite_positive_value_within_maximum(value):
    with pytest.raises(ValueError):
        QueryExecutionLimits(active_time_limit_seconds=value)


def test_binding_and_acceptance_precede_every_page_snapshot():
    harness = _harness(_plan("cat"), auto_start=False)
    events: list[tuple[str, object]] = []
    harness.executor.executionBound.connect(
        lambda event: events.append(("bound", event))
    )
    harness.executor.firstPageAccepted.connect(
        lambda event: events.append(("accepted", event))
    )
    harness.executor.snapshotChanged.connect(
        lambda snapshot: events.append(("snapshot", snapshot.state))
    )

    execution_id = harness.executor.start(_plan("cat"))

    assert events[0] == ("bound", QueryExecutionBound(execution_id))
    assert events[1][0] == "accepted"
    accepted = events[1][1]
    assert isinstance(accepted, QueryExecutionAccepted)
    assert accepted.execution_id == execution_id
    assert accepted.position == QueryBranchPosition(0, 1)
    assert accepted.task_id in harness.queue._entries
    assert events[2] == ("snapshot", QueryExecutionState.WAITING)


@pytest.mark.parametrize("action", ["cancel", "shutdown"])
def test_acceptance_listener_stops_page_without_emitting_snapshot(action):
    harness = _harness(_plan("cat"), auto_start=False)
    accepted: list[QueryExecutionAccepted] = []

    def stop(event: QueryExecutionAccepted) -> None:
        accepted.append(event)
        getattr(harness.executor, action)()

    harness.executor.firstPageAccepted.connect(stop)
    harness.executor.start(_plan("cat"))
    harness.dispatch()

    assert len(accepted) == 1
    assert harness.snapshots == []
    assert harness.factory.attempts == []
    assert harness.queue.has_work is False


def test_binding_listener_replacement_prevents_old_page_submission():
    harness = _harness(_plan("old"), auto_start=False)
    bindings: list[int] = []

    def replace(event: QueryExecutionBound) -> None:
        bindings.append(event.execution_id)
        if len(bindings) == 1:
            harness.executor.start(_plan("new"))

    harness.executor.executionBound.connect(replace)
    harness.executor.start(_plan("old"))
    harness.dispatch()

    assert bindings == [1, 2]
    assert _request_positions(harness) == [("new", 1)]


@pytest.mark.parametrize("action", ["cancel", "shutdown"])
def test_binding_listener_stop_prevents_page_submission(action):
    harness = _harness(_plan("cat"), auto_start=False)
    accepted: list[QueryExecutionAccepted] = []
    harness.executor.firstPageAccepted.connect(accepted.append)
    harness.executor.executionBound.connect(
        lambda event: getattr(harness.executor, action)()
    )

    harness.executor.start(_plan("cat"))
    harness.dispatch()

    assert accepted == []
    assert harness.factory.attempts == []
    assert len(harness.snapshots) == 1
    assert harness.snapshots[0].state == QueryExecutionState.PARTIAL_CANCELLED


def test_acceptance_listener_replacement_emits_no_old_page_snapshot():
    harness = _harness(_plan("old"), auto_start=False)
    accepted: list[QueryExecutionAccepted] = []

    def replace(event: QueryExecutionAccepted) -> None:
        accepted.append(event)
        if event.execution_id == 1:
            harness.executor.start(_plan("new"))

    harness.executor.firstPageAccepted.connect(replace)
    harness.executor.start(_plan("old"))
    harness.dispatch()

    assert [event.execution_id for event in accepted] == [1, 2]
    assert _request_positions(harness) == [("new", 1)]
    assert harness.snapshots
    assert {snapshot.execution_id for snapshot in harness.snapshots} == {2}


def test_queue_rejection_does_not_emit_acceptance_and_retry_emits_once():
    clock = ManualClock()
    scheduler = ManualScheduler(clock)
    queue = BoundedTaskQueue(
        TaskQueueConfig(capacity=1, minimum_interval_seconds=0),
        clock=clock,
        scheduler=scheduler,
    )
    blocker = queue.submit(
        TaskKind.METADATA,
        lambda attempt, completion: FakeAbortable(),
        lambda result: None,
    )
    factory = ControlledPageFactory()
    executor = DanbooruMultiBranchExecutor(
        "https://example.invalid",
        task_queue=queue,
        clock=clock,
        scheduler=scheduler,
        attempt_factory=factory,
    )
    accepted: list[QueryExecutionAccepted] = []
    executor.firstPageAccepted.connect(accepted.append)

    executor.start(_plan("cat"))
    assert accepted == []
    queue.cancel(blocker.identifier)
    assert executor.retry_failed_page() is True
    assert len(accepted) == 1
    scheduler.run_ready()
    factory.attempts[-1].completion(TaskAttemptResult.success(()))

    assert len(accepted) == 1
    assert executor.snapshot.state == QueryExecutionState.COMPLETE


def test_latest_plan_executes_an_empty_tag_request():
    harness = _harness(build_latest_query_plan(), auto_start=False)

    harness.executor.start(build_latest_query_plan())
    harness.dispatch()

    assert _request_positions(harness) == [("", 1)]


def test_request_construction_failure_binds_but_never_accepts_page():
    clock = ManualClock()
    scheduler = ManualScheduler(clock)
    queue = BoundedTaskQueue(
        TaskQueueConfig(minimum_interval_seconds=0),
        clock=clock,
        scheduler=scheduler,
    )
    executor = DanbooruMultiBranchExecutor(
        "not a valid base URL",
        task_queue=queue,
        clock=clock,
        scheduler=scheduler,
        attempt_factory=ControlledPageFactory(),
    )
    bound: list[QueryExecutionBound] = []
    accepted: list[QueryExecutionAccepted] = []
    executor.executionBound.connect(bound.append)
    executor.firstPageAccepted.connect(accepted.append)

    execution_id = executor.start(_plan("cat"))

    assert bound == [QueryExecutionBound(execution_id)]
    assert accepted == []
    assert executor.snapshot.state == QueryExecutionState.FAILED
    assert executor.snapshot.failure.error_code == QueryExecutionErrorCode.REQUEST_START
