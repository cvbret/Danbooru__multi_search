"""Ordinary gallery requests advance only with explicit user demand."""

from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

from danbooru_multi_search.danbooru.query_types import (
    QueryExecutionLimits, QueryExecutionState, QueryLimitReason,
)
from danbooru_multi_search.storage.checkpoint_serialization import decode_checkpoint, encode_checkpoint
from test_query_execution import _latest_json_query, _search_row


@pytest.fixture
def gallery():
    resources = []

    def create(pages, **kwargs):
        value = _latest_json_query(pages, page_size=50, branch_page_limit=20, **kwargs)
        resources.append(value)
        return value

    yield create
    for core, bridge, _executor, queue, *_ in resources:
        core.shutdown()
        bridge.detach()
        queue.shutdown()


def _pages(manager):
    return [parse_qs(urlsplit(request.url().toString()).query)["page"][0] for request in manager.requests]


def test_first_50_waits_without_requests_or_active_time_and_next_load_is_single(gallery):
    first = [_search_row(i) for i in range(100, 50, -1)]
    core, bridge, executor, _queue, scheduler, manager, replies = gallery([first, [_search_row(50)]])
    replies[0].finished.emit()
    assert bridge.loadedCount == 50 and bridge.canLoadMore and not bridge.isLoadingMore
    assert bridge.queryState == "partial_paused"
    assert core.last_processed.result_state.value == "partial"
    checkpoint = core.capture_checkpoint()
    assert not checkpoint.execution.active
    scheduler.advance(300)
    assert len(manager.requests) == 1
    assert core.capture_checkpoint().execution.elapsed_active_seconds == checkpoint.execution.elapsed_active_seconds
    assert bridge.loadMore() and not bridge.loadMore()
    scheduler.run_ready()
    assert _pages(manager) == ["1", "2"]
    assert all(parse_qs(urlsplit(request.url().toString()).query)["limit"] == ["50"] for request in manager.requests)
    replies[1].finished.emit()
    assert [post.id for post in core.posts_model.posts] == list(range(100, 49, -1))
    assert executor.snapshot.state is QueryExecutionState.COMPLETE and not bridge.canLoadMore


def test_failed_second_page_retries_only_same_page_and_keeps_50(gallery):
    core, bridge, executor, _queue, scheduler, manager, replies = gallery([
        [_search_row(i + 1) for i in range(50)], {}, [_search_row(80)],
    ])
    replies[0].finished.emit()
    operation = core.operation_id
    assert bridge.loadMore()
    scheduler.run_ready()
    replies[1].finished.emit()
    assert bridge.loadedCount == 50 and bridge.canRetry and not bridge.canLoadMore
    assert bridge.retryFailedPage()
    scheduler.run_ready()
    replies[2].finished.emit()
    assert _pages(manager) == ["1", "2", "2"]
    assert bridge.loadedCount == 51 and core.operation_id == operation
    assert executor.snapshot.state is QueryExecutionState.COMPLETE


@pytest.mark.parametrize("unavailable", [False, True])
def test_empty_visible_gallery_can_continue_and_counts_original_rows(gallery, unavailable):
    first = [{"id": i + 1} for i in range(50)] if unavailable else [_search_row(i + 1) for i in range(50)]
    core, bridge, executor, _queue, scheduler, manager, replies = gallery(
        [first, []], form_values={} if unavailable else {"minimumScore": "10"},
    )
    replies[0].finished.emit()
    assert bridge.visibleCount == 0 and bridge.remoteRows == 50 and bridge.canLoadMore
    assert bridge.loadMore()
    scheduler.run_ready()
    replies[1].finished.emit()
    assert _pages(manager) == ["1", "2"]
    assert executor.snapshot.state is QueryExecutionState.COMPLETE


def test_requested_n_stops_at_candidate_limit_without_additional_page(gallery):
    core, bridge, executor, _queue, scheduler, manager, replies = gallery([
        [_search_row(i + 1) for i in range(50)],
        [_search_row(i + 51) for i in range(50)],
    ], form_values={"candidateLimit": "75"})
    replies[0].finished.emit()
    assert bridge.loadMore()
    scheduler.run_ready()
    replies[1].finished.emit()
    assert bridge.loadedCount == 75 and bridge.remoteRows == 100
    assert executor.snapshot.limit_reason is QueryLimitReason.CANDIDATE_LIMIT
    assert not bridge.canLoadMore and not bridge.loadMore() and len(manager.requests) == 2


def test_default_gallery_limits_allow_1000_and_explicit_smaller_page_limit_stays(gallery):
    core, bridge, executor, _queue, scheduler, manager, replies = gallery(
        [[_search_row(i + 1 + 50 * page) for i in range(50)] for page in range(20)],
    )
    assert bridge.draftFormValues["branchPageLimit"] == "20"
    for page in range(20):
        replies[page].finished.emit()
        if page < 19:
            assert bridge.loadMore()
            scheduler.run_ready()
    assert len(manager.requests) == 20 and bridge.loadedCount == 1000
    assert executor.snapshot.limit_reason is QueryLimitReason.CANDIDATE_LIMIT
    assert not bridge.canLoadMore
    assert QueryExecutionLimits(branch_page_limit=10).branch_page_limit == 10


def test_paused_checkpoint_round_trip_resumes_one_page_with_original_position(gallery):
    core, bridge, executor, _queue, scheduler, manager, replies = gallery([
        [_search_row(i + 1) for i in range(50)], [_search_row(60)],
    ])
    replies[0].finished.emit()
    checkpoint = decode_checkpoint(encode_checkpoint(core.capture_checkpoint()))
    scheduler.advance(120)
    assert bridge.resumeCheckpoint(checkpoint)
    scheduler.run_ready()
    assert _pages(manager) == ["1", "2"]
    replies[1].finished.emit()
    assert bridge.loadedCount == 51 and executor.snapshot.state is QueryExecutionState.COMPLETE


