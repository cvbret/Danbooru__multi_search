from dataclasses import FrozenInstanceError, replace

import pytest

from danbooru_multi_search.app.query_candidates import (
    CandidateCriteria,
    CandidateSelectionMode,
    QueryCandidateService,
    select_candidates,
)
from danbooru_multi_search.danbooru.query_execution import DanbooruMultiBranchExecutor
from danbooru_multi_search.danbooru.query_types import QueryExecutionLimits, QueryExecutionState
from danbooru_multi_search.danbooru.result_processing import (
    PostFileCategory, ResultSortDirection, ResultSortField, ResultSortSpec,
)
from danbooru_multi_search.tasks import TaskAttemptResult
from test_query_controller import ControlledPageFactory, _harness, _post


@pytest.fixture
def setup():
    harness = _harness(counts={"cat": 10, "dog": 20})
    factory = ControlledPageFactory()
    supplement = DanbooruMultiBranchExecutor(
        "https://example.invalid", task_queue=harness.queue,
        clock=harness.clock, scheduler=harness.scheduler, attempt_factory=factory,
    )
    service = QueryCandidateService(harness.controller, supplement)
    results = []
    service.completed.connect(results.append)
    try:
        yield harness, service, supplement, factory, results
    finally:
        service.shutdown()
        harness.controller.shutdown()
        harness.queue.shutdown()


@pytest.mark.parametrize("values", [
    {"count": 0}, {"count": 101}, {"count": True}, {"start": 0},
    {"minimum_fav_count": -1}, {"ratings": frozenset({"general"})},
])
def test_invalid_criteria_are_rejected_before_requests(values):
    with pytest.raises(ValueError):
        CandidateCriteria(**values)
    assert CandidateCriteria().count == 20
    assert CandidateCriteria(count=100).count == 100


@pytest.mark.parametrize("mode,expected", [
    (CandidateSelectionMode.FIRST, (9, 3)),
    (CandidateSelectionMode.FROM_POSITION, (3, 7)),
    (CandidateSelectionMode.SELECTED, (9, 7)),
])
def test_selection_preserves_first_gallery_order_and_original_positions(mode, expected):
    first = _post(9, "cat")
    posts = (first, _post(3, "cat"), replace(first, score=999), _post(7, "cat"))
    chosen = select_candidates(
        posts, CandidateCriteria(mode=mode, count=2, start=2), selected_keys=("7", "9"),
    )
    assert tuple(post.id for post in chosen) == expected
    if mode is not CandidateSelectionMode.FROM_POSITION:
        assert chosen[0] is first


def test_four_restrictions_exclude_unknown_scores_and_do_not_modify_posts():
    good = replace(_post(4, "cat", score=5), file_ext="gif", rating="s", fav_count=8)
    posts = (
        replace(good, id=1, file_ext="png"), replace(good, id=2, rating="q"),
        replace(good, id=3, score=None), replace(good, id=5, fav_count=None), good,
    )
    criteria = CandidateCriteria(
        file_categories=frozenset({PostFileCategory.GIF}), ratings=frozenset({"s"}),
        minimum_score=3, minimum_fav_count=7,
    )
    assert select_candidates(posts, criteria) == (good,)
    assert posts[2].score is None and posts[3].fav_count is None


def _begin(harness, *, limits=None, sort_spec=None):
    harness.controller.start_standard(
        "cat OR dog", limits=limits or QueryExecutionLimits(page_size=2), sort_spec=sort_spec,
    )
    harness.dispatch()
    harness.succeed((_post(1, "cat", score=1), _post(2, "cat", score=2)), dispatch=False)


def test_capture_is_immutable_and_selection_filters_without_loading(setup):
    harness, service, _supplement, factory, results = setup
    _begin(harness)
    source = service.capture(selected_posts=(_post(2, "cat"), _post(1, "cat")), model_revision=7)
    assert source.selected_keys == ("1", "2") and source.model_revision == 7 and source.partial
    with pytest.raises(FrozenInstanceError):
        source.posts = ()
    assert service.prepare(source, CandidateCriteria(
        mode=CandidateSelectionMode.SELECTED, minimum_score=2,
    ), "selected")
    assert tuple(post.id for post in results[0].posts) == (2,)
    assert results[0].requested_count == 2 and not results[0].error_code
    assert "勾选了 2 张" in results[0].reason and "1 张符合" in results[0].reason
    assert factory.attempts == [] and len(harness.factory.attempts) == 1


def test_active_query_uses_same_execution_and_freezes_sorted_result(setup):
    harness, service, _supplement, factory, results = setup
    sort = ResultSortSpec(ResultSortField.SCORE, ResultSortDirection.DESCENDING)
    _begin(harness, sort_spec=sort)
    source = service.capture()
    assert service.prepare(source, CandidateCriteria(count=3), "active") and not results
    harness.dispatch()
    harness.succeed((_post(3, "dog", score=9), _post(4, "dog", score=4)), dispatch=False)
    assert tuple(post.id for post in results[0].posts) == (3, 4, 2)
    assert factory.attempts == [] and harness.executor.is_execution_active(source.execution_id)
    frozen = results[0].posts
    harness.dispatch()
    harness.succeed((_post(5, "cat", score=20),), dispatch=False)
    assert results[0].posts is frozen and len(results) == 1


