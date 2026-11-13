"""Immutable execution state; independent of Qt and persistence formats."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
import math
import re
from urllib.parse import urlsplit, urlunsplit

from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.danbooru.query_definition import GalleryQueryDefinition, QueryMode
from danbooru_multi_search.danbooru.query_language import parse_query_expression
from danbooru_multi_search.danbooru.post_validation import validate_post_values
from danbooru_multi_search.danbooru.query_evaluator import evaluate_post_query
from danbooru_multi_search.danbooru.query_planning import QueryPlan, RemoteOrder, expand_query_to_dnf
from danbooru_multi_search.danbooru.query_types import (
    QueryBranchPosition, QueryBranchProgress, QueryExecutionCounts, QueryExecutionFailure,
    QueryExecutionLimits, QueryExecutionState, QueryLimitReason,
)
from danbooru_multi_search.danbooru.result_processing import ResultFilterSpec, validate_result_configuration


class CheckpointError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def source_identity(value: str) -> str:
    """Retain the API origin/path, excluding credentials and URL parameters."""
    try:
        parts = urlsplit(value)
        if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
            raise ValueError("invalid API origin")
        host = parts.hostname.casefold()
        if ":" in host:
            host = f"[{host}]"
        port = parts.port
        if port is not None and (parts.scheme.casefold(), port) not in {("http", 80), ("https", 443)}:
            host += f":{port}"
        return urlunsplit((parts.scheme.casefold(), host, parts.path.rstrip("/"), "", ""))
    except (ValueError, TypeError, AttributeError) as exc:
        raise CheckpointError("invalid_source", "检查点的 API 来源无效。") from exc


def _integer(value: object, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


@dataclass(frozen=True, slots=True)
class ExecutionCheckpoint:
    plan: QueryPlan
    limits: QueryExecutionLimits
    state: QueryExecutionState
    counts: QueryExecutionCounts
    candidates: tuple[DanbooruPost, ...]
    matched_ids: tuple[int, ...]
    branches: tuple[QueryBranchProgress, ...]
    pending: tuple[int, ...]
    uncommitted: QueryBranchPosition | None
    failure: QueryExecutionFailure | None
    limit_reason: QueryLimitReason | None
    elapsed_active_seconds: float
    active: bool
    persistence_error: str | None = None

    def __post_init__(self) -> None:
        try:
            self._validate()
        except (ValueError, TypeError, AttributeError, IndexError) as exc:
            raise CheckpointError("invalid_checkpoint", f"执行检查点无效：{exc}") from exc

    def _validate(self) -> None:
        if not isinstance(self.plan, QueryPlan) or not isinstance(self.limits, QueryExecutionLimits):
            raise ValueError("plan/limits")
        if not isinstance(self.state, QueryExecutionState) or self.state is QueryExecutionState.IDLE:
            raise ValueError("state")
        if type(self.active) is not bool or isinstance(self.elapsed_active_seconds, bool) or not isinstance(self.elapsed_active_seconds, (int, float)):
            raise ValueError("activity")
        if not math.isfinite(self.elapsed_active_seconds) or self.elapsed_active_seconds < 0:
            raise ValueError("elapsed time")
        if not isinstance(self.counts, QueryExecutionCounts) or not all(_integer(value) for value in (
                self.counts.remote_rows, self.counts.unique_candidates, self.counts.local_matches)):
            raise ValueError("counts")
        if not isinstance(self.candidates, tuple) or not all(isinstance(post, DanbooruPost) and _integer(post.id, 1) for post in self.candidates):
            raise ValueError("candidates")
        identifiers = tuple(post.id for post in self.candidates)
        for post in self.candidates:
            validate_post_values(post)
        if len(set(identifiers)) != len(identifiers) or len(identifiers) != self.counts.unique_candidates or len(identifiers) > self.limits.candidate_limit:
            raise ValueError("candidate identity/count")
        expected_matches = tuple(post.id for post in self.candidates if self.plan.expression is None or evaluate_post_query(self.plan.expression, post))
        if not isinstance(self.matched_ids, tuple) or any(not _integer(value, 1) for value in self.matched_ids) or self.matched_ids != expected_matches:
            raise ValueError("matched identities/order")
        if self.counts.local_matches != len(expected_matches) or self.counts.remote_rows < len(identifiers):
            raise ValueError("matched/remote count")
        if not self.plan.remote_branches or len(self.branches) != len(self.plan.remote_branches):
            raise ValueError("branch count")
        if self.plan.expression is not None and expand_query_to_dnf(self.plan.expression) != self.plan.dnf_branches:
            raise ValueError("DNF does not match expression")
        if self.plan.expression is None and self.plan.dnf_branches:
            raise ValueError("unexpected DNF")
        if self.plan.remote_order is RemoteOrder.RANK:
            if len(self.plan.remote_branches) != 1:
                raise ValueError("rank branch count")
            remote = self.plan.remote_branches[0]
            if remote.remote_order is not RemoteOrder.RANK or remote.local_branch is not None or remote.rating != self.plan.rating:
                raise ValueError("rank branch")
            if self.plan.expression is None:
                if remote.candidate_tags:
                    raise ValueError("unexpected rank candidate")
            else:
                common = set(self.plan.dnf_branches[0].positive_tags)
                for branch in self.plan.dnf_branches[1:]:
                    common.intersection_update(branch.positive_tags)
                if len(remote.candidate_tags) != 1 or remote.candidate_tags[0] not in common:
                    raise ValueError("rank candidate is not common to every branch")
        elif self.plan.expression is None:
            if not self.plan.is_latest:
                raise ValueError("latest plan")
        elif len(self.plan.remote_branches) != len(self.plan.dnf_branches):
            raise ValueError("remote/local branch count")
        else:
            for remote, local in zip(self.plan.remote_branches, self.plan.dnf_branches, strict=True):
                if remote.local_branch != local or remote.remote_order is not None or remote.rating != self.plan.rating or not remote.candidate_tags or not set(remote.candidate_tags) <= local.positive_tags:
                    raise ValueError("remote/local branch mismatch")
        for index, branch in enumerate(self.branches):
            if not isinstance(branch, QueryBranchProgress) or type(branch.branch_index) is not int or branch.branch_index != index:
                raise ValueError("branch identity")
            if not _integer(branch.successful_pages) or branch.successful_pages > self.limits.branch_page_limit:
                raise ValueError("successful pages")
            if type(branch.natural_end) is not bool or type(branch.page_limited) is not bool or branch.natural_end and branch.page_limited:
                raise ValueError("branch flags")
            if branch.next_page is None:
                if not (branch.natural_end or branch.page_limited) or branch.successful_pages < 1:
                    raise ValueError("closed branch")
            elif not _integer(branch.next_page, 1) or branch.next_page != branch.successful_pages + 1 or branch.natural_end or branch.page_limited:
                raise ValueError("next page")
            if branch.page_limited and branch.successful_pages != self.limits.branch_page_limit:
                raise ValueError("page limit")
        if not isinstance(self.pending, tuple) or len(set(self.pending)) != len(self.pending) or any(not _integer(i) or i >= len(self.branches) for i in self.pending):
            raise ValueError("rotation")
        outstanding = {i for i, branch in enumerate(self.branches) if branch.next_page is not None}
        if self.uncommitted is not None:
            if not isinstance(self.uncommitted, QueryBranchPosition) or self.uncommitted.branch_index not in outstanding or self.uncommitted.page != self.branches[self.uncommitted.branch_index].next_page:
                raise ValueError("uncommitted page")
            outstanding.remove(self.uncommitted.branch_index)
        if set(self.pending) != outstanding:
            raise ValueError("missing/duplicate outstanding branch")
        if self.failure is not None and (not isinstance(self.failure, QueryExecutionFailure) or self.failure.position != self.uncommitted):
            raise ValueError("failure position")
        if self.failure is not None and self.failure.api_tags != self.plan.remote_branches[self.failure.position.branch_index].api_tags:
            raise ValueError("failure branch tags")
        if self.limit_reason is not None and not isinstance(self.limit_reason, QueryLimitReason):
            raise ValueError("limit reason")
        pages = sum(branch.successful_pages for branch in self.branches)
        short_pages = sum(branch.natural_end for branch in self.branches)
        minimum_rows = (pages - short_pages) * self.limits.page_size
        if not minimum_rows <= self.counts.remote_rows <= minimum_rows + short_pages * (self.limits.page_size - 1):
            raise ValueError("page/count mismatch")
        running = self.state in {QueryExecutionState.RUNNING, QueryExecutionState.WAITING, QueryExecutionState.RETRY_WAIT}
        if running != self.active:
            raise ValueError("activity/state mismatch")
        if self.state is QueryExecutionState.COMPLETE and (self.pending or self.uncommitted is not None or not all(branch.natural_end for branch in self.branches) or self.limit_reason is not None or self.failure is not None):
            raise ValueError("incomplete terminal state")
        if self.failure is not None and self.state not in {QueryExecutionState.FAILED, QueryExecutionState.PARTIAL_FAILED}:
            raise ValueError("unexpected failure")
        if self.persistence_error is not None and (not isinstance(self.persistence_error, str) or not self.persistence_error or self.state not in {QueryExecutionState.FAILED, QueryExecutionState.PARTIAL_FAILED}):
            raise ValueError("unexpected persistence error")
        if self.state in {QueryExecutionState.FAILED, QueryExecutionState.PARTIAL_FAILED} and self.failure is None and self.persistence_error is None:
            raise ValueError("missing failure reason")
        if (self.state is QueryExecutionState.PARTIAL_LIMITED) != (self.limit_reason is not None):
            raise ValueError("limit state/reason mismatch")
        if self.state is QueryExecutionState.FAILED and self.counts.local_matches or self.state is QueryExecutionState.PARTIAL_FAILED and not self.counts.local_matches:
            raise ValueError("failure/match count mismatch")

    @property
    def resume_reason(self) -> str:
        if self.active:
            return "上次异常结束，无法确认剩余活动时间，不能续扫。可以从头执行。"
        if self.state is QueryExecutionState.COMPLETE:
            return "查询已经完整结束，无需续扫。"
        if self.limit_reason is not None or self.counts.unique_candidates >= self.limits.candidate_limit or self.elapsed_active_seconds >= self.limits.active_time_limit_seconds:
            return "查询已达到原扫描上限，不能通过续扫增加额度。可以从头执行。"
        if not self.pending and self.uncommitted is None:
            return "没有可继续扫描的页面。"
        return ""


@dataclass(frozen=True, slots=True)
class QueryCheckpoint:
    source: str
    definition: GalleryQueryDefinition
    runtime_filter: ResultFilterSpec
    execution: ExecutionCheckpoint

    def __post_init__(self) -> None:
        if self.source != source_identity(self.source):
            raise CheckpointError("invalid_source", "检查点来源未规范化。")
        if not isinstance(self.definition, GalleryQueryDefinition) or not isinstance(self.execution, ExecutionCheckpoint):
            raise CheckpointError("invalid_checkpoint", "检查点缺少完整查询或执行状态。")
        from dataclasses import replace
        configuration = validate_result_configuration(self.runtime_filter, self.definition.sort_spec, remote_order=self.execution.plan.remote_order)
        if replace(configuration.filter_spec, age_reference_time=None) != self.definition.filter_spec or self.execution.limits != self.definition.limits:
            raise CheckpointError("invalid_checkpoint", "检查点筛选条件或额度不一致。")
        expected_expression = parse_query_expression(self.definition.normalized_expression).root if self.definition.normalized_expression is not None else None
        if self.execution.plan.expression != expected_expression or self.execution.plan.rating != self.definition.rating or (self.execution.plan.remote_order is RemoteOrder.RANK) != (self.definition.mode is QueryMode.RANK) or (self.definition.mode is QueryMode.LATEST and not self.execution.plan.is_latest):
            raise CheckpointError("invalid_checkpoint", "检查点查询模式或执行计划不一致。")
        reference = self.runtime_filter.age_reference_time
        if reference is not None and (reference.tzinfo is None or reference.utcoffset() is None or reference.astimezone(timezone.utc) != reference):
            raise CheckpointError("invalid_checkpoint", "检查点年龄参考时间无效。")


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    history_id: int
    owner: str
    view_revision: int
    revision: int
    checkpoint: QueryCheckpoint

    def __post_init__(self) -> None:
        if not _integer(self.history_id, 1) or not isinstance(self.owner, str) or re.fullmatch(r"[a-f0-9]{32}", self.owner) is None or not _integer(self.view_revision) or not _integer(self.revision) or self.revision > 2**63 - 1:
            raise CheckpointError("invalid_checkpoint", "检查点归属或修订号无效。")
