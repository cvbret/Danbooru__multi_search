"""Versioned complete checkpoints, including non-matching API candidates."""

from __future__ import annotations

from dataclasses import asdict, fields, replace
from datetime import datetime, timezone
import json

from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.danbooru.query_checkpoint import CheckpointError, ExecutionCheckpoint, QueryCheckpoint
from danbooru_multi_search.danbooru.query_language import parse_query_expression
from danbooru_multi_search.danbooru.query_planning import QueryBranch, QueryPlan, QueryRating, RemoteOrder, RemoteQueryBranch
from danbooru_multi_search.danbooru.query_types import (
    QueryBranchPosition, QueryBranchProgress, QueryExecutionCounts, QueryExecutionErrorCode,
    QueryExecutionFailure, QueryExecutionState, QueryLimitReason,
)
from danbooru_multi_search.storage.query_serialization import decode_query_definition, encode_query_definition

CHECKPOINT_VERSION = 1


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def encode_checkpoint(value: QueryCheckpoint) -> str:
    if not isinstance(value, QueryCheckpoint):
        raise CheckpointError("invalid_checkpoint", "检查点内容无效。")
    execution = value.execution
    plan = execution.plan
    def branch(item):
        return None if item is None else {"positive": sorted(item.positive_tags), "excluded": sorted(item.excluded_tags)}
    data = {
        "version": CHECKPOINT_VERSION, "source": value.source,
        "definition": json.loads(encode_query_definition(value.definition)),
        "age_reference_time": value.runtime_filter.age_reference_time.astimezone(timezone.utc).isoformat(timespec="microseconds") if value.runtime_filter.age_reference_time is not None else None,
        "plan": {"dnf": [branch(item) for item in plan.dnf_branches],
                 "remote": [{"candidates": list(item.candidate_tags), "rating": item.rating.value,
                             "order": item.remote_order.value if item.remote_order else None,
                             "local": branch(item.local_branch)} for item in plan.remote_branches],
                 "rating": plan.rating.value, "order": plan.remote_order.value if plan.remote_order else None},
        "execution": {"state": execution.state.value, "counts": asdict(execution.counts),
                      "candidates": [post.to_mapping() for post in execution.candidates],
                      "matched_ids": list(execution.matched_ids), "branches": [asdict(item) for item in execution.branches],
                      "pending": list(execution.pending), "uncommitted": asdict(execution.uncommitted) if execution.uncommitted else None,
                      "failure": asdict(execution.failure) if execution.failure else None,
                      "limit_reason": execution.limit_reason.value if execution.limit_reason else None,
                      "elapsed_active_seconds": execution.elapsed_active_seconds, "active": execution.active},
    }
    data["execution"]["persistence_error"] = execution.persistence_error
    return _json(data)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _object(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError("missing or unknown fields")
    return value


def _array(value):
    if not isinstance(value, list):
        raise ValueError("expected array")
    return value


def _branch(value):
    _object(value, ("positive", "excluded"))
    positive, excluded = _array(value["positive"]), _array(value["excluded"])
    if positive != sorted(set(positive)) or excluded != sorted(set(excluded)):
        raise ValueError("non-canonical branch")
    return QueryBranch(frozenset(positive), frozenset(excluded))


def _post(value):
    _object(value, (item.name for item in fields(DanbooruPost)))
    value = dict(value)
    for name in ("artist_tags", "character_tags", "copyright_tags", "general_tags", "meta_tags"):
        value[name] = tuple(_array(value[name]))
    from danbooru_multi_search.danbooru.post_validation import validate_post_values
    post = DanbooruPost(**value)
    validate_post_values(post)
    return post


def decode_checkpoint(encoded: str) -> QueryCheckpoint:
    try:
        raw = json.loads(encoded, object_pairs_hook=_pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        _object(raw, ("version", "source", "definition", "age_reference_time", "plan", "execution"))
        if type(raw["version"]) is not int or raw["version"] != CHECKPOINT_VERSION:
            raise CheckpointError("unsupported_checkpoint", "检查点版本不受支持，不能续扫。")
        definition = decode_query_definition(_json(raw["definition"]))
        plan = _object(raw["plan"], ("dnf", "remote", "rating", "order"))
        remote = []
        for item in _array(plan["remote"]):
            _object(item, ("candidates", "rating", "order", "local"))
            remote.append(RemoteQueryBranch(tuple(_array(item["candidates"])), QueryRating(item["rating"]),
                                           RemoteOrder(item["order"]) if item["order"] is not None else None,
                                           _branch(item["local"]) if item["local"] is not None else None))
        expression = parse_query_expression(definition.normalized_expression).root if definition.normalized_expression is not None else None
        saved_plan = QueryPlan(expression, tuple(_branch(item) for item in _array(plan["dnf"])), tuple(remote),
                               QueryRating(plan["rating"]), RemoteOrder(plan["order"]) if plan["order"] is not None else None)
        state = _object(raw["execution"], ("state", "counts", "candidates", "matched_ids", "branches", "pending", "uncommitted", "failure", "limit_reason", "elapsed_active_seconds", "active", "persistence_error"))
        _object(state["counts"], ("remote_rows", "unique_candidates", "local_matches"))
        branches = []
        for item in _array(state["branches"]):
            _object(item, ("branch_index", "next_page", "successful_pages", "natural_end", "page_limited"))
            branches.append(QueryBranchProgress(**item))
        def position(value):
            _object(value, ("branch_index", "page"))
            return QueryBranchPosition(**value)
        failure = state["failure"]
        if failure is not None:
            _object(failure, ("position", "api_tags", "error_code", "http_status", "message"))
            if not isinstance(failure["api_tags"], str) or not isinstance(failure["message"], str) or (failure["http_status"] is not None and (type(failure["http_status"]) is not int or not 100 <= failure["http_status"] <= 599)):
                raise ValueError("invalid failure")
            failure = QueryExecutionFailure(position(failure["position"]), failure["api_tags"], QueryExecutionErrorCode(failure["error_code"]), failure["http_status"], failure["message"])
        execution = ExecutionCheckpoint(saved_plan, definition.limits, QueryExecutionState(state["state"]), QueryExecutionCounts(**state["counts"]),
            tuple(_post(item) for item in _array(state["candidates"])), tuple(_array(state["matched_ids"])), tuple(branches), tuple(_array(state["pending"])),
            position(state["uncommitted"]) if state["uncommitted"] is not None else None, failure,
            QueryLimitReason(state["limit_reason"]) if state["limit_reason"] is not None else None, state["elapsed_active_seconds"], state["active"], state["persistence_error"])
        reference = raw["age_reference_time"]
        if reference is not None:
            reference = datetime.fromisoformat(reference)
            if reference.tzinfo is None or reference.utcoffset() is None:
                raise ValueError("age reference must contain timezone")
        value = QueryCheckpoint(raw["source"], definition, replace(definition.filter_spec, age_reference_time=reference), execution)
        if encode_checkpoint(value) != encoded:
            raise ValueError("checkpoint is not canonical")
        return value
    except CheckpointError:
        raise
    except (TypeError, ValueError, OverflowError, KeyError, AttributeError, RecursionError) as exc:
        raise CheckpointError("invalid_checkpoint", f"检查点内容损坏，不能续扫：{exc}") from exc
