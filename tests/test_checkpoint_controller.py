from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from danbooru_multi_search.danbooru.query_types import QueryExecutionLimits, QueryExecutionState
from danbooru_multi_search.danbooru.result_processing import ResultFilterSpec
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.favorites import FavoriteRepository
from danbooru_multi_search.storage.history import HistoryRepository
from danbooru_multi_search.storage.query_checkpoints import QueryCheckpointRepository
from danbooru_multi_search.tasks import TaskAttemptResult
from danbooru_multi_search.app.query_checkpoints import QueryCheckpointService
from danbooru_multi_search.ui.query_checkpoints import QueryCheckpointController
from danbooru_multi_search.ui.query_state import M2QueryBridgeController
from danbooru_multi_search.ui.session_state import BrowserSessionController
from danbooru_multi_search.ui.window_state import ScreenGeometry, WindowGeometry
from tests.test_favorite_controller import APPLICATION, controller_for
from tests.test_query_controller import _harness, _post, ImmediateZeroScheduler


def build(tmp_path, *, scheduler_type=None):
    harness = _harness(**({"scheduler_type": scheduler_type} if scheduler_type else {}))
    database = Database(tmp_path / "application.sqlite3")
    database.initialize()
    history = HistoryRepository(database, clock=lambda: harness.clock.wall_time, busy_timeout_ms=0)
    shared = controller_for(FavoriteRepository(database))
    bridge = M2QueryBridgeController(harness.controller)
    repository = QueryCheckpointRepository(database)
    service = QueryCheckpointService(repository, history, harness.controller, shared._writes.access)
    checkpoints = QueryCheckpointController(repository, service, bridge, shared._writes.access)
    harness.executor.before_attempt = service.persist_execution
    harness.executor.before_abandon = service.persist_execution
    harness.executor.before_publish = service.persist_execution
    session = BrowserSessionController(history, (ScreenGeometry(WindowGeometry(0, 0, 1280, 820), primary=True),),
        m2_controller=harness.controller, query_bridge=bridge, checkpoint_service=service)
    checkpoints.attach_session(session)
    return harness, database, history, shared, bridge, repository, checkpoints, session


def close(values):
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    checkpoints.begin_shutdown()
    checkpoints._service.begin_shutdown()
    shared.shutdown()
    checkpoints._service.finish_shutdown()
    harness.controller.shutdown()
    session.begin_shutdown()
    session.detach_events()
    bridge.detach()


@pytest.mark.parametrize("immediate", [False, True])
def test_initial_activity_and_history_are_durable_before_real_starter(tmp_path, immediate):
    values = build(tmp_path, scheduler_type=ImmediateZeroScheduler if immediate else None)
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    original = harness.factory
    observations = []
    def start(request, attempt, completion):
        query = history.latest_query()
        saved = repository.get_for_history(query.id)
        observations.append(saved)
        assert saved.checkpoint.execution.active and saved.checkpoint.execution.uncommitted.page == 1
        return original(request, attempt, completion)
    harness.executor._attempt_factory = start
    try:
        harness.controller.start_standard("a")
        harness.dispatch()
        assert len(observations) == len(harness.factory.attempts) == 1
    finally:
        close(values)
    record = repository.get_for_history(history.latest_query().id)
    assert not record.checkpoint.execution.active and not record.checkpoint.execution.resume_reason


def test_initial_storage_failure_rolls_back_history_and_starts_no_network(tmp_path):
    values = build(tmp_path, scheduler_type=ImmediateZeroScheduler)
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    with database.connect() as connection:
        connection.execute("CREATE TRIGGER reject_checkpoint BEFORE INSERT ON query_checkpoints BEGIN SELECT RAISE(ABORT, 'failure'); END")
    harness.controller.start_standard("a")
    harness.dispatch()
    assert harness.factory.attempts == [] and history.latest_query() is None
    assert "保存失败" in checkpoints.messageText
    assert harness.executor.snapshot.state is QueryExecutionState.FAILED
    close(values)


def test_restart_resumes_original_rotation_all_candidates_and_does_not_replan(tmp_path, monkeypatch):
    values = build(tmp_path)
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    limits = QueryExecutionLimits(page_size=2, branch_page_limit=3)
    harness.controller.start_standard("(a AND b) OR c", limits=limits)
    harness.dispatch()
    harness.succeed((_post(1, "a", "b"), _post(2, "a")))
    # c/p1 is active: preserve it ahead of a/p2, without losing candidate 2.
    harness.clock.value += 1.25
    harness.controller.cancel()
    record = repository.get_for_history(history.latest_query().id)
    assert record.checkpoint.execution.uncommitted.branch_index == 1
    assert tuple(post.id for post in record.checkpoint.execution.candidates) == (1, 2)
    close(values)
    resumed = build(tmp_path)
    second, _database, second_history, _shared, second_bridge, second_repository, coordinator, browser = resumed
    try:
        assert second.factory.attempts == []
        def forbidden(*args, **kwargs):
            raise AssertionError("resume must not build a plan from current tag counts")
        monkeypatch.setattr(second.controller, "_build_plan", forbidden)
        browser.selectHistory(0)
        coordinator.selectHistory(str(record.history_id))
        assert coordinator.canResume
        assert coordinator.resumeSelected(coordinator.selectionToken)
        second.dispatch()
        assert [(item.tags, item.page) for item in second.factory.attempts] == [("c", 1)]
        second.succeed((_post(2, "c"), _post(3, "c")))
        second.succeed((_post(4, "a", "b"),))
        second.succeed(())
        assert [post.id for post in second.controller.posts_model.posts] == [1, 3, 4]
        assert [(item.tags, item.page) for item in second.factory.attempts] == [("c", 1), ("a b", 2), ("c", 2)]
        assert second.executor.snapshot.counts.remote_rows == 5
        assert second.executor.snapshot.counts.unique_candidates == 4
        assert second.executor.export_checkpoint().elapsed_active_seconds == 1.25
    finally:
        close(resumed)


