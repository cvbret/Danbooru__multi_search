from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from typing import Callable
from urllib.parse import parse_qs, urlsplit

import pytest

from PySide6.QtCore import QObject

from danbooru_multi_search.danbooru import (
    DanbooruM2QueryController,
    DanbooruMultiBranchExecutor,
    DanbooruPost,
    DanbooruPostListModel,
    GalleryQueryAccepted,
    GalleryQueryDefinition,
    GalleryQueryOrigin,
    GalleryResultEvent,
    GalleryViewDefinitionChanged,
    QueryExecutionLimits,
    QueryExecutionState,
    QueryMode,
    QueryParseError,
    QueryRating,
    ResultFilterSpec,
    ResultProcessingError,
    ResultSortDirection,
    ResultSortField,
    ResultSortSpec,
    TagIndexSnapshot,
    TagSuggestion,
    TagTranslationStore,
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
        self.wall_time = datetime(2026, 1, 2, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.value

    def utcnow(self) -> datetime:
        return self.wall_time


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


class ImmediateZeroScheduler(ManualScheduler):
    def call_later(
        self,
        delay_seconds: float,
        callback: Callable[[], None],
    ) -> ManualCall:
        call = ManualCall(self.clock.monotonic() + delay_seconds, callback)
        self.calls.append(call)
        if delay_seconds <= 0:
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
    completion: Callable[[TaskAttemptResult], None]
    abortable: FakeAbortable


class ControlledPageFactory:
    def __init__(self) -> None:
        self.attempts: list[PageAttempt] = []
        self.synchronous_results: list[TaskAttemptResult] = []

    def __call__(self, request, attempt_number, completion):
        del attempt_number
        query = parse_qs(
            urlsplit(request.url().toString()).query,
            keep_blank_values=True,
        )
        abortable = FakeAbortable()
        attempt = PageAttempt(
            tags=query["tags"][0],
            page=int(query["page"][0]),
            completion=completion,
            abortable=abortable,
        )
        self.attempts.append(attempt)
        if self.synchronous_results:
            completion(self.synchronous_results.pop(0))
        return abortable


@dataclass(slots=True)
class ControllerHarness:
    controller: DanbooruM2QueryController
    executor: DanbooruMultiBranchExecutor
    queue: BoundedTaskQueue
    clock: ManualClock
    scheduler: ManualScheduler
    factory: ControlledPageFactory
    model: DanbooruPostListModel
    store: TagTranslationStore

    def dispatch(self) -> None:
        self.scheduler.run_ready()

    def succeed(
        self,
        posts: tuple[DanbooruPost, ...],
        *,
        dispatch: bool = True,
    ) -> None:
        self.factory.attempts[-1].completion(TaskAttemptResult.success(posts))
        if dispatch:
            self.dispatch()

    def fail(self, message: str = "failed", *, status: int = 500) -> None:
        self.factory.attempts[-1].completion(
            TaskAttemptResult.failure(
                message,
                error_code="http",
                http_status=status,
            )
        )


def _post(post_id: int, *tags: str, score: int = 0) -> DanbooruPost:
    return DanbooruPost(
        id=post_id,
        preview_url=f"https://example.invalid/{post_id}.jpg",
        large_file_url=None,
        file_url=f"https://example.invalid/{post_id}-full.jpg",
        score=score,
        fav_count=post_id,
        rating="g",
        width=100,
        height=100,
        file_ext="jpg",
        created_at="2026-01-01T00:00:00.000000Z",
        general_tags=tuple(tags),
    )


def _tag_snapshot(**counts: int | None) -> TagIndexSnapshot:
    return TagIndexSnapshot.build(
        tuple(
            TagSuggestion(name=name, post_count=count)
            for name, count in counts.items()
        )
    )


def _harness(
    *,
    counts: dict[str, int | None] | None = None,
    scheduler_type=ManualScheduler,
    synchronous_results: list[TaskAttemptResult] | None = None,
    queue_config: TaskQueueConfig | None = None,
) -> ControllerHarness:
    clock = ManualClock()
    scheduler = scheduler_type(clock)
    queue = BoundedTaskQueue(
        queue_config
        or TaskQueueConfig(
            capacity=64,
            metadata_concurrency=1,
            media_concurrency=1,
            minimum_interval_seconds=0,
            max_retries=0,
        ),
        clock=clock,
        scheduler=scheduler,
    )
    factory = ControlledPageFactory()
    if synchronous_results:
        factory.synchronous_results.extend(synchronous_results)
    executor = DanbooruMultiBranchExecutor(
        "https://example.invalid",
        task_queue=queue,
        clock=clock,
        scheduler=scheduler,
        attempt_factory=factory,
    )
    model = DanbooruPostListModel()
    store = TagTranslationStore(_tag_snapshot(**(counts or {})))
    controller = DanbooruM2QueryController(
        "https://example.invalid",
        executor=executor,
        tag_store=store,
        posts_model=model,
        clock=lambda: clock.wall_time,
    )
    return ControllerHarness(
        controller,
        executor,
        queue,
        clock,
        scheduler,
        factory,
        model,
        store,
    )


@pytest.mark.parametrize("supplied", [False, True])
def test_ui_controller_keeps_public_identity_model_type_and_parent_ownership(supplied):
    from danbooru_multi_search.danbooru import query_controller as coordinator_module
    from danbooru_multi_search.ui.query_controller import (
        DanbooruM2QueryController as UiController,
    )

    parent = QObject()
    model_parent = QObject()
    model = DanbooruPostListModel(model_parent) if supplied else None
    controller = UiController("https://example.invalid", posts_model=model, parent=parent)
    try:
        assert UiController is DanbooruM2QueryController
        assert coordinator_module.DanbooruM2QueryController is UiController
        assert coordinator_module.__dict__["DanbooruM2QueryController"] is UiController
        assert "DanbooruM2QueryController" in dir(coordinator_module)
        with pytest.raises(AttributeError, match="unknown_controller"):
            getattr(coordinator_module, "unknown_controller")
        assert isinstance(controller, coordinator_module.M2QueryCoordinator)
        assert type(controller.posts_model) is DanbooruPostListModel
        assert controller.parent() is parent
        if supplied:
            assert controller.posts_model is model
            assert model.parent() is model_parent
        else:
            assert controller.posts_model.parent() is controller
        assert controller.executor.parent() is controller
        assert controller.operation_id is None and controller.last_snapshot is None
    finally:
        controller.shutdown()


def test_query_definition_is_immutable_and_enforces_mode_shapes():
    definition = GalleryQueryDefinition(
        QueryMode.STANDARD,
        "cat AND dog",
        QueryRating.ALL,
        ResultFilterSpec(),
        ResultSortSpec(),
        QueryExecutionLimits(),
    )
    with pytest.raises(FrozenInstanceError):
        definition.mode = QueryMode.LATEST
    with pytest.raises(ValueError):
        GalleryQueryDefinition(
            QueryMode.STANDARD,
            None,
            QueryRating.ALL,
            ResultFilterSpec(),
            ResultSortSpec(),
            QueryExecutionLimits(),
        )
    with pytest.raises(ValueError):
        GalleryQueryDefinition(
            QueryMode.LATEST,
            "cat",
            QueryRating.ALL,
            ResultFilterSpec(),
            ResultSortSpec(),
            QueryExecutionLimits(),
        )


def test_definition_normalizes_identity_inputs_and_rejects_rank_outside_rank_mode():
    with pytest.raises(ValueError):
        GalleryQueryDefinition(
            QueryMode.STANDARD,
            "CAT",
            QueryRating.ALL,
            ResultFilterSpec(),
            ResultSortSpec(),
            QueryExecutionLimits(),
        )
    normalized = GalleryQueryDefinition(
        QueryMode.STANDARD,
        "cat",
        QueryRating.ALL,
        ResultFilterSpec(blocked_tags=frozenset({"DOG"})),
        ResultSortSpec(),
        QueryExecutionLimits(),
    )
    assert normalized.filter_spec.blocked_tags == frozenset({"dog"})
    with pytest.raises(ResultProcessingError) as caught:
        GalleryQueryDefinition(
            QueryMode.LATEST,
            None,
            QueryRating.ALL,
            ResultFilterSpec(),
            ResultSortSpec(ResultSortField.RANK),
            QueryExecutionLimits(),
        )
    assert caught.value.code == "rank_unavailable"


def test_three_modes_build_expected_first_requests_and_acceptance_metadata():
    standard = _harness(counts={"cat": 5, "dog": 0, "bird": None})
    accepted: list[GalleryQueryAccepted] = []
    standard.controller.queryAccepted.connect(accepted.append)
    standard.controller.start_standard(
        "DOG and CAT and BIRD",
        rating="general",
    )
    standard.dispatch()

    assert standard.factory.attempts[0].tags == "dog cat rating:g"
    assert accepted[0].definition.mode == QueryMode.STANDARD
    assert accepted[0].definition.normalized_expression == "dog AND cat AND bird"
    assert accepted[0].query_tags == ("bird", "cat", "dog")
    assert accepted[0].count_tag_usage is True

    rank = _harness()
    rank.controller.start_rank()
    rank.dispatch()
    assert rank.factory.attempts[0].tags == "order:rank"
    assert rank.controller.definition.mode == QueryMode.RANK

    latest = _harness()
    latest_accepts: list[GalleryQueryAccepted] = []
    latest.controller.queryAccepted.connect(latest_accepts.append)
    latest.controller.start_latest()
    latest.dispatch()
    assert latest.factory.attempts[0].tags == ""
    assert latest.controller.definition.mode == QueryMode.LATEST
    assert latest_accepts[0].query_tags == ()
    assert latest_accepts[0].count_tag_usage is False


def test_standard_empty_expression_reports_parser_error_before_display_text():
    harness = _harness()

    with pytest.raises(QueryParseError) as caught:
        harness.controller.start_standard("")

    assert caught.value.code == "empty_expression"
    assert harness.executor.current_execution_id is None


def test_invalid_new_configuration_preserves_current_execution_and_gallery():
    harness = _harness()
    harness.controller.start_standard("cat")
    harness.dispatch()
    current_execution = harness.controller.execution_id
    request_count = len(harness.factory.attempts)

    with pytest.raises(ResultProcessingError) as caught:
        harness.controller.start_latest(
            sort_spec=ResultSortSpec(ResultSortField.RANK)
        )

    assert caught.value.code == "rank_unavailable"
    assert harness.controller.execution_id == current_execution
    assert len(harness.factory.attempts) == request_count


@pytest.mark.parametrize(
    "keyword",
    [
        {"filter_spec": 0},
        {"sort_spec": 0},
    ],
)
def test_falsey_invalid_configuration_is_not_replaced_by_defaults(keyword):
    harness = _harness()

    with pytest.raises((TypeError, ResultProcessingError)):
        harness.controller.start_standard("cat", **keyword)

    assert harness.executor.current_execution_id is None


def test_controller_normalizes_filter_identity_before_starting_network_work():
    harness = _harness()

    harness.controller.start_standard(
        "cat",
        filter_spec=ResultFilterSpec(
            blocked_tags=frozenset({"DOG"}),
            minimum_aspect_ratio=Fraction(2, 4),
        ),
    )

    logical_filter = harness.controller.definition.filter_spec
    assert logical_filter.blocked_tags == frozenset({"dog"})
    assert logical_filter.minimum_aspect_ratio == Fraction(1, 2)
    assert logical_filter.age_reference_time is None


def test_tag_count_snapshot_is_fixed_per_execution_and_refresh_reads_replacement():
    harness = _harness(counts={"cat": 0, "dog": None, "bird": None})
    harness.controller.start_standard("cat AND dog AND bird")
    harness.store.replace_snapshot(_tag_snapshot(cat=100, dog=1, bird=200))
    harness.dispatch()
    assert harness.factory.attempts[0].tags == "cat bird"

    harness.controller.refresh()
    harness.dispatch()
    assert harness.factory.attempts[1].tags == "dog cat"


def test_query_acceptance_precedes_first_result_summary():
    harness = _harness()
    events: list[tuple[str, object]] = []
    harness.controller.queryAccepted.connect(
        lambda event: events.append(("accepted", event))
    )
    harness.controller.resultSummaryChanged.connect(
        lambda event: events.append(("summary", event))
    )

    harness.controller.start_standard("cat")

    assert events[0][0] == "accepted"
    assert events[1][0] == "summary"
    assert events[1][1].summary.state == QueryExecutionState.WAITING


def test_state_changes_emit_summaries_without_resetting_an_uncommitted_model():
    harness = _harness()
    summaries: list[GalleryResultEvent] = []
    harness.controller.resultSummaryChanged.connect(summaries.append)
    initial_revision = harness.model.revision

    harness.controller.start_standard("cat")
    harness.dispatch()

    assert [event.summary.state for event in summaries] == [
        QueryExecutionState.WAITING,
        QueryExecutionState.RUNNING,
    ]
    assert harness.model.revision == initial_revision
    assert harness.model.display_operation_id is None


def test_reemitted_old_snapshot_object_cannot_replace_newer_controller_state():
    harness = _harness()
    summaries: list[GalleryResultEvent] = []
    harness.controller.resultSummaryChanged.connect(summaries.append)
    harness.controller.start_standard("cat")
    old_snapshot = harness.controller.last_snapshot
    harness.dispatch()
    current_snapshot = harness.controller.last_snapshot
    summary_count = len(summaries)

    harness.controller._snapshot_changed(old_snapshot)

    assert harness.controller.last_snapshot is current_snapshot
    assert len(summaries) == summary_count


def test_synchronous_completion_before_start_returns_is_processed_once():
    harness = _harness(
        scheduler_type=ImmediateZeroScheduler,
        synchronous_results=[TaskAttemptResult.success(())],
    )
    events: list[str] = []
    summaries: list[GalleryResultEvent] = []
    harness.controller.queryAccepted.connect(lambda event: events.append("accepted"))
    harness.controller.resultSummaryChanged.connect(
        lambda event: (events.append("summary"), summaries.append(event))
    )

    operation_id = harness.controller.start_latest()

    assert operation_id == 1
    assert events == ["accepted", "summary"]
    assert len(summaries) == 1
    assert summaries[0].summary.state == QueryExecutionState.COMPLETE
    assert harness.model.display_operation_id == operation_id
    assert harness.model.count == 0


def test_external_earlier_acceptance_cancel_does_not_create_controller_acceptance():
    harness = _harness()
    harness.executor.firstPageAccepted.disconnect(harness.controller._first_page_accepted)
    harness.executor.firstPageAccepted.connect(lambda event: harness.executor.cancel())
    harness.executor.firstPageAccepted.connect(harness.controller._first_page_accepted)
    accepted: list[GalleryQueryAccepted] = []
    summaries: list[GalleryResultEvent] = []
    harness.controller.queryAccepted.connect(accepted.append)
    harness.controller.resultSummaryChanged.connect(summaries.append)

    harness.controller.start_standard("cat")
    harness.dispatch()

    assert accepted == []
    assert summaries == []
    assert harness.factory.attempts == []


def test_earlier_binding_listener_new_query_cannot_bind_old_plan_to_new_execution():
    harness = _harness()
    harness.executor.executionBound.disconnect(harness.controller._execution_bound)
    replaced = False

    def replace_before_controller(event) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            harness.controller.start_latest()

    harness.executor.executionBound.connect(replace_before_controller)
    harness.executor.executionBound.connect(harness.controller._execution_bound)
    accepted: list[GalleryQueryAccepted] = []
    harness.controller.queryAccepted.connect(accepted.append)

    old_operation = harness.controller.start_standard("cat")
    harness.dispatch()

    assert old_operation == 1
    assert harness.controller.operation_id == 2
    assert harness.controller.definition.mode == QueryMode.LATEST
    assert [event.operation_id for event in accepted] == [2]
    assert [attempt.tags for attempt in harness.factory.attempts] == [""]


def test_earlier_binding_listener_shutdown_finishes_old_start_without_error():
    harness = _harness()
    harness.executor.executionBound.disconnect(harness.controller._execution_bound)
    harness.executor.executionBound.connect(
        lambda event: harness.controller.shutdown()
    )
    harness.executor.executionBound.connect(harness.controller._execution_bound)
    accepted: list[GalleryQueryAccepted] = []
    harness.controller.queryAccepted.connect(accepted.append)

    operation_id = harness.controller.start_standard("cat")
    harness.dispatch()

    assert operation_id == 1
    assert harness.controller.closed is True
    assert harness.controller.operation_id is None
    assert accepted == []
    assert harness.factory.attempts == []
    assert harness.queue.is_shutdown is False


def test_view_change_reprocesses_full_snapshot_without_network_and_orders_events():
    harness = _harness()
    harness.controller.start_standard("cat")
    harness.dispatch()
    harness.succeed((_post(1, "cat", score=1), _post(2, "cat", score=5)))
    request_count = len(harness.factory.attempts)
    events: list[tuple[str, int]] = []
    harness.controller.viewDefinitionChanged.connect(
        lambda event: events.append(("view", event.view_generation))
    )
    harness.controller.resultSummaryChanged.connect(
        lambda event: events.append(("summary", event.view_generation))
    )

    assert harness.controller.update_view(
        filter_spec=ResultFilterSpec(minimum_score=2),
        sort_spec=ResultSortSpec(
            ResultSortField.SCORE,
            ResultSortDirection.DESCENDING,
        ),
    )

    assert len(harness.factory.attempts) == request_count
    assert [post.id for post in harness.model.posts] == [2]
    assert events == [("view", 1), ("summary", 1)]


def test_view_listener_reentrant_update_suppresses_old_view_summary():
    harness = _harness()
    harness.controller.start_standard("cat")
    harness.dispatch()
    harness.succeed((_post(1, "cat", score=1),))
    view_events: list[GalleryViewDefinitionChanged] = []
    summary_generations: list[int] = []

    def update_again(event: GalleryViewDefinitionChanged) -> None:
        view_events.append(event)
        if event.view_generation == 1:
            harness.controller.update_view(
                filter_spec=ResultFilterSpec(minimum_score=2)
            )

    harness.controller.viewDefinitionChanged.connect(update_again)
    harness.controller.resultSummaryChanged.connect(
        lambda event: summary_generations.append(event.view_generation)
    )

    harness.controller.update_view(filter_spec=ResultFilterSpec(minimum_score=0))

    assert [event.view_generation for event in view_events] == [1, 2]
    assert summary_generations == [2]


@pytest.mark.parametrize("action", ["new_query", "shutdown"])
def test_view_listener_invalidation_suppresses_old_source_summary(action):
    harness = _harness()
    old_operation = harness.controller.start_standard("cat")
    harness.dispatch()
    harness.succeed((_post(1, "cat", score=1),))
    summaries: list[GalleryResultEvent] = []

    def invalidate(event: GalleryViewDefinitionChanged) -> None:
        if action == "new_query":
            harness.controller.start_latest()
        else:
            harness.controller.shutdown()

    harness.controller.viewDefinitionChanged.connect(invalidate)
    harness.controller.resultSummaryChanged.connect(summaries.append)
    harness.controller.update_view(filter_spec=ResultFilterSpec(minimum_score=1))

    stale = [
        event
        for event in summaries
        if event.operation_id == old_operation
        and event.view_generation == 1
        and event.summary.state != QueryExecutionState.PARTIAL_CANCELLED
    ]
    assert stale == []


def test_new_execution_keeps_old_gallery_until_source_arrives_or_completes_empty():
    harness = _harness()
    first_operation = harness.controller.start_standard("cat")
    harness.dispatch()
    first_post = _post(1, "cat")
    harness.succeed((first_post,))
    assert harness.model.display_operation_id == first_operation

    failed_operation = harness.controller.start_standard("dog")
    assert harness.model.display_operation_id == first_operation
    assert harness.model.post_at(0) is first_post
    harness.dispatch()
    harness.fail()
    assert harness.controller.operation_id == failed_operation
    assert harness.model.display_operation_id == first_operation

    empty_filtered = harness.controller.start_standard(
        "bird",
        filter_spec=ResultFilterSpec(minimum_score=10),
    )
    harness.dispatch()
    harness.succeed((_post(2, "bird", score=1),))
    assert harness.model.display_operation_id == empty_filtered
    assert harness.model.count == 0

    empty_complete = harness.controller.start_latest()
    harness.dispatch()
    harness.succeed(())
    assert harness.model.display_operation_id == empty_complete
    assert harness.model.count == 0


def test_cancel_retains_partial_results_and_emits_cancelled_summary():
    harness = _harness()
    summaries: list[GalleryResultEvent] = []
    harness.controller.resultSummaryChanged.connect(summaries.append)
    operation_id = harness.controller.start_standard(
        "cat",
        limits=QueryExecutionLimits(page_size=1),
    )
    harness.dispatch()
    harness.succeed((_post(1, "cat"),), dispatch=False)

    assert harness.controller.cancel() is True

    assert harness.model.display_operation_id == operation_id
    assert [post.id for post in harness.model.posts] == [1]
    assert summaries[-1].summary.state == QueryExecutionState.PARTIAL_CANCELLED


@pytest.mark.parametrize("action", ["new_query", "cancel", "shutdown"])
def test_model_reset_listener_invalidation_suppresses_old_page_summary(action):
    harness = _harness()
    summaries: list[GalleryResultEvent] = []
    harness.controller.resultSummaryChanged.connect(summaries.append)
    old_operation = harness.controller.start_standard(
        "cat",
        limits=QueryExecutionLimits(page_size=1),
    )
    harness.dispatch()
    summaries.clear()
    acted = False

    def invalidate() -> None:
        nonlocal acted
        if acted:
            return
        acted = True
        if action == "new_query":
            harness.controller.start_latest()
        elif action == "cancel":
            harness.controller.cancel()
        else:
            harness.controller.shutdown()

    harness.model.modelReset.connect(invalidate)
    harness.succeed((_post(1, "cat"),))

    stale = [
        event
        for event in summaries
        if event.operation_id == old_operation
        and event.summary.state
        in {QueryExecutionState.RUNNING, QueryExecutionState.COMPLETE}
    ]
    assert acted is True
    assert stale == []


def test_failed_page_retry_keeps_operation_and_does_not_accept_again():
    harness = _harness()
    accepted: list[GalleryQueryAccepted] = []
    harness.controller.queryAccepted.connect(accepted.append)
    operation_id = harness.controller.start_standard("cat")
    harness.dispatch()
    harness.fail()

    assert harness.controller.retry_failed_page() is True
    harness.dispatch()
    harness.succeed(())

    assert harness.controller.operation_id == operation_id
    assert harness.controller.execution_id == accepted[0].execution_id
    assert len(accepted) == 1
    assert harness.executor.snapshot.state == QueryExecutionState.COMPLETE


def test_first_queue_rejection_records_actual_later_acceptance_time():
    harness = _harness(
        queue_config=TaskQueueConfig(
            capacity=1,
            metadata_concurrency=1,
            media_concurrency=1,
            minimum_interval_seconds=0,
            max_retries=0,
        )
    )
    blocker = harness.queue.submit(
        TaskKind.METADATA,
        lambda attempt, completion: FakeAbortable(),
        lambda result: None,
    )
    accepted: list[GalleryQueryAccepted] = []
    harness.controller.queryAccepted.connect(accepted.append)
    harness.controller.start_standard("cat")
    assert accepted == []

    harness.clock.wall_time += timedelta(minutes=5)
    harness.queue.cancel(blocker.identifier)
    assert harness.controller.retry_failed_page() is True

    assert len(accepted) == 1
    assert accepted[0].accepted_at == datetime(
        2026,
        1,
        2,
        0,
        5,
        tzinfo=timezone.utc,
    )


def test_query_accepted_listener_new_query_isolates_old_operation():
    harness = _harness()
    accepted: list[GalleryQueryAccepted] = []
    summaries: list[GalleryResultEvent] = []

    def replace(event: GalleryQueryAccepted) -> None:
        accepted.append(event)
        if event.definition.mode == QueryMode.STANDARD:
            harness.controller.start_latest()

    harness.controller.queryAccepted.connect(replace)
    harness.controller.resultSummaryChanged.connect(summaries.append)

    old_operation = harness.controller.start_standard("cat")
    harness.dispatch()

    assert old_operation == 1
    assert [event.definition.mode for event in accepted] == [
        QueryMode.STANDARD,
        QueryMode.LATEST,
    ]
    assert all(event.operation_id != old_operation for event in summaries)
    assert [attempt.tags for attempt in harness.factory.attempts] == [""]


def test_refresh_and_history_reexecution_get_new_operations_and_usage_rules():
    harness = _harness()
    accepted: list[GalleryQueryAccepted] = []
    harness.controller.queryAccepted.connect(accepted.append)
    first = harness.controller.start_standard("cat")
    definition = harness.controller.definition
    second = harness.controller.refresh()
    third = harness.controller.reexecute_history(definition, display_text="历史 cat")

    assert (first, second, third) == (1, 2, 3)
    assert accepted[0].origin == GalleryQueryOrigin.STANDARD
    assert accepted[0].count_tag_usage is True
    assert accepted[1].origin == GalleryQueryOrigin.REFRESH
    assert accepted[1].count_tag_usage is False
    assert accepted[2].origin == GalleryQueryOrigin.HISTORY
    assert accepted[2].count_tag_usage is True


def test_query_tags_include_unique_positive_and_negative_occurrences():
    harness = _harness()
    accepted: list[GalleryQueryAccepted] = []
    harness.controller.queryAccepted.connect(accepted.append)

    harness.controller.start_standard("cat AND NOT dog AND cat")

    assert accepted[0].query_tags == ("cat", "dog")


def test_rank_results_keep_session_positions_after_local_resort():
    harness = _harness()
    harness.controller.start_rank(
        sort_spec=ResultSortSpec(ResultSortField.RANK)
    )
    harness.dispatch()
    harness.succeed(
        (
            _post(1, score=1),
            _post(2, score=10),
        )
    )

    assert [item.session_rank for item in harness.model.processed_posts] == [1, 2]
    request_count = len(harness.factory.attempts)
    harness.controller.update_view(
        sort_spec=ResultSortSpec(
            ResultSortField.SCORE,
            ResultSortDirection.DESCENDING,
        )
    )

    assert len(harness.factory.attempts) == request_count
    assert [item.post.id for item in harness.model.processed_posts] == [2, 1]
    assert [item.session_rank for item in harness.model.processed_posts] == [2, 1]


def test_shutdown_allows_one_final_cancel_summary_then_rejects_entry_points():
    harness = _harness()
    summaries: list[GalleryResultEvent] = []
    harness.controller.resultSummaryChanged.connect(summaries.append)
    harness.controller.start_standard("cat")
    initial_count = len(summaries)

    harness.controller.shutdown()
    harness.controller.shutdown()

    assert len(summaries) == initial_count + 1
    assert summaries[-1].summary.state == QueryExecutionState.PARTIAL_CANCELLED
    assert harness.controller.closed is True
    assert harness.queue.is_shutdown is False
    assert harness.controller.retry_failed_page() is False
    with pytest.raises(RuntimeError):
        harness.controller.start_latest()
    with pytest.raises(RuntimeError):
        harness.controller.update_view(filter_spec=ResultFilterSpec())


def test_age_filter_definition_omits_reference_and_runtime_keeps_start_time():
    harness = _harness()
    harness.controller.start_standard(
        "cat",
        filter_spec=ResultFilterSpec(maximum_age=timedelta(days=2)),
    )

    assert harness.controller.definition.filter_spec.age_reference_time is None
    assert harness.controller._current_operation.runtime_filter.age_reference_time == (
        datetime(2026, 1, 2, tzinfo=timezone.utc)
    )


def test_logical_definition_accepts_normalized_fraction_without_reference_time():
    definition = GalleryQueryDefinition(
        QueryMode.RANK,
        None,
        QueryRating.ALL,
        ResultFilterSpec(
            maximum_age=timedelta(days=1),
            minimum_aspect_ratio=Fraction(1, 2),
        ),
        ResultSortSpec(ResultSortField.RANK),
        QueryExecutionLimits(),
    )

    assert definition.filter_spec.age_reference_time is None
