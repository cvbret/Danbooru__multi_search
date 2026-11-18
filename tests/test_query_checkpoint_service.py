from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from danbooru_multi_search.app.query_checkpoints import (
    QueryCheckpointService,
    build_checkpoint_summary,
)
from danbooru_multi_search.app.write_access import SharedWriteAccess
from danbooru_multi_search.danbooru.query_checkpoint import CheckpointError
from danbooru_multi_search.danbooru.query_events import GalleryQueryAccepted, GalleryQueryOrigin
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.history import HistoryRepository
from danbooru_multi_search.storage.query_checkpoints import QueryCheckpointRepository
from tests.test_query_checkpoints import checkpoint


class Target:
    def __init__(self, value):
        self.value = value
        self.operation_id = self.execution_id = 1
        self.view_generation = 0
        self.checkpoint_source = value.source
        self.failures = []
        self.executor = SimpleNamespace(
            current_execution_id=1,
            checkpoint_failed=self.failures.append,
        )

    def capture_checkpoint(self, execution=None):
        from dataclasses import replace
        return self.value if execution is None else replace(self.value, execution=execution)

    def cancel(self):
        return False


@pytest.fixture
def progress(tmp_path):
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    history = HistoryRepository(database, busy_timeout_ms=0)
    repository = QueryCheckpointRepository(database)
    target = Target(checkpoint())
    access = SharedWriteAccess()
    service = QueryCheckpointService(repository, history, target, access)
    return SimpleNamespace(
        service=service, repository=repository, history=history, target=target, access=access,
    )


def accepted(value, *, origin=GalleryQueryOrigin.STANDARD):
    target = value.target
    return GalleryQueryAccepted(
        target.operation_id, target.execution_id, target.value.definition,
        "a AND b", origin, ("a", "b"), datetime.now(timezone.utc), True,
    )


def test_claim_remains_current_through_first_accepted_and_keeps_public_facts(progress):
    value = progress
    query = value.service.record_accepted(accepted(value))
    old = value.repository.get_for_history(query.id)
    claim = value.service.begin_resume(old, guard=lambda: True)
    assert claim is value.service.resuming
    assert value.service.current_ownership is None and value.access.busy
    with pytest.raises(FrozenInstanceError):
        claim.write_owner = "replacement"
    value.service.release_resume_write(claim)
    assert not value.access.busy and value.service.resuming is claim
    value.target.operation_id = value.target.execution_id = 2
    value.target.executor.current_execution_id = 2
    resumed = value.service.record_accepted(accepted(value, origin=GalleryQueryOrigin.CHECKPOINT))
    assert resumed.id == query.id
    assert value.service.current_ownership.owner == claim.record.owner
    assert value.service.resuming is claim
    value.service.finish_resume(claim)
    assert value.service.resuming is None
    assert value.history.get_query(query.id).result_summary == build_checkpoint_summary(value.target.value)


def test_retirement_revokes_deferred_writes_before_synchronous_notification(progress):
    value = progress
    query = value.service.record_accepted(accepted(value))
    owned = value.service.current_ownership
    value.service.begin_shutdown()
    assert value.service._deferred is not None
    value.repository.clear_checkpoints(expected=value.repository.capture_cleanup())
    seen = []

    def retired(owners):
        assert value.service.current_ownership is None
        assert value.service._deferred is None
        seen.append(owners)

    value.service.ownership_retired = retired
    owners = frozenset({(owned.history_id, owned.owner)})
    value.service.retire_checkpoints(owners)
    value.service.finish_shutdown()
    assert seen == [owners] and value.service.closed
    assert value.repository.get_for_history(query.id) is None
    assert value.history.get_query(query.id) is not None


