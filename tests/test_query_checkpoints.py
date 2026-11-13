from __future__ import annotations

import sys

if sys.argv[-1:] == ["--pure-core-check"]:
    import builtins
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"PySide6", "json", "sqlite3"} or name.startswith(("danbooru_multi_search.storage", "danbooru_multi_search.ui")):
            raise AssertionError(name)
        return original(name, *args, **kwargs)
    builtins.__import__ = guarded
    from danbooru_multi_search.danbooru.query_checkpoint import ExecutionCheckpoint, source_identity
    from danbooru_multi_search.danbooru.query_planning import build_latest_query_plan
    from danbooru_multi_search.danbooru.query_types import QueryExecutionLimits, QueryExecutionState, QueryExecutionCounts, QueryBranchProgress
    value = ExecutionCheckpoint(build_latest_query_plan(), QueryExecutionLimits(), QueryExecutionState.PARTIAL_CANCELLED,
        QueryExecutionCounts(), (), (), (QueryBranchProgress(0, 1, 0, False, False),), (0,), None, None, None, 0.25, False)
    assert value.resume_reason == "" and source_identity("https://user:secret@EXAMPLE.invalid:443/api/?token=secret") == "https://example.invalid/api"
    sys.exit(0)

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from danbooru_multi_search.danbooru.query_checkpoint import CheckpointError, QueryCheckpoint, source_identity
from danbooru_multi_search.danbooru.query_types import QueryExecutionLimits, QueryExecutionCounts, QueryExecutionState
from danbooru_multi_search.danbooru.query_definition import GalleryQueryDefinition, QueryMode
from danbooru_multi_search.danbooru.result_processing import ResultFilterSpec, ResultSortSpec
from tests.test_query_execution import _harness, _plan, _post


def checkpoint():
    harness = _harness(_plan("a AND b"), limits=QueryExecutionLimits(page_size=2, branch_page_limit=3))
    harness.succeed((_post(1, "a", "b"), _post(2, "a")))
    harness.clock.advance_only(1.125)
    harness.executor.cancel()
    execution = harness.executor.export_checkpoint()
    definition = GalleryQueryDefinition(QueryMode.STANDARD, "a AND b", execution.plan.rating, ResultFilterSpec(), ResultSortSpec(), execution.limits)
    return QueryCheckpoint("https://example.invalid", definition, definition.filter_spec, execution)


def test_core_checkpoint_constructs_without_qt_json_sqlite_or_storage():
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--pure-core-check"], env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_all_candidates_and_first_seen_order_are_distinct_from_matches():
    value = checkpoint()
    assert tuple(post.id for post in value.execution.candidates) == (1, 2)
    assert value.execution.matched_ids == (1,)
    assert value.execution.elapsed_active_seconds == 1.125
    assert value.execution.uncommitted.page == 2 and not value.execution.resume_reason


@pytest.mark.parametrize("changes", [
    {"state": QueryExecutionState.COMPLETE}, {"state": QueryExecutionState.RUNNING},
    {"counts": QueryExecutionCounts(1, 2, 1)}, {"matched_ids": (2,)}, {"pending": (0,)},
    {"elapsed_active_seconds": float("nan")},
])
def test_inconsistent_state_counts_and_rotation_are_rejected(changes):
    with pytest.raises(CheckpointError):
        replace(checkpoint().execution, **changes)


def test_mode_rating_and_remote_plan_must_match_definition():
    value = checkpoint()
    from danbooru_multi_search.danbooru.query_planning import QueryRating
    with pytest.raises(CheckpointError):
        replace(value, definition=replace(value.definition, rating=QueryRating.EXPLICIT))
    with pytest.raises(CheckpointError):
        replace(value, definition=replace(value.definition, mode=QueryMode.RANK))


def test_one_page_limited_branch_does_not_block_other_original_branches():
    harness = _harness(_plan("a OR b"), limits=QueryExecutionLimits(page_size=1, branch_page_limit=2))
    harness.succeed((_post(1, "a"),))
    harness.succeed((_post(2, "b"),))
    harness.succeed((_post(3, "a"),))
    harness.executor.cancel()
    value = harness.executor.export_checkpoint()
    assert value.branches[0].page_limited and value.branches[1].successful_pages == 1
    assert not value.resume_reason and value.uncommitted.branch_index == 1


def test_source_identity_removes_credentials_and_preserves_base_path():
    assert source_identity("HTTPS://user:secret@EXAMPLE.invalid:443/api/?key=secret#secret") == "https://example.invalid/api"
    assert source_identity("http://example.invalid:8080/api") != source_identity("https://example.invalid/api")
