"""M2 query orchestration independent from the existing M1 gallery controller."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from PySide6.QtCore import QObject, Signal

from danbooru_multi_search.danbooru.gallery import DanbooruPostListModel
from danbooru_multi_search.danbooru.query_definition import (
    GalleryQueryDefinition,
    QueryMode,
)
from danbooru_multi_search.danbooru.query_execution import (
    DanbooruMultiBranchExecutor,
)
# Existing callers may continue to import GalleryResultSummary from here.
from danbooru_multi_search.danbooru.query_types import (
    GalleryResultSummary,
    QueryExecutionAccepted,
    QueryExecutionBound,
    QueryExecutionErrorCode,
    QueryExecutionLimits,
    QueryExecutionSnapshot,
    QueryExecutionState,
    QueryLimitReason,
)
from danbooru_multi_search.danbooru.query_language import (
    ParsedQuery,
    analyze_query,
    parse_query_expression,
)
from danbooru_multi_search.danbooru.query_planning import (
    QueryPlan,
    RemoteOrder,
    build_latest_query_plan,
    build_rank_query_plan,
    build_standard_query_plan,
    normalize_plan_rating,
)
from danbooru_multi_search.danbooru.result_processing import (
    ProcessedQueryResults,
    QueryResultSession,
    ResultFilterSpec,
    ResultProcessingError,
    ResultSortSpec,
    build_query_result_session,
    process_query_snapshot,
    validate_result_configuration,
)
from danbooru_multi_search.danbooru.tag_translation import TagTranslationStore


class GalleryQueryOrigin(StrEnum):
    STANDARD = "standard"
    RANK = "rank"
    LATEST = "latest"
    HISTORY = "history"
    REFRESH = "refresh"


@dataclass(frozen=True, slots=True)
class GalleryQueryAccepted:
    operation_id: int
    execution_id: int
    definition: GalleryQueryDefinition
    display_text: str
    origin: GalleryQueryOrigin
    query_tags: tuple[str, ...]
    accepted_at: datetime
    count_tag_usage: bool


@dataclass(frozen=True, slots=True)
class GalleryViewDefinitionChanged:
    operation_id: int
    execution_id: int
    view_generation: int
    definition: GalleryQueryDefinition
    applied_to_gallery: bool


@dataclass(frozen=True, slots=True)
class GalleryResultEvent:
    operation_id: int
    execution_id: int
    view_generation: int
    definition: GalleryQueryDefinition
    summary: GalleryResultSummary
    gallery_operation_id: int | None


@dataclass(slots=True)
class _PreparedOperation:
    operation_id: int
    definition: GalleryQueryDefinition
    runtime_filter: ResultFilterSpec
    display_text: str
    origin: GalleryQueryOrigin
    query_tags: tuple[str, ...]
    plan: QueryPlan
    binding_token: object
    execution_id: int | None = None
    result_session: QueryResultSession | None = None
    view_generation: int = 0
    accepted: bool = False
    last_snapshot: QueryExecutionSnapshot | None = None
    last_processed: ProcessedQueryResults | None = None
    seen_snapshot_keys: set[tuple[object, ...]] = field(default_factory=set)


class DanbooruM2QueryController(QObject):
    """Coordinate parse, plan, execute, process, and M2 model submission."""

    queryAccepted = Signal(object)
    viewDefinitionChanged = Signal(object)
    resultSummaryChanged = Signal(object)

    def __init__(
        self,
        base_url: str,
        *,
        executor: DanbooruMultiBranchExecutor | None = None,
        tag_store: TagTranslationStore | None = None,
        posts_model: DanbooruPostListModel | None = None,
        clock: Callable[[], datetime] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._executor = executor or DanbooruMultiBranchExecutor(
            base_url,
            parent=self,
        )
        self._tag_store = tag_store or TagTranslationStore(parent=self)
        self._posts_model = posts_model or DanbooruPostListModel(self)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._operation_counter = 0
        self._pending_operation: _PreparedOperation | None = None
        self._current_operation: _PreparedOperation | None = None
        self._closing = False
        self._closed = False
        self._final_cancel_summary_emitted = False
        self._last_accepted_at: datetime | None = None
        self._executor.executionBound.connect(self._execution_bound)
        self._executor.firstPageAccepted.connect(self._first_page_accepted)
        self._executor.snapshotChanged.connect(self._snapshot_changed)

    @property
    def posts_model(self) -> DanbooruPostListModel:
        return self._posts_model

    @property
    def executor(self) -> DanbooruMultiBranchExecutor:
        return self._executor

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def operation_id(self) -> int | None:
        operation = self._current_operation
        return operation.operation_id if operation is not None else None

    @property
    def execution_id(self) -> int | None:
        operation = self._current_operation
        return operation.execution_id if operation is not None else None

    @property
    def view_generation(self) -> int:
        operation = self._current_operation
        return operation.view_generation if operation is not None else 0

    @property
    def definition(self) -> GalleryQueryDefinition | None:
        operation = self._current_operation
        return operation.definition if operation is not None else None

    @property
    def gallery_operation_id(self) -> int | None:
        return self._posts_model.display_operation_id

    @property
    def last_snapshot(self) -> QueryExecutionSnapshot | None:
        operation = self._current_operation
        return operation.last_snapshot if operation is not None else None

    @property
    def last_processed(self) -> ProcessedQueryResults | None:
        operation = self._current_operation
        return operation.last_processed if operation is not None else None

    def start_standard(
        self,
        expression: str,
        *,
        display_text: str | None = None,
        rating: str = "all",
        filter_spec: ResultFilterSpec | None = None,
        sort_spec: ResultSortSpec | None = None,
        limits: QueryExecutionLimits | None = None,
    ) -> int:
        return self._start_new(
            mode=QueryMode.STANDARD,
            expression=expression,
            display_text=expression if display_text is None else display_text,
            rating=rating,
            filter_spec=filter_spec,
            sort_spec=sort_spec,
            limits=limits,
            origin=GalleryQueryOrigin.STANDARD,
        )

    def start_rank(
        self,
        expression: str | None = None,
        *,
        display_text: str | None = None,
        rating: str = "all",
        filter_spec: ResultFilterSpec | None = None,
        sort_spec: ResultSortSpec | None = None,
        limits: QueryExecutionLimits | None = None,
    ) -> int:
        resolved_display = (
            display_text
            if display_text is not None
            else (expression if expression and expression.strip() else "近期热门")
        )
        return self._start_new(
            mode=QueryMode.RANK,
            expression=expression,
            display_text=resolved_display,
            rating=rating,
            filter_spec=filter_spec,
            sort_spec=sort_spec,
            limits=limits,
            origin=GalleryQueryOrigin.RANK,
        )

    def start_latest(
        self,
        *,
        display_text: str = "最新作品",
        rating: str = "all",
        filter_spec: ResultFilterSpec | None = None,
        sort_spec: ResultSortSpec | None = None,
        limits: QueryExecutionLimits | None = None,
    ) -> int:
        return self._start_new(
            mode=QueryMode.LATEST,
            expression=None,
            display_text=display_text,
            rating=rating,
            filter_spec=filter_spec,
            sort_spec=sort_spec,
            limits=limits,
            origin=GalleryQueryOrigin.LATEST,
        )

    def reexecute_history(
        self,
        definition: GalleryQueryDefinition,
        *,
        display_text: str,
    ) -> int:
        if not isinstance(definition, GalleryQueryDefinition):
            raise TypeError("definition must be a GalleryQueryDefinition")
        return self._start_new(
            mode=definition.mode,
            expression=definition.normalized_expression,
            display_text=display_text,
            rating=definition.rating.value,
            filter_spec=definition.filter_spec,
            sort_spec=definition.sort_spec,
            limits=definition.limits,
            origin=GalleryQueryOrigin.HISTORY,
        )

    def refresh(self) -> int:
        self._ensure_open()
        operation = self._current_operation
        if operation is None:
            raise RuntimeError("There is no query to refresh.")
        definition = operation.definition
        return self._start_new(
            mode=definition.mode,
            expression=definition.normalized_expression,
            display_text=operation.display_text,
            rating=definition.rating.value,
            filter_spec=definition.filter_spec,
            sort_spec=definition.sort_spec,
            limits=definition.limits,
            origin=GalleryQueryOrigin.REFRESH,
        )

    def cancel(self) -> bool:
        if self._closed or self._closing:
            return False
        return self._executor.cancel()

    def retry_failed_page(self) -> bool:
        if self._closed or self._closing:
            return False
        return self._executor.retry_failed_page()

    def update_view(
        self,
        *,
        filter_spec: ResultFilterSpec | None = None,
        sort_spec: ResultSortSpec | None = None,
    ) -> bool:
        self._ensure_open()
        operation = self._current_operation
        if operation is None or operation.execution_id is None:
            return False
        if filter_spec is None:
            logical_filter = operation.definition.filter_spec
            runtime_filter = operation.runtime_filter
        else:
            logical_filter = self._logical_filter(filter_spec)
            if (
                logical_filter.minimum_age is not None
                or logical_filter.maximum_age is not None
            ):
                runtime_filter = self._runtime_filter(
                    logical_filter,
                    self._clock_utc(),
                )
            else:
                runtime_filter = logical_filter
        candidate_sort = (
            operation.definition.sort_spec if sort_spec is None else sort_spec
        )
        configuration = validate_result_configuration(
            runtime_filter,
            candidate_sort,
            remote_order=operation.plan.remote_order,
        )
        normalized_logical_filter = replace(
            configuration.filter_spec,
            age_reference_time=None,
        )
        candidate_definition = GalleryQueryDefinition(
            mode=operation.definition.mode,
            normalized_expression=operation.definition.normalized_expression,
            rating=operation.definition.rating,
            filter_spec=normalized_logical_filter,
            sort_spec=configuration.sort_spec,
            limits=operation.definition.limits,
        )
        snapshot = operation.last_snapshot
        result: ProcessedQueryResults | None = None
        old_view_generation = operation.view_generation
        if snapshot is not None and operation.result_session is not None:
            result = process_query_snapshot(
                snapshot,
                operation.result_session,
                configuration.filter_spec,
                configuration.sort_spec,
            )
            if not self._operation_current(
                operation,
                snapshot=snapshot,
                view_generation=old_view_generation,
            ):
                return False
            self._commit_result_if_needed(operation, result)
            if not self._operation_current(
                operation,
                snapshot=snapshot,
                view_generation=old_view_generation,
            ):
                return False

        operation.runtime_filter = configuration.filter_spec
        operation.definition = candidate_definition
        operation.view_generation = old_view_generation + 1
        if result is not None:
            operation.last_processed = result
        event = GalleryViewDefinitionChanged(
            operation_id=operation.operation_id,
            execution_id=operation.execution_id,
            view_generation=operation.view_generation,
            definition=operation.definition,
            applied_to_gallery=(
                self._posts_model.display_operation_id == operation.operation_id
            ),
        )
        self.viewDefinitionChanged.emit(event)
        if not self._operation_current(
            operation,
            snapshot=snapshot,
            view_generation=event.view_generation,
        ):
            return True
        if result is not None and operation.accepted:
            self._emit_result_summary(operation, result)
        return True

    def shutdown(self) -> None:
        if self._closed or self._closing:
            return
        self._closing = True
        self._executor.shutdown()
        self._closed = True
        self._closing = False
        self._pending_operation = None

    def _start_new(
        self,
        *,
        mode: QueryMode,
        expression: str | None,
        display_text: str,
        rating: str,
        filter_spec: ResultFilterSpec | None,
        sort_spec: ResultSortSpec | None,
        limits: QueryExecutionLimits | None,
        origin: GalleryQueryOrigin,
    ) -> int:
        self._ensure_open()
        prepared = self._prepare_operation(
            mode=mode,
            expression=expression,
            display_text=display_text,
            rating=rating,
            filter_spec=filter_spec,
            sort_spec=sort_spec,
            limits=limits,
            origin=origin,
        )
        previous_pending = self._pending_operation
        self._pending_operation = prepared
        try:
            execution_id = self._executor.start(
                prepared.plan,
                prepared.definition.limits,
                binding_token=prepared.binding_token,
            )
        except Exception:
            if self._pending_operation is prepared:
                self._pending_operation = previous_pending
            raise
        if prepared.execution_id is None:
            if self._pending_operation is prepared:
                self._pending_operation = None
            if self._closed or self._closing:
                return prepared.operation_id
            if self._executor.current_execution_id != execution_id:
                return prepared.operation_id
            raise RuntimeError("The query executor did not emit its binding event.")
        if execution_id != prepared.execution_id:
            raise RuntimeError("The query executor returned a different execution id.")
        if self._current_operation is prepared:
            snapshot = self._executor.snapshot
            if (
                snapshot.execution_id == execution_id
                and prepared.last_snapshot is not snapshot
            ):
                self._consume_snapshot(prepared, snapshot)
        return prepared.operation_id

    def _prepare_operation(
        self,
        *,
        mode: QueryMode,
        expression: str | None,
        display_text: str,
        rating: str,
        filter_spec: ResultFilterSpec | None,
        sort_spec: ResultSortSpec | None,
        limits: QueryExecutionLimits | None,
        origin: GalleryQueryOrigin,
    ) -> _PreparedOperation:
        parsed = self._parse_for_mode(mode, expression)
        normalized_expression = parsed.normalized_text if parsed is not None else None
        if not isinstance(display_text, str) or not display_text.strip():
            raise ValueError("display_text must be non-empty text")
        normalized_rating = normalize_plan_rating(rating)
        logical_filter = self._logical_filter(
            ResultFilterSpec() if filter_spec is None else filter_spec
        )
        resolved_limits = QueryExecutionLimits() if limits is None else limits
        if not isinstance(resolved_limits, QueryExecutionLimits):
            raise TypeError("limits must be QueryExecutionLimits")
        candidate_sort = ResultSortSpec() if sort_spec is None else sort_spec
        logical_definition = GalleryQueryDefinition(
            mode=mode,
            normalized_expression=normalized_expression,
            rating=normalized_rating,
            filter_spec=logical_filter,
            sort_spec=candidate_sort,
            limits=resolved_limits,
        )
        moment = self._clock_utc()
        runtime_filter = self._runtime_filter(
            logical_definition.filter_spec,
            moment,
        )
        remote_order = RemoteOrder.RANK if mode == QueryMode.RANK else None
        configuration = validate_result_configuration(
            runtime_filter,
            logical_definition.sort_spec,
            remote_order=remote_order,
        )
        normalized_logical_filter = replace(
            configuration.filter_spec,
            age_reference_time=None,
        )
        query_tags = self._query_tags(parsed)
        counts = self._post_count_snapshot(query_tags)
        plan = self._build_plan(
            mode,
            parsed,
            counts,
            normalized_rating.value,
        )
        final_definition = GalleryQueryDefinition(
            mode=mode,
            normalized_expression=normalized_expression,
            rating=plan.rating,
            filter_spec=normalized_logical_filter,
            sort_spec=configuration.sort_spec,
            limits=resolved_limits,
        )
        self._operation_counter += 1
        return _PreparedOperation(
            operation_id=self._operation_counter,
            definition=final_definition,
            runtime_filter=configuration.filter_spec,
            display_text=display_text,
            origin=origin,
            query_tags=query_tags,
            plan=plan,
            binding_token=object(),
        )

    @staticmethod
    def _parse_for_mode(
        mode: QueryMode,
        expression: str | None,
    ) -> ParsedQuery | None:
        if not isinstance(mode, QueryMode):
            raise TypeError("mode must be a QueryMode")
        if mode == QueryMode.LATEST:
            if expression is not None:
                raise ValueError("latest queries cannot contain an expression")
            return None
        if mode == QueryMode.RANK and (
            expression is None
            or (isinstance(expression, str) and not expression.strip())
        ):
            return None
        return parse_query_expression(expression)

    @staticmethod
    def _logical_filter(spec: ResultFilterSpec) -> ResultFilterSpec:
        if not isinstance(spec, ResultFilterSpec):
            raise TypeError("filter_spec must be a ResultFilterSpec")
        if spec.age_reference_time is not None:
            raise ValueError("logical filter definitions cannot store age_reference_time")
        return spec

    @staticmethod
    def _runtime_filter(spec: ResultFilterSpec, moment: datetime) -> ResultFilterSpec:
        if spec.minimum_age is None and spec.maximum_age is None:
            return spec
        return replace(spec, age_reference_time=moment)

    @staticmethod
    def _query_tags(parsed: ParsedQuery | None) -> tuple[str, ...]:
        if parsed is None:
            return ()
        analysis = analyze_query(parsed)
        return tuple(sorted(set(analysis.positive_tags + analysis.negative_tags)))

    def _post_count_snapshot(
        self,
        tags: tuple[str, ...],
    ) -> dict[str, int | None]:
        snapshot = self._tag_store.snapshot
        counts: dict[str, int | None] = {}
        for tag in tags:
            record = snapshot.records_by_name.get(tag)
            value = record.post_count if record is not None else None
            counts[tag] = (
                value
                if isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
                else None
            )
        return counts

    @staticmethod
    def _build_plan(
        mode: QueryMode,
        parsed: ParsedQuery | None,
        counts: dict[str, int | None],
        rating: str,
    ) -> QueryPlan:
        if mode == QueryMode.STANDARD:
            assert parsed is not None
            return build_standard_query_plan(
                parsed,
                post_counts=counts,
                rating=rating,
            )
        if mode == QueryMode.RANK:
            return build_rank_query_plan(
                parsed,
                post_counts=counts,
                rating=rating,
            )
        return build_latest_query_plan(rating=rating)

    def _execution_bound(self, event: object) -> None:
        if self._closed or not isinstance(event, QueryExecutionBound):
            return
        operation = self._pending_operation
        if (
            operation is None
            or event.binding_token is not operation.binding_token
            or self._executor.current_execution_id != event.execution_id
        ):
            return
        operation.execution_id = event.execution_id
        operation.result_session = build_query_result_session(
            event.execution_id,
            operation.plan,
        )
        self._current_operation = operation
        self._pending_operation = None

    def _first_page_accepted(self, event: object) -> None:
        if self._closed or self._closing or not isinstance(
            event,
            QueryExecutionAccepted,
        ):
            return
        operation = self._current_operation
        if (
            operation is None
            or operation.execution_id != event.execution_id
            or operation.accepted
            or not self._executor.is_execution_active(event.execution_id)
        ):
            return
        try:
            accepted_at = self._next_accepted_at(self._clock_utc())
        except ValueError:
            self._executor.cancel()
            return
        operation.accepted = True
        accepted = GalleryQueryAccepted(
            operation_id=operation.operation_id,
            execution_id=event.execution_id,
            definition=operation.definition,
            display_text=operation.display_text,
            origin=operation.origin,
            query_tags=operation.query_tags,
            accepted_at=accepted_at,
            count_tag_usage=(
                operation.origin != GalleryQueryOrigin.REFRESH
                and bool(operation.query_tags)
            ),
        )
        self.queryAccepted.emit(accepted)

    def _snapshot_changed(self, snapshot: object) -> None:
        if not isinstance(snapshot, QueryExecutionSnapshot):
            return
        operation = self._current_operation
        if operation is None or operation.execution_id != snapshot.execution_id:
            return
        if self._closed:
            return
        if self._closing and snapshot.state != QueryExecutionState.PARTIAL_CANCELLED:
            return
        if self._closing and self._final_cancel_summary_emitted:
            return
        self._consume_snapshot(operation, snapshot)

    def _consume_snapshot(
        self,
        operation: _PreparedOperation,
        snapshot: QueryExecutionSnapshot,
    ) -> None:
        if operation.result_session is None:
            return
        snapshot_key = self._snapshot_key(snapshot)
        if snapshot_key in operation.seen_snapshot_keys:
            return
        view_generation = operation.view_generation
        operation.seen_snapshot_keys.add(snapshot_key)
        operation.last_snapshot = snapshot
        try:
            result = process_query_snapshot(
                snapshot,
                operation.result_session,
                operation.runtime_filter,
                operation.definition.sort_spec,
            )
        except ResultProcessingError:
            return
        if not self._operation_current(
            operation,
            snapshot=snapshot,
            view_generation=view_generation,
            allow_closing=True,
        ):
            return
        self._commit_result_if_needed(operation, result)
        if not self._operation_current(
            operation,
            snapshot=snapshot,
            view_generation=view_generation,
            allow_closing=True,
        ):
            return
        operation.last_processed = result
        if not operation.accepted:
            return
        self._emit_result_summary(operation, result)
        if self._closing and snapshot.state == QueryExecutionState.PARTIAL_CANCELLED:
            self._final_cancel_summary_emitted = True

    def _commit_result_if_needed(
        self,
        operation: _PreparedOperation,
        result: ProcessedQueryResults,
    ) -> bool:
        snapshot = result.source_snapshot
        already_displayed = (
            self._posts_model.display_operation_id == operation.operation_id
        )
        should_commit = (
            already_displayed
            or bool(snapshot.posts)
            or snapshot.state == QueryExecutionState.COMPLETE
        )
        if not should_commit:
            return False
        self._posts_model.set_processed_posts(operation.operation_id, result.items)
        return True

    def _emit_result_summary(
        self,
        operation: _PreparedOperation,
        result: ProcessedQueryResults,
    ) -> None:
        snapshot = result.source_snapshot
        execution_id = operation.execution_id
        if execution_id is None:
            return
        failure_code = (
            snapshot.failure.error_code if snapshot.failure is not None else None
        )
        event = GalleryResultEvent(
            operation_id=operation.operation_id,
            execution_id=execution_id,
            view_generation=operation.view_generation,
            definition=operation.definition,
            summary=GalleryResultSummary(
                state=snapshot.state,
                remote_rows=snapshot.counts.remote_rows,
                unique_candidates=snapshot.counts.unique_candidates,
                local_matches=snapshot.counts.local_matches,
                visible_count=result.visible_count,
                limit_reason=snapshot.limit_reason,
                failure_error_code=failure_code,
            ),
            gallery_operation_id=self._posts_model.display_operation_id,
        )
        self.resultSummaryChanged.emit(event)

    def _operation_current(
        self,
        operation: _PreparedOperation,
        *,
        snapshot: QueryExecutionSnapshot | None,
        view_generation: int,
        allow_closing: bool = False,
    ) -> bool:
        return (
            self._current_operation is operation
            and operation.execution_id is not None
            and operation.result_session is not None
            and operation.result_session.execution_id == operation.execution_id
            and operation.view_generation == view_generation
            and operation.last_snapshot is snapshot
            and not self._closed
            and (allow_closing or not self._closing)
        )

    @staticmethod
    def _snapshot_key(snapshot: QueryExecutionSnapshot) -> tuple[object, ...]:
        return (
            snapshot.execution_id,
            snapshot.state,
            snapshot.counts,
            tuple(post.id for post in snapshot.posts),
            snapshot.branch_progress,
            snapshot.limit_reason,
            snapshot.failure,
        )

    def _clock_utc(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise ValueError("query controller clock must return datetime")
        try:
            if value.utcoffset() is None:
                raise ValueError("query controller clock must be timezone-aware")
            return value.astimezone(timezone.utc)
        except (OverflowError, OSError, TypeError, ValueError) as exc:
            raise ValueError(f"query controller clock is invalid: {exc}") from exc

    def _next_accepted_at(self, value: datetime) -> datetime:
        previous = self._last_accepted_at
        if previous is not None and value <= previous:
            try:
                value = previous + timedelta(microseconds=1)
            except OverflowError:
                value = previous
        self._last_accepted_at = value
        return value

    def _ensure_open(self) -> None:
        if self._closed or self._closing:
            raise RuntimeError("The M2 query controller has been shut down.")