def test_stopped_query_resumes_original_branch_and_dedup_without_changing_gallery(setup):
    harness, service, supplement, factory, results = setup
    _begin(harness)
    harness.controller.cancel()
    source = service.capture()
    saved = harness.controller.capture_checkpoint()
    assert service.prepare(source, CandidateCriteria(count=3), "resume")
    harness.dispatch()
    assert [(attempt.tags, attempt.page) for attempt in factory.attempts] == [("dog", 1)]
    factory.attempts[-1].completion(TaskAttemptResult.success((_post(2, "cat", "dog"), _post(3, "dog"))))
    harness.dispatch()
    assert tuple(post.id for post in results[0].posts) == (1, 2, 3)
    assert results[0].scanned_candidates == 3 and results[0].remote_rows == 4
    assert supplement.snapshot.state is QueryExecutionState.PARTIAL_CANCELLED
    assert harness.controller.capture_checkpoint() == saved
    assert tuple(post.id for post in harness.model.posts) == (1, 2)
    assert len(factory.attempts) == 1


@pytest.mark.parametrize("limit", ["candidates", "time"])
def test_supplement_never_resets_spent_query_limits(setup, limit):
    harness, service, _supplement, factory, results = setup
    limits = QueryExecutionLimits(page_size=2, candidate_limit=2 if limit == "candidates" else 1000)
    _begin(harness, limits=limits)
    if limit == "time":
        harness.clock.value = 59.8
        harness.controller.cancel()
    source = service.capture()
    service.prepare(source, CandidateCriteria(count=4), "limits")
    if limit == "time":
        harness.dispatch()
        assert len(factory.attempts) == 1
        harness.clock.value += 0.3
        harness.dispatch()
        assert factory.attempts[0].abortable.abort_count == 1
    else:
        assert factory.attempts == []
    assert tuple(post.id for post in results[0].posts) == (1, 2)
    assert ("候选扫描上限" if limit == "candidates" else "活动时间上限") in results[0].reason
    assert not results[0].error_code


@pytest.mark.parametrize("change", ["new_query", "view", "close"])
def test_source_change_or_close_cancels_only_owned_supplement_and_ignores_late_page(setup, change):
    harness, service, supplement, factory, results = setup
    _begin(harness)
    harness.controller.cancel()
    source = service.capture()
    service.prepare(source, CandidateCriteria(count=4), "old")
    harness.dispatch()
    attempt = factory.attempts[-1]
    if change == "new_query":
        harness.controller.start_latest()
    elif change == "view":
        harness.controller.update_view(sort_spec=ResultSortSpec(ResultSortField.SCORE, ResultSortDirection.DESCENDING))
    else:
        service.shutdown()
    assert attempt.abortable.abort_count == 1 and not harness.queue.is_shutdown
    attempt.completion(TaskAttemptResult.success((_post(3, "dog"),)))
    harness.dispatch()
    assert not service.source_current(source)
    assert not service.prepare(source, CandidateCriteria(count=4), "stale")
    assert len(results) == (0 if change == "close" else 1)
    if results:
        assert results[0].error_code == "source_changed" and results[0].posts == ()
    assert supplement.closed is (change == "close")


def test_cancel_preparation_does_not_cancel_active_query(setup):
    harness, service, _supplement, factory, results = setup
    _begin(harness)
    source = service.capture()
    service.prepare(source, CandidateCriteria(count=4), "cancel")
    assert service.cancel("cancel")
    assert harness.executor.is_execution_active(source.execution_id)
    assert not harness.queue.is_shutdown and factory.attempts == []
    assert results[0].error_code == "cancelled" and results[0].posts == ()


def test_reentrant_cancel_completion_cannot_replace_new_preparation(setup):
    harness, service, _supplement, _factory, results = setup
    _begin(harness)
    source = service.capture()
    service.prepare(source, CandidateCriteria(count=4), "first")

    def new_request(result):
        if result.token == "first":
            assert service.prepare(source, CandidateCriteria(count=4), "new")

    service.completed.connect(new_request)
    assert not service.prepare(source, CandidateCriteria(count=4), "superseded")
    assert service.cancel("new")
    assert [result.token for result in results] == ["first", "new"]


def test_partial_without_more_and_position_beyond_result_report_shortage(setup):
    harness, service, _supplement, factory, results = setup
    _begin(harness)
    source = service.capture()
    service.prepare(source, CandidateCriteria(
        mode=CandidateSelectionMode.FROM_POSITION, start=7, count=2, allow_more=False,
    ), "no-more")
    assert results[0].posts == () and results[0].requested_count == 2
    assert "已加载" in results[0].reason and not results[0].error_code
    assert factory.attempts == []


@pytest.mark.parametrize("allow_more", [True, False])
def test_query_finishes_between_capture_and_confirmation_uses_correct_snapshot(setup, allow_more):
    harness, service, _supplement, factory, results = setup
    _begin(harness)
    source = service.capture()
    harness.dispatch()
    harness.succeed((_post(3, "dog"),))
    harness.succeed(())
    assert harness.controller.last_snapshot.state is QueryExecutionState.COMPLETE
    assert service.prepare(source, CandidateCriteria(count=3, allow_more=allow_more), "captured")
    result = results[0]
    assert tuple(post.id for post in result.posts) == ((1, 2, 3) if allow_more else (1, 2))
    assert result.partial is not allow_more
    assert result.scanned_candidates == (3 if allow_more else 2)
    assert result.remote_rows == (3 if allow_more else 2)
    assert factory.attempts == []