def test_local_age_view_revision_restart_preserves_precise_reference_and_new_owner_view_zero(tmp_path):
    values = build(tmp_path)
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    harness.controller.start_standard("a", limits=QueryExecutionLimits(page_size=2))
    harness.dispatch()
    harness.succeed((_post(1, "a"), _post(2, "a")))
    assert harness.controller.update_view(filter_spec=ResultFilterSpec(minimum_age=timedelta(microseconds=1234567)))
    reference = harness.controller.capture_checkpoint().runtime_filter.age_reference_time
    harness.controller.cancel()
    record = repository.get_for_history(history.latest_query().id)
    assert record.view_revision == 1 and record.checkpoint.runtime_filter.age_reference_time == reference
    close(values)
    resumed = build(tmp_path)
    second, _db, _history, _shared, _bridge, saved, coordinator, browser = resumed
    try:
        browser.selectHistory(0)
        coordinator.selectHistory(str(record.history_id))
        assert coordinator.resumeSelected(coordinator.selectionToken), coordinator.messageText
        second.dispatch()
        current = saved.get_for_history(record.history_id)
        assert current.view_revision == 0 and current.owner != record.owner
        assert second.controller.capture_checkpoint().runtime_filter.age_reference_time == reference
        assert second.controller.definition == record.checkpoint.definition
    finally:
        close(resumed)


def test_import_occupation_during_page_commit_stops_next_request_immediately(tmp_path):
    values = build(tmp_path)
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    harness.controller.start_standard("a", limits=QueryExecutionLimits(page_size=1))
    harness.dispatch()
    assert shared._writes.access.begin_external_write("import")
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        harness.succeed((_post(1, "a"),))
        assert len(harness.factory.attempts) == 1
        assert harness.executor.snapshot.state is QueryExecutionState.PARTIAL_FAILED
        assert "收藏正在导入" in checkpoints.messageText
    shared._writes.access.finish_external_write("import")
    close(values)


@pytest.mark.parametrize("phase", ["release", "draft"])
@pytest.mark.parametrize("action", ["close", "edit", "new_query"])
def test_resume_rechecks_sync_release_and_draft_load_before_network(tmp_path, phase, action):
    values = build(tmp_path)
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    harness.controller.start_standard("a")
    harness.dispatch()
    harness.controller.cancel()
    session.selectHistory(0)
    checkpoints.selectHistory(str(history.latest_query().id))
    token = checkpoints.selectionToken
    before = len(harness.factory.attempts)
    triggered = []
    def change():
        if triggered:
            return
        triggered.append(True)
        if action == "close":
            bridge.beginClosing()
        elif action == "edit":
            bridge.applyAdvancedText("newer")
        else:
            harness.controller.start_standard("newer")
    if phase == "release":
        shared.busyChanged.connect(lambda: change() if not shared.busy else None)
    else:
        bridge.draftChanged.connect(change)
    assert not checkpoints.resumeSelected(token)
    harness.dispatch()
    if action == "new_query":
        assert harness.controller.definition.normalized_expression == "newer"
        assert all(attempt.tags == "newer" for attempt in harness.factory.attempts[before:])
    else:
        assert len(harness.factory.attempts) == before
    if action == "edit":
        assert bridge.advancedText == "newer"
    close(values)


def test_restart_active_unsettled_record_is_read_only_and_cannot_resume(tmp_path):
    values = build(tmp_path)
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    harness.controller.start_standard("a")
    harness.dispatch()
    record = repository.get_for_history(history.latest_query().id)
    assert record.checkpoint.execution.active
    # Open another application graph without settling the first graph's activity.
    second_values = build(tmp_path)
    second, _db, _history, _shared, _bridge, _saved, coordinator, browser = second_values
    try:
        browser.selectHistory(0)
        coordinator.selectHistory(str(record.history_id))
        assert not coordinator.canResume and "无法确认剩余活动时间" in coordinator.messageText
        assert not coordinator.resumeSelected(coordinator.selectionToken)
        assert second.factory.attempts == []
    finally:
        close(second_values)
        close(values)


