from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from danbooru_multi_search.danbooru.query_checkpoint import CheckpointError
from danbooru_multi_search.storage.checkpoint_serialization import encode_checkpoint, decode_checkpoint
from tests.test_query_checkpoints import checkpoint


def test_full_checkpoint_roundtrip_preserves_plan_all_candidates_and_exact_age_reference():
    value = checkpoint()
    reference = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)
    logical = replace(value.definition.filter_spec, minimum_age=timedelta(microseconds=1234567))
    value = replace(value, definition=replace(value.definition, filter_spec=logical), runtime_filter=replace(logical, age_reference_time=reference))
    encoded = encode_checkpoint(value)
    assert decode_checkpoint(encoded) == value
    assert encode_checkpoint(decode_checkpoint(encoded)) == encoded
    assert '"candidates"' in encoded and '"matched_ids":[1]' in encoded


@pytest.mark.parametrize("path,changed", [
    (("version",), 99), (("execution", "counts", "remote_rows"), 1.5),
    (("execution", "active"), True), (("execution", "state"), "complete"),
    (("execution", "pending"), [0]), (("plan", "rating"), "explicit"),
    (("definition", "mode"), "rank"),
    (("execution", "candidates", 0, "created_at"), "broken"),
    (("execution", "candidates", 0, "width"), 0),
])
def test_malformed_future_or_inconsistent_json_is_explicitly_rejected(path, changed):
    raw = json.loads(encode_checkpoint(checkpoint()))
    target = raw
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = changed
    encoded = json.dumps(raw, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    with pytest.raises(CheckpointError):
        decode_checkpoint(encoded)


def test_optional_media_and_large_integers_keep_existing_post_rules():
    value = checkpoint()
    post = replace(value.execution.candidates[0], id=2**60 + 1, preview_url=None, large_file_url=None,
        created_at="9999-12-31T23:59:59.999999Z")
    execution = replace(value.execution, candidates=(post, value.execution.candidates[1]), matched_ids=(post.id,))
    value = replace(value, execution=execution)
    assert decode_checkpoint(encode_checkpoint(value)) == value
    for changes in ({"preview_url": None, "large_file_url": None, "file_url": None}, {"height": 0}):
        with pytest.raises(CheckpointError):
            replace(execution, candidates=(replace(post, **changes), execution.candidates[1]))


@pytest.mark.parametrize("mode", ["rank", "latest"])
def test_other_modes_roundtrip_actual_saved_plan_without_replanning(mode):
    from danbooru_multi_search.danbooru.query_definition import GalleryQueryDefinition, QueryMode
    from danbooru_multi_search.danbooru.query_checkpoint import QueryCheckpoint
    from danbooru_multi_search.danbooru.query_planning import build_rank_query_plan, build_latest_query_plan
    from danbooru_multi_search.danbooru.result_processing import ResultFilterSpec, ResultSortSpec
    from tests.test_query_execution import _harness
    plan = build_rank_query_plan(None) if mode == "rank" else build_latest_query_plan()
    harness = _harness(plan)
    harness.executor.cancel()
    execution = harness.executor.export_checkpoint()
    definition = GalleryQueryDefinition(QueryMode(mode), None, plan.rating, ResultFilterSpec(), ResultSortSpec(), execution.limits)
    value = QueryCheckpoint("https://example.invalid", definition, definition.filter_spec, execution)
    assert decode_checkpoint(encode_checkpoint(value)) == value


@pytest.mark.parametrize("expression,candidates", [(None, ["a"]), ("a AND b", ["alien"])])
def test_rank_candidate_rules_are_checked_before_claim_or_execution(expression, candidates):
    from danbooru_multi_search.danbooru.query_definition import GalleryQueryDefinition, QueryMode
    from danbooru_multi_search.danbooru.query_checkpoint import QueryCheckpoint
    from danbooru_multi_search.danbooru.query_planning import build_rank_query_plan
    from danbooru_multi_search.danbooru.query_language import parse_query_expression
    from danbooru_multi_search.danbooru.result_processing import ResultFilterSpec, ResultSortSpec
    from tests.test_query_execution import _harness
    plan = build_rank_query_plan(parse_query_expression(expression) if expression else None)
    harness = _harness(plan)
    harness.executor.cancel()
    execution = harness.executor.export_checkpoint()
    definition = GalleryQueryDefinition(QueryMode.RANK, expression, plan.rating, ResultFilterSpec(), ResultSortSpec(), execution.limits)
    raw = json.loads(encode_checkpoint(QueryCheckpoint("https://example.invalid", definition, definition.filter_spec, execution)))
    raw["plan"]["remote"][0]["candidates"] = candidates
    with pytest.raises(CheckpointError):
        decode_checkpoint(json.dumps(raw, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
