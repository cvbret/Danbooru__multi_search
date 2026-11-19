"""Capture ordered query candidates and extend only their original query scope."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from PySide6.QtCore import QObject, Signal

from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.danbooru.query_checkpoint import QueryCheckpoint
from danbooru_multi_search.danbooru.query_controller import M2QueryCoordinator
from danbooru_multi_search.danbooru.query_execution import DanbooruMultiBranchExecutor
from danbooru_multi_search.danbooru.query_types import (
    ACTIVE_EXECUTION_STATES,
    DEFAULT_CANDIDATE_LIMIT,
    QueryExecutionSnapshot,
    QueryExecutionState,
    QueryLimitReason,
)
from danbooru_multi_search.danbooru.result_processing import (
    PostFileCategory,
    build_query_result_session,
    matches_file_category,
    process_query_snapshot,
)


DEFAULT_CANDIDATE_COUNT = 20
MAX_CANDIDATE_COUNT = 100


class CandidateSelectionMode(StrEnum):
    FIRST = "first"
    FROM_POSITION = "from_position"
    SELECTED = "selected"


@dataclass(frozen=True, slots=True)
class CandidateCriteria:
    mode: CandidateSelectionMode = CandidateSelectionMode.FIRST
    count: int = DEFAULT_CANDIDATE_COUNT
    start: int = 1
    file_categories: frozenset[PostFileCategory] | None = None
    ratings: frozenset[str] | None = None
    minimum_score: int | None = None
    minimum_fav_count: int | None = None
    allow_more: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.mode, CandidateSelectionMode):
            raise ValueError("候选选择方式无效。")
        if type(self.count) is not int or not 1 <= self.count <= MAX_CANDIDATE_COUNT:
            raise ValueError("单次采集数量必须为 1 至 100 的整数。")
        if type(self.start) is not int or not 1 <= self.start <= DEFAULT_CANDIDATE_LIMIT:
            raise ValueError(f"起始位置必须为 1 至 {DEFAULT_CANDIDATE_LIMIT} 的整数。")
        if self.file_categories is not None and (
            not isinstance(self.file_categories, frozenset)
            or not self.file_categories
            or any(not isinstance(value, PostFileCategory) for value in self.file_categories)
        ):
            raise ValueError("文件类型必须为至少一种有效类型。")
        if self.ratings is not None and (
            not isinstance(self.ratings, frozenset)
            or not self.ratings
            or any(type(value) is not str or value not in {"g", "s", "q", "e"} for value in self.ratings)
        ):
            raise ValueError("内容分级无效。")
        if self.minimum_score is not None and type(self.minimum_score) is not int:
            raise ValueError("最低得分必须为整数。")
        if self.minimum_fav_count is not None and (
            type(self.minimum_fav_count) is not int or self.minimum_fav_count < 0
        ):
            raise ValueError("最低收藏数必须为非负整数。")
        if type(self.allow_more) is not bool:
            raise ValueError("补页选项必须为布尔值。")


@dataclass(frozen=True, slots=True)
class CandidateSource:
    identifier: str
    operation_id: int
    execution_id: int
    view_generation: int
    model_revision: int
    posts: tuple[DanbooruPost, ...]
    selected_keys: tuple[str, ...]
    checkpoint: QueryCheckpoint
    snapshot: QueryExecutionSnapshot
    range_description: str

    @property
    def partial(self) -> bool:
        return self.checkpoint.execution.state is not QueryExecutionState.COMPLETE

    @property
    def can_load_more(self) -> bool:
        execution = self.checkpoint.execution
        return execution.active or not execution.resume_reason


@dataclass(frozen=True, slots=True)
class CandidateResult:
    token: str
    posts: tuple[DanbooruPost, ...]
    requested_count: int
    reason: str
    partial: bool
    scanned_candidates: int
    remote_rows: int
    error_code: str = ""


def select_candidates(
    posts: tuple[DanbooruPost, ...],
    criteria: CandidateCriteria,
    *,
    selected_keys: tuple[str, ...] = (),
) -> tuple[DanbooruPost, ...]:
    """Apply extra restrictions in gallery order, keeping the first row per ID.

    The 1-based start belongs to the original deduplicated gallery, before the
    additional restrictions. Manual selection never discovers unselected rows.
    """
    if not isinstance(criteria, CandidateCriteria):
        raise TypeError("criteria must be CandidateCriteria")
    if not isinstance(posts, tuple) or any(not isinstance(post, DanbooruPost) for post in posts):
        raise TypeError("posts must be a tuple of DanbooruPost values")
    by_id: dict[int, DanbooruPost] = {}
    for post in posts:
        by_id.setdefault(post.id, post)
    unique = tuple(by_id.values())
    if criteria.mode is CandidateSelectionMode.SELECTED:
        keys = frozenset(selected_keys)
        if not 1 <= len(keys) <= MAX_CANDIDATE_COUNT:
            raise ValueError("请勾选 1 至 100 张作品。")
        ordered = tuple(post for post in unique if str(post.id) in keys)
    else:
        ordered = unique[criteria.start - 1:] if criteria.mode is CandidateSelectionMode.FROM_POSITION else unique
    output = []
    for post in ordered:
        extension = post.file_ext.casefold()
        if criteria.file_categories is not None and not matches_file_category(extension, criteria.file_categories):
            continue
        if criteria.ratings is not None and post.rating not in criteria.ratings:
            continue
        if criteria.minimum_score is not None and (post.score is None or post.score < criteria.minimum_score):
            continue
        if criteria.minimum_fav_count is not None and (post.fav_count is None or post.fav_count < criteria.minimum_fav_count):
            continue
        output.append(post)
        if criteria.mode is not CandidateSelectionMode.SELECTED and len(output) >= criteria.count:
            break
    return tuple(output)


@dataclass(slots=True)
class _Preparation:
    source: CandidateSource
    criteria: CandidateCriteria
    token: str
    generation: int
    supplement_started: bool = False


class QueryCandidateService(QObject):
    """Keep immutable candidate batches separate from live gallery mutations.

    An active query already schedules its own bounded pages: observe that same
    execution. A stopped query may use the separately injected executor with its
    exact checkpoint. Neither path changes the query definition or scan limits.
    ``completed`` may be emitted synchronously from ``prepare`` or ``cancel``.
    """

    completed = Signal(object)

    def __init__(
        self,
        query: M2QueryCoordinator,
        supplement: DanbooruMultiBranchExecutor,
        *,
        is_closing: Callable[[], bool] = lambda: False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if supplement is query.executor:
            raise ValueError("候选补页必须使用独立的执行器实例。")
        self._query = query
        self._supplement = supplement
        self._is_closing = is_closing
        self._closed = False
        self._generation = 0
        self._preparation: _Preparation | None = None
        query.resultSummaryChanged.connect(self._query_result)
        query.viewDefinitionChanged.connect(self._check_source)
        query.executor.executionBound.connect(self._source_replaced)
        query.executor.snapshotChanged.connect(self._check_source)
        supplement.snapshotChanged.connect(self._supplement_result)

    def capture(
        self, *, selected_posts: tuple[DanbooruPost, ...] = (), model_revision: int = 0,
    ) -> CandidateSource:
        """Capture only the current operation's displayed, processed query rows."""
        query = self._query
        if self._closed or self._is_closing() or query.closed:
            raise ValueError("应用正在关闭，不能建立采集范围。")
        processed = query.last_processed
        identity = (query.operation_id, query.execution_id, query.view_generation)
        if (
            identity[0] is None or identity[1] is None or processed is None
            or query.gallery_operation_id != identity[0]
            or processed.source_snapshot.execution_id != identity[1]
        ):
            raise ValueError("当前画廊没有属于本次查询的结果。")
        if type(model_revision) is not int or model_revision < 0:
            raise ValueError("画廊版本无效。")
        if not isinstance(selected_posts, tuple) or any(not isinstance(post, DanbooruPost) for post in selected_posts):
            raise ValueError("勾选作品快照无效。")
        posts = processed.posts
        selected = {str(post.id) for post in selected_posts}
        if not selected.issubset(str(post.id) for post in posts):
            raise ValueError("勾选作品已经离开当前查询范围。")
        checkpoint = query.capture_checkpoint()
        source = CandidateSource(
            uuid4().hex, identity[0], identity[1], identity[2], model_revision,
            posts, tuple(str(post.id) for post in posts if str(post.id) in selected),
            checkpoint, processed.source_snapshot, processed.range_description,
        )
        if not self.source_current(source):
            raise ValueError("查询来源已经改变，请重新选择。")
        return source

    def source_current(self, source: CandidateSource) -> bool:
        """Normal page progress is allowed; another query or local view is not."""
        query = self._query
        return (
            not self._closed and not self._is_closing() and not query.closed
            and isinstance(source, CandidateSource)
            and (query.operation_id, query.execution_id, query.view_generation)
            == (source.operation_id, source.execution_id, source.view_generation)
            and query.gallery_operation_id == source.operation_id
            and query.definition == source.checkpoint.definition
            and query.checkpoint_source == source.checkpoint.source
        )

    def prepare(self, source: CandidateSource, criteria: CandidateCriteria, token: str) -> bool:
        """Freeze immediately when possible, otherwise scan within saved quotas."""
        if not isinstance(criteria, CandidateCriteria) or not isinstance(token, str) or not token:
            raise ValueError("候选请求参数无效。")
        if not self.source_current(source):
            return False
        posts = select_candidates(source.posts, criteria, selected_keys=source.selected_keys)
        self._generation += 1
        generation = self._generation
        previous = self._preparation
        if previous is not None:
            self._finish(previous, (), None, "已取消上一次候选准备。", "cancelled")
        if generation != self._generation or not self.source_current(source):
            return False
        preparation = _Preparation(source, criteria, token, generation)
        self._preparation = preparation
        requested = self._requested(preparation)
        if criteria.mode is CandidateSelectionMode.SELECTED or len(posts) >= requested:
            reason = (
                f"勾选了 {requested} 张作品，其中 {len(posts)} 张符合本次筛选条件。"
                if criteria.mode is CandidateSelectionMode.SELECTED and len(posts) < requested
                else "已按当前结果顺序固定候选。"
            )
            self._finish(preparation, posts, source.snapshot, reason)
            return True
        if not criteria.allow_more:
            self._finish(preparation, posts, source.snapshot, "仅使用打开窗口时已加载的结果，符合条件的作品不足。")
            return True
        processed = self._query.last_processed
        current = processed.source_snapshot if processed is not None else source.snapshot
        if processed is not None:
            posts = select_candidates(processed.posts, criteria, selected_keys=source.selected_keys)
            if len(posts) >= requested:
                self._finish(preparation, posts, current, "已在原查询扫描范围内取得指定候选。")
                return True
        if current is not None and current.state in ACTIVE_EXECUTION_STATES:
            return True
        checkpoint = self._query.capture_checkpoint()
        if checkpoint.execution.resume_reason:
            self._finish(preparation, posts, current, self._reason(current))
            return True
        preparation.supplement_started = True
        try:
            self._supplement.resume(checkpoint.execution)
        except (ValueError, RuntimeError) as exc:
            if self._current(preparation):
                self._finish(preparation, posts, current, str(exc), "preparation_failed")
        return True

    def cancel(self, token: str) -> bool:
        """Cancel only this preparation and its supplemental metadata request."""
        preparation = self._preparation
        if preparation is None or preparation.token != token or self._closed:
            return False
        self._generation += 1
        self._finish(preparation, (), None, "已取消候选准备。", "cancelled")
        return True

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        self._preparation = None
        self._supplement.shutdown()

    def _current(self, preparation: _Preparation) -> bool:
        return (
            self._preparation is preparation and preparation.generation == self._generation
            and self.source_current(preparation.source)
        )

    @staticmethod
    def _requested(preparation: _Preparation) -> int:
        if preparation.criteria.mode is CandidateSelectionMode.SELECTED:
            return len(preparation.source.selected_keys)
        return preparation.criteria.count

    def _source_replaced(self, _event: object) -> None:
        preparation = self._preparation
        if preparation is not None:
            self._generation += 1
            self._finish(preparation, (), None, "查询已经切换，请重新选择候选。", "source_changed")

    def _check_source(self, _event: object = None) -> None:
        preparation = self._preparation
        if preparation is not None and not self.source_current(preparation.source):
            self._generation += 1
            self._finish(preparation, (), None, "查询来源已经改变，候选准备已停止。", "source_changed")

    def _query_result(self, _event: object) -> None:
        preparation = self._preparation
        if preparation is None or preparation.supplement_started:
            return
        if not self._current(preparation):
            self._check_source()
            return
        processed = self._query.last_processed
        if processed is not None:
            if processed.source_snapshot.state is QueryExecutionState.PARTIAL_PAUSED and preparation.criteria.allow_more:
                selected = select_candidates(processed.posts, preparation.criteria,
                                             selected_keys=preparation.source.selected_keys)
                if len(selected) < self._requested(preparation):
                    preparation.supplement_started = True
                    try:
                        self._supplement.resume(self._query.capture_checkpoint().execution)
                    except (ValueError, RuntimeError) as exc:
                        if self._current(preparation):
                            self._finish(preparation, selected, processed.source_snapshot, str(exc), "preparation_failed")
                    return
            self._accept(preparation, processed.posts, processed.source_snapshot)

    def _supplement_result(self, snapshot: object) -> None:
        preparation = self._preparation
        if (
            preparation is None or not preparation.supplement_started
            or not isinstance(snapshot, QueryExecutionSnapshot)
            or snapshot is not self._supplement.snapshot
        ):
            return
        if not self._current(preparation):
            self._check_source()
            return
        checkpoint = preparation.source.checkpoint
        processed = process_query_snapshot(
            snapshot, build_query_result_session(snapshot.execution_id, checkpoint.execution.plan),
            checkpoint.runtime_filter, checkpoint.definition.sort_spec,
        )
        self._accept(preparation, processed.posts, snapshot)

    def _accept(
        self, preparation: _Preparation, posts: tuple[DanbooruPost, ...], snapshot: QueryExecutionSnapshot,
    ) -> None:
        selected = select_candidates(posts, preparation.criteria, selected_keys=preparation.source.selected_keys)
        if len(selected) >= self._requested(preparation):
            self._finish(preparation, selected, snapshot, "已在原查询扫描范围内取得指定候选。")
        elif snapshot.state not in ACTIVE_EXECUTION_STATES:
            self._finish(preparation, selected, snapshot, self._reason(snapshot))

    @staticmethod
    def _reason(snapshot: QueryExecutionSnapshot | None) -> str:
        if snapshot is None:
            return "当前已加载结果不足，没有可继续扫描的页面。"
        if snapshot.limit_reason is not None:
            return {
                QueryLimitReason.CANDIDATE_LIMIT: "已达到原查询的候选扫描上限，符合条件的作品不足。",
                QueryLimitReason.PAGE_LIMIT: "已达到原查询的分支页数上限，符合条件的作品不足。",
                QueryLimitReason.TIME_LIMIT: "已达到原查询的活动时间上限，符合条件的作品不足。",
            }[snapshot.limit_reason]
        if snapshot.state in {QueryExecutionState.FAILED, QueryExecutionState.PARTIAL_FAILED}:
            return "原查询页面请求失败，已保留能够确认的候选，未扩大查询范围。"
        if snapshot.state is QueryExecutionState.PARTIAL_CANCELLED:
            return "原查询已经取消，符合条件的作品不足。"
        return "原查询范围已经扫描完成，符合条件的作品不足。"

    def _finish(
        self, preparation: _Preparation, posts: tuple[DanbooruPost, ...],
        snapshot: QueryExecutionSnapshot | None, reason: str, error_code: str = "",
    ) -> None:
        if self._preparation is not preparation:
            return
        self._preparation = None
        snapshot = snapshot or preparation.source.snapshot
        result = CandidateResult(
            preparation.token, posts, self._requested(preparation), reason,
            snapshot is None or snapshot.state is not QueryExecutionState.COMPLETE,
            snapshot.counts.unique_candidates if snapshot is not None else 0,
            snapshot.counts.remote_rows if snapshot is not None else 0,
            error_code,
        )
        if preparation.supplement_started:
            self._supplement.cancel()
        if not self.source_current(preparation.source) and not error_code:
            result = CandidateResult(
                preparation.token, (), result.requested_count,
                "查询来源已经改变，候选准备已停止。", result.partial,
                result.scanned_candidates, result.remote_rows, "source_changed",
            )
        if not self._closed:
            self.completed.emit(result)