def test_old_failure_does_not_report_or_stop_the_reentrant_new_identity(progress):
    value = progress
    reported = []

    def fail(message):
        value.target.failures.append(message)
        value.target.operation_id = value.target.execution_id = 2
        value.target.executor.current_execution_id = 2

    value.target.executor.checkpoint_failed = fail
    value.service.failure_reported = lambda identity, error: reported.append((identity, error))
    value.service.persistence_failed(CheckpointError("storage_busy", "old failure"))
    assert len(value.target.failures) == 1 and "old failure" in value.target.failures[0]
    assert reported == [] and value.service.identity == (2, 2, 0)
    assert value.service._failing_identities == set()


def test_finished_service_rejects_a_late_network_persistence_barrier(progress):
    from dataclasses import replace
    from danbooru_multi_search.danbooru.query_types import QueryExecutionState

    value = progress
    query = value.service.record_accepted(accepted(value))
    value.service.begin_shutdown()
    value.service.finish_shutdown()
    confirmed = value.repository.get_for_history(query.id)
    late_execution = replace(
        value.target.value.execution, active=True, state=QueryExecutionState.RUNNING,
    )
    assert not value.service.persist_execution(late_execution)
    assert value.repository.get_for_history(query.id) == confirmed


def test_service_import_and_real_persistence_do_not_load_ui_or_query_controller(tmp_path):
    code = """
import importlib.abc
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

class Reject(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("danbooru_multi_search.ui", "danbooru_multi_search.danbooru.query_controller")):
            raise AssertionError(fullname)

sys.meta_path.insert(0, Reject())
from danbooru_multi_search.app.query_checkpoints import QueryCheckpointService
from danbooru_multi_search.app.write_access import SharedWriteAccess
from danbooru_multi_search.danbooru.query_checkpoint import ExecutionCheckpoint, QueryCheckpoint
from danbooru_multi_search.danbooru.query_definition import GalleryQueryDefinition, QueryMode
from danbooru_multi_search.danbooru.query_events import GalleryQueryAccepted, GalleryQueryOrigin
from danbooru_multi_search.danbooru.query_planning import build_latest_query_plan
from danbooru_multi_search.danbooru.query_types import QueryExecutionLimits, QueryExecutionState, QueryExecutionCounts, QueryBranchProgress
from danbooru_multi_search.danbooru.result_processing import ResultFilterSpec, ResultSortSpec
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.history import HistoryRepository
from danbooru_multi_search.storage.query_checkpoints import QueryCheckpointRepository

limits = QueryExecutionLimits()
plan = build_latest_query_plan()
execution = ExecutionCheckpoint(plan, limits, QueryExecutionState.PARTIAL_CANCELLED,
    QueryExecutionCounts(), (), (), (QueryBranchProgress(0, 1, 0, False, False),),
    (0,), None, None, None, 0.25, False)
definition = GalleryQueryDefinition(QueryMode.LATEST, None, plan.rating, ResultFilterSpec(), ResultSortSpec(), limits)
checkpoint = QueryCheckpoint("https://example.invalid", definition, definition.filter_spec, execution)
database = Database(Path(sys.argv[1]) / "cold.sqlite3")
database.initialize()
history = HistoryRepository(database, busy_timeout_ms=0)
repository = QueryCheckpointRepository(database)
target = SimpleNamespace(operation_id=1, execution_id=1, view_generation=0,
    checkpoint_source=checkpoint.source, cancel=lambda: False,
    capture_checkpoint=lambda current=None: checkpoint,
    executor=SimpleNamespace(current_execution_id=1, checkpoint_failed=lambda message: None))
service = QueryCheckpointService(repository, history, target, SharedWriteAccess())
event = GalleryQueryAccepted(1, 1, definition, "最新作品", GalleryQueryOrigin.LATEST, (), datetime.now(timezone.utc), False)
query = service.record_accepted(event)
assert repository.get_for_history(query.id).checkpoint == checkpoint
assert service.persist_execution(execution)
service.begin_shutdown()
service.finish_shutdown()
assert service.closed and repository.get_for_history(query.id).checkpoint == checkpoint
assert not any(name.startswith(("danbooru_multi_search.ui", "danbooru_multi_search.danbooru.query_controller")) for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