def test_legacy_large_checkpoint_is_kept_and_reexecution_displays_and_requests_50(gallery):
    core, bridge, _executor, _queue, scheduler, manager, replies = gallery([
        [_search_row(i + 1) for i in range(50)], [],
    ])
    replies[0].finished.emit()
    checkpoint = core.capture_checkpoint()
    legacy_limits = replace(checkpoint.execution.limits, page_size=200, branch_page_limit=10)
    legacy = replace(checkpoint, definition=replace(checkpoint.definition, limits=legacy_limits),
                     execution=replace(checkpoint.execution, limits=legacy_limits,
                                       counts=replace(checkpoint.execution.counts, remote_rows=200)))
    posts = core.posts_model.posts
    assert not bridge.resumeCheckpoint(legacy)
    assert bridge.errorCode and "重新执行" in bridge.errorText
    assert core.posts_model.posts == posts and len(manager.requests) == 1
    assert bridge.draftFormValues["pageSize"] == "50"
    assert bridge.draftFormValues["branchPageLimit"] == "10"
    assert bridge.reexecute_loaded_history("旧查询")
    scheduler.run_ready()
    assert parse_qs(urlsplit(manager.requests[-1].url().toString()).query)["limit"] == ["50"]
    assert core.definition.limits.page_size == 50 and core.definition.limits.branch_page_limit == 10


def test_result_notification_can_close_before_old_load_continues(gallery):
    core, bridge, executor, _queue, scheduler, manager, replies = gallery([
        [_search_row(i + 1) for i in range(50)], [],
    ])
    replies[0].finished.emit()
    bridge.availabilityChanged.connect(lambda: core.shutdown() if bridge.isLoadingMore else None)
    bridge.loadMore()
    scheduler.run_ready()
    assert len(manager.requests) == 1
    assert not executor.can_load_more


def test_load_notification_starting_new_query_never_submits_old_next_page(gallery):
    core, bridge, _executor, _queue, scheduler, manager, replies = gallery([
        [_search_row(i + 1) for i in range(50)], [],
    ])
    replies[0].finished.emit()
    original = core.operation_id

    def replace_query():
        if bridge.isLoadingMore:
            bridge.availabilityChanged.disconnect(replace_query)
            core.start_latest()

    bridge.availabilityChanged.connect(replace_query)
    bridge.loadMore()
    scheduler.run_ready()
    assert core.operation_id != original and _pages(manager) == ["1", "1"]
    replies[1].finished.emit()
    assert bridge.queryState == "complete"


def test_paused_page_and_history_are_durable_before_and_after_close(tmp_path):
    from test_checkpoint_controller import build, close
    from test_query_controller import _post

    values = build(tmp_path)
    harness, _database, history, _shared, bridge, repository, _checkpoints, _session = values
    form = bridge.draftFormValues
    form["pageSize"] = "2"
    assert bridge.updateDraftForm(form) and bridge.executeLatest()
    harness.dispatch()
    harness.succeed((_post(2, "cat"), _post(1, "cat")))
    saved = repository.get_for_history(history.latest_query().id)
    assert saved.checkpoint.execution.state is QueryExecutionState.PARTIAL_PAUSED
    assert not saved.checkpoint.execution.active
    close(values)
    saved = repository.get_for_history(history.latest_query().id)
    assert saved.checkpoint.execution.state is QueryExecutionState.PARTIAL_PAUSED
    assert saved.checkpoint.execution.counts.remote_rows == 2
    assert not saved.checkpoint.execution.resume_reason and len(harness.factory.attempts) == 1


def test_collection_continues_with_own_bounded_executor_after_gallery_pauses():
    from danbooru_multi_search.app.query_candidates import CandidateCriteria, QueryCandidateService
    from danbooru_multi_search.danbooru.query_execution import DanbooruMultiBranchExecutor
    from danbooru_multi_search.tasks import TaskAttemptResult
    from test_query_controller import ControlledPageFactory, _harness, _post

    harness = _harness(counts={"cat": 10, "dog": 20})
    harness.executor.set_manual_paging(True)
    factory = ControlledPageFactory()
    supplement = DanbooruMultiBranchExecutor(
        "https://example.invalid", task_queue=harness.queue,
        clock=harness.clock, scheduler=harness.scheduler, attempt_factory=factory,
    )
    service = QueryCandidateService(harness.controller, supplement)
    results = []
    service.completed.connect(results.append)
    try:
        harness.controller.start_standard("cat OR dog", limits=QueryExecutionLimits(page_size=2))
        harness.dispatch()
        harness.succeed((_post(1, "cat"), _post(2, "cat")))
        source = service.capture()
        assert harness.controller.load_more()
        harness.dispatch()
        assert service.prepare(source, CandidateCriteria(count=5), "collect")
        harness.succeed((_post(3, "dog"), _post(4, "dog")))
        assert not results and len(factory.attempts) == 1
        assert (factory.attempts[0].tags, factory.attempts[0].page) == ("cat", 2)
        factory.attempts[0].completion(TaskAttemptResult.success((_post(5, "cat"),)))
        harness.dispatch()
        assert [post.id for post in results[0].posts] == [1, 2, 3, 4, 5]
        assert len(harness.factory.attempts) == 2 and [post.id for post in harness.model.posts] == [1, 2, 3, 4]
        assert harness.executor.snapshot.state is QueryExecutionState.PARTIAL_PAUSED
        assert not supplement.manual_paging
    finally:
        service.shutdown()
        harness.controller.shutdown()
        harness.queue.shutdown()