def test_failed_page_and_accumulated_time_resume_without_refetching_committed_page(tmp_path):
    values = build(tmp_path)
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    harness.controller.start_standard("a", limits=QueryExecutionLimits(page_size=1))
    harness.dispatch()
    harness.succeed((_post(1, "a"),))
    harness.clock.value += 4.5
    harness.fail("failure")
    record = repository.get_for_history(history.latest_query().id)
    assert record.checkpoint.execution.failure.position.page == 2
    assert record.checkpoint.execution.elapsed_active_seconds == 4.5
    session.selectHistory(0)
    checkpoints.selectHistory(str(record.history_id))
    assert checkpoints.resumeSelected(checkpoints.selectionToken)
    harness.dispatch()
    assert harness.factory.attempts[-1].page == 2
    assert harness.executor.export_checkpoint().elapsed_active_seconds == 4.5
    close(values)


@pytest.mark.parametrize("recovery", ["close", "view"])
def test_final_empty_page_persistence_failure_is_visible_and_never_reports_complete(tmp_path, recovery):
    values = build(tmp_path)
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    harness.controller.start_standard("a", limits=QueryExecutionLimits(page_size=1))
    harness.dispatch()
    harness.succeed((_post(1, "a"),))
    before = repository.get_for_history(history.latest_query().id)
    with database.connect() as connection:
        connection.execute("CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON query_checkpoints BEGIN SELECT RAISE(ABORT, 'final checkpoint failure'); END")
    harness.succeed(())
    assert harness.executor.snapshot.state is QueryExecutionState.PARTIAL_FAILED
    assert "final checkpoint failure" in bridge.errorText and "检查点保存失败" in bridge.errorText
    assert "final checkpoint failure" in bridge.failureErrorText
    assert repository.get_for_history(before.history_id) == before
    assert history.get_query(before.history_id).result_summary.state is not QueryExecutionState.COMPLETE
    with database.connect() as connection:
        connection.execute("DROP TRIGGER reject_checkpoint")
    if recovery == "view":
        assert harness.controller.update_view(filter_spec=ResultFilterSpec(minimum_score=0))
    close(values)
    stored = repository.get_for_history(history.latest_query().id)
    assert not stored.checkpoint.execution.active
    assert stored.checkpoint.execution.uncommitted is None and stored.checkpoint.execution.pending == ()
    assert stored.checkpoint.execution.persistence_error is not None
    assert "异常结束" not in stored.checkpoint.execution.resume_reason
    from danbooru_multi_search.danbooru.query_types import QueryExecutionErrorCode
    assert history.latest_query().result_summary.failure_error_code is QueryExecutionErrorCode.PERSISTENCE


def test_failure_notification_cannot_stop_or_skip_persistence_for_a_new_query(tmp_path, monkeypatch):
    from danbooru_multi_search.danbooru.query_checkpoint import CheckpointError
    values = build(tmp_path)
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    original = repository.save_in_transaction
    failures = []
    def fail_once(*args, **kwargs):
        if not failures:
            failures.append(True)
            raise CheckpointError("storage_error", "once")
        return original(*args, **kwargs)
    monkeypatch.setattr(repository, "save_in_transaction", fail_once)
    replaced = []
    def replace_query(snapshot):
        if snapshot.state is QueryExecutionState.FAILED and not replaced:
            replaced.append(True)
            harness.controller.start_standard("newer")
    harness.executor.snapshotChanged.connect(replace_query)
    harness.controller.start_standard("a")
    harness.dispatch()
    assert len(harness.factory.attempts) == 1 and harness.factory.attempts[0].tags == "newer"
    current = repository.get_for_history(history.latest_query().id)
    assert current.checkpoint.definition.normalized_expression == "newer" and current.checkpoint.execution.active
    assert harness.executor.snapshot.state in {QueryExecutionState.RUNNING, QueryExecutionState.WAITING}
    close(values)


def test_aborted_claim_revokes_only_target_history_and_keeps_other_execution_final_save(tmp_path):
    values = build(tmp_path)
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    harness.controller.start_standard("a")
    harness.dispatch()
    harness.controller.cancel()
    target = repository.get_for_history(history.latest_query().id)
    harness.controller.start_standard("b")
    harness.dispatch()
    other = repository.get_for_history(history.latest_query().id)
    row = next(index for index, query in enumerate(session._history_model.items) if query.id == target.history_id)
    session.selectHistory(row)
    checkpoints.selectHistory(str(target.history_id))
    changed = []
    def edit_draft():
        if not changed:
            changed.append(True)
            bridge.applyAdvancedText("newer")
    bridge.draftChanged.connect(edit_draft)
    assert not checkpoints.resumeSelected(checkpoints.selectionToken)
    assert harness.controller.definition.normalized_expression == "b"
    claimed = repository.get_for_history(target.history_id)
    assert claimed.owner != target.owner and not claimed.checkpoint.execution.active
    assert all(lease.history_id != target.history_id for lease in checkpoints._service._leases.values())
    assert any(lease.history_id == other.history_id for lease in checkpoints._service._leases.values())
    close(values)
    final = repository.get_for_history(other.history_id)
    assert final.owner == other.owner and not final.checkpoint.execution.active
    assert repository.get_for_history(target.history_id) == claimed
