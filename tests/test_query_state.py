from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QTimeZone, Signal
from PySide6.QtGui import QGuiApplication

from danbooru_multi_search.danbooru import (
    DanbooruM2QueryController,
    DanbooruPost,
    DanbooruPostListModel,
    GalleryQueryAccepted,
    GalleryQueryDefinition,
    GalleryQueryOrigin,
    GalleryResultEvent,
    GalleryResultSummary,
    GalleryViewDefinitionChanged,
    PostFileCategory,
    PostOrientation,
    QueryBranchPosition,
    QueryExecutionCounts,
    QueryExecutionAccepted,
    QueryExecutionBound,
    QueryExecutionErrorCode,
    QueryExecutionFailure,
    QueryExecutionLimits,
    QueryExecutionSnapshot,
    QueryExecutionState,
    QueryLimitReason,
    QueryMode,
    QueryPlanError,
    QueryRating,
    ResultFilterSpec,
    ResultSortDirection,
    ResultSortField,
    ResultSortSpec,
    TagTranslationStore,
    parse_query_expression,
)
from danbooru_multi_search.ui.query_editor import QueryTreeEditor
from danbooru_multi_search.ui.query_state import M2QueryBridgeController
from danbooru_multi_search.ui.query_forms import (
    QueryInputError,
    build_draft_definition,
    default_query_form_values,
    parse_query_form,
    parse_windows_local_datetime,
)


APPLICATION = QGuiApplication.instance() or QGuiApplication([])


def test_original_form_imports_keep_the_same_objects():
    from danbooru_multi_search.ui import query_forms, query_state

    for name in (
        "QueryFormState", "QueryInputDiagnostic", "QueryInputError",
        "build_draft_definition", "default_query_form_values", "parse_query_form",
        "parse_windows_local_datetime", "query_form_values_from_definition",
    ):
        assert getattr(query_state, name) is getattr(query_forms, name)


@dataclass(frozen=True, slots=True)
class _Processed:
    source_snapshot: QueryExecutionSnapshot
    visible_count: int
    range_description: str


class _FakeExecutor(QObject):
    snapshotChanged = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.snapshot = _snapshot(0, QueryExecutionState.IDLE)

    def publish(self, snapshot: QueryExecutionSnapshot) -> None:
        self.snapshot = snapshot
        self.snapshotChanged.emit(snapshot)


class _SignalOrderExecutor(QObject):
    """Small executor double preserving the real controller signal order."""

    executionBound = Signal(object)
    firstPageAccepted = Signal(object)
    snapshotChanged = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.snapshot = _snapshot(0, QueryExecutionState.IDLE)
        self.current_execution_id: int | None = None
        self.closed = False
        self._active = False

    def start(self, plan, limits=None, *, binding_token=None) -> int:
        del plan, limits
        self.current_execution_id = (self.current_execution_id or 0) + 1
        execution_id = self.current_execution_id
        self._active = True
        self.snapshot = _snapshot(execution_id, QueryExecutionState.WAITING)
        self.executionBound.emit(QueryExecutionBound(execution_id, binding_token))
        self.snapshotChanged.emit(self.snapshot)
        return execution_id

    def is_execution_active(self, execution_id: int) -> bool:
        return self._active and self.current_execution_id == execution_id

    def complete(self, post: DanbooruPost) -> None:
        assert self.current_execution_id is not None
        execution_id = self.current_execution_id
        self.firstPageAccepted.emit(
            QueryExecutionAccepted(
                execution_id,
                QueryBranchPosition(0, 1),
                "task-1",
            )
        )
        self._active = False
        self.snapshot = QueryExecutionSnapshot(
            execution_id,
            QueryExecutionState.COMPLETE,
            QueryExecutionCounts(1, 1, 1),
            (post,),
            (),
        )
        self.snapshotChanged.emit(self.snapshot)

    def cancel(self) -> bool:
        return False

    def retry_failed_page(self) -> bool:
        return False

    def shutdown(self) -> None:
        self.closed = True
        self._active = False


class _FakeM2Controller(QObject):
    queryAccepted = Signal(object)
    viewDefinitionChanged = Signal(object)
    resultSummaryChanged = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.executor = _FakeExecutor()
        self.posts_model = DanbooruPostListModel()
        self.definition: GalleryQueryDefinition | None = None
        self.operation_id: int | None = None
        self.execution_id: int | None = None
        self.view_generation = 0
        self.gallery_operation_id: int | None = None
        self.last_processed: _Processed | None = None
        self.start_calls: list[tuple[str, object]] = []
        self.update_view_calls = 0
        self.retry_calls = 0
        self.cancel_calls = 0
        self.synchronous_accept = False

    def start_standard(self, expression: str, **values) -> int:
        definition = _definition(QueryMode.STANDARD, expression, values)
        return self._start("standard", definition)

    def start_rank(self, expression: str | None, **values) -> int:
        definition = _definition(QueryMode.RANK, expression, values)
        return self._start("rank", definition)

    def start_latest(self, **values) -> int:
        definition = _definition(QueryMode.LATEST, None, values)
        return self._start("latest", definition)

    def _start(self, kind: str, definition: GalleryQueryDefinition) -> int:
        self.operation_id = (self.operation_id or 0) + 1
        self.execution_id = (self.execution_id or 0) + 1
        self.definition = definition
        self.view_generation = 0
        self.start_calls.append((kind, definition))
        self.executor.publish(
            _snapshot(self.execution_id, QueryExecutionState.WAITING)
        )
        if self.synchronous_accept:
            self.accept_current()
        return self.operation_id

    def accept_current(self) -> None:
        assert self.operation_id is not None
        assert self.execution_id is not None
        assert self.definition is not None
        self.queryAccepted.emit(
            GalleryQueryAccepted(
                self.operation_id,
                self.execution_id,
                self.definition,
                self.definition.normalized_expression or "无标签查询",
                GalleryQueryOrigin(self.definition.mode.value),
                (),
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                False,
            )
        )

    def update_view(self, *, filter_spec, sort_spec) -> bool:
        assert self.definition is not None
        assert self.operation_id is not None
        assert self.execution_id is not None
        self.update_view_calls += 1
        self.view_generation += 1
        self.definition = replace(
            self.definition,
            filter_spec=filter_spec,
            sort_spec=sort_spec,
        )
        self.viewDefinitionChanged.emit(
            GalleryViewDefinitionChanged(
                self.operation_id,
                self.execution_id,
                self.view_generation,
                self.definition,
                self.gallery_operation_id == self.operation_id,
            )
        )
        return True

    def refresh(self) -> int:
        assert self.definition is not None
        definition = self.definition
        return self._start("refresh", definition)

    def retry_failed_page(self) -> bool:
        self.retry_calls += 1
        assert self.execution_id is not None
        self.executor.publish(
            _snapshot(self.execution_id, QueryExecutionState.RUNNING)
        )
        return True

    def cancel(self) -> bool:
        self.cancel_calls += 1
        assert self.execution_id is not None
        self.executor.publish(
            _snapshot(self.execution_id, QueryExecutionState.PARTIAL_CANCELLED)
        )
        return True

    def publish_result(
        self,
        snapshot: QueryExecutionSnapshot,
        *,
        visible_count: int,
        range_description: str,
        commit_gallery: bool,
    ) -> None:
        assert self.operation_id is not None
        assert self.execution_id is not None
        assert self.definition is not None
        self.executor.publish(snapshot)
        self.last_processed = _Processed(
            snapshot,
            visible_count,
            range_description,
        )
        if commit_gallery:
            self.gallery_operation_id = self.operation_id
        self.resultSummaryChanged.emit(
            GalleryResultEvent(
                self.operation_id,
                self.execution_id,
                self.view_generation,
                self.definition,
                GalleryResultSummary(
                    snapshot.state,
                    snapshot.counts.remote_rows,
                    snapshot.counts.unique_candidates,
                    snapshot.counts.local_matches,
                    visible_count,
                    snapshot.limit_reason,
                    (
                        snapshot.failure.error_code
                        if snapshot.failure is not None
                        else None
                    ),
                ),
                self.gallery_operation_id,
            )
        )


def _definition(mode, expression, values) -> GalleryQueryDefinition:
    normalized_expression = expression
    if expression is not None:
        normalized_expression = parse_query_expression(expression).normalized_text
    filter_spec = values.get("filter_spec")
    sort_spec = values.get("sort_spec")
    limits = values.get("limits")
    return GalleryQueryDefinition(
        mode,
        normalized_expression,
        QueryRating(values.get("rating", "all")),
        ResultFilterSpec() if filter_spec is None else filter_spec,
        ResultSortSpec() if sort_spec is None else sort_spec,
        QueryExecutionLimits() if limits is None else limits,
    )


def _snapshot(
    execution_id: int,
    state: QueryExecutionState,
    *,
    counts: QueryExecutionCounts | None = None,
    failure: QueryExecutionFailure | None = None,
    limit_reason: QueryLimitReason | None = None,
) -> QueryExecutionSnapshot:
    return QueryExecutionSnapshot(
        execution_id,
        state,
        counts or QueryExecutionCounts(),
        (),
        (),
        limit_reason,
        failure,
    )


def _bridge(expression: str = "cat AND dog"):
    core = _FakeM2Controller()
    bridge, editor = _make_bridge(core, expression)
    return bridge, core, editor


def _make_bridge(core: _FakeM2Controller, expression: str = "cat AND dog"):
    editor = QueryTreeEditor(expression)
    bridge = M2QueryBridgeController(
        core,  # type: ignore[arg-type]
        query_editor=editor,
        time_zone=QTimeZone(b"Asia/Shanghai"),
    )
    return bridge, editor


def _complete_latest(
    core: _FakeM2Controller,
    *,
    visible_count: int = 3,
    range_description: str = "最新查询范围",
) -> None:
    core.start_latest()
    core.accept_current()
    assert core.execution_id is not None
    core.publish_result(
        _snapshot(core.execution_id, QueryExecutionState.COMPLETE),
        visible_count=visible_count,
        range_description=range_description,
        commit_gallery=True,
    )


def test_parse_query_form_converts_all_supported_values_strictly():
    form = default_query_form_values()
    form.update(
        {
            "rating": "sensitive",
            "minimumScore": "-5",
            "minimumFavCount": "3",
            "publishedFrom": "2026-01-02 08:30",
            "publishedUntil": "2026-01-03 08:30",
            "minimumAgeDays": "2",
            "maximumAgeDays": "9",
            "fileCategories": ["static", "webm"],
            "minimumWidth": "640",
            "minimumHeight": "480",
            "minimumAspectRatio": "4:3",
            "maximumAspectRatio": "16:9",
            "orientations": ["landscape", "square"],
            "blockedTags": "bad_tag, other_tag\nbad_tag",
            "sortField": "score",
            "sortDirection": "descending",
            "pageSize": "100",
            "candidateLimit": "500",
            "branchPageLimit": "4",
            "activeTimeLimitSeconds": "12.5",
        }
    )

    parsed = parse_query_form(
        form,
        mode=QueryMode.STANDARD,
        time_zone=QTimeZone(b"Asia/Shanghai"),
    )

    assert parsed.rating == QueryRating.SENSITIVE
    assert parsed.filter_spec.minimum_score == -5
    assert parsed.filter_spec.minimum_fav_count == 3
    assert parsed.filter_spec.published_from_utc == datetime(
        2026, 1, 2, 0, 30, tzinfo=timezone.utc
    )
    assert parsed.filter_spec.minimum_age == timedelta(days=2)
    assert parsed.filter_spec.maximum_age == timedelta(days=9)
    assert parsed.filter_spec.age_reference_time is None
    assert parsed.filter_spec.file_categories == frozenset(
        {PostFileCategory.STATIC, PostFileCategory.WEBM}
    )
    assert parsed.filter_spec.minimum_aspect_ratio == Fraction(4, 3)
    assert parsed.filter_spec.maximum_aspect_ratio == Fraction(16, 9)
    assert parsed.filter_spec.orientations == frozenset(
        {PostOrientation.LANDSCAPE, PostOrientation.SQUARE}
    )
    assert parsed.filter_spec.blocked_tags == frozenset(
        {"bad_tag", "other_tag"}
    )
    assert parsed.sort_spec == ResultSortSpec(
        ResultSortField.SCORE,
        ResultSortDirection.DESCENDING,
    )
    assert parsed.limits == QueryExecutionLimits(100, 500, 4, 12.5)


def test_draft_form_values_is_a_detached_qml_snapshot():
    bridge, _core, _editor = _bridge()

    first = bridge.draftFormValues
    first["rating"] = "explicit"
    first["fileCategories"].append("gif")

    assert bridge.draftFormValues["rating"] == "all"
    assert bridge.draftFormValues["fileCategories"] == []

    updated = default_query_form_values()
    updated["minimumScore"] = "12"
    assert bridge.updateDraftForm(updated) is True
    assert bridge.draftFormValues["minimumScore"] == "12"


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"unexpected": "value"}, "unknown_form_field"),
        ({"minimumScore": False}, "invalid_text"),
        ({"minimumFavCount": "-1"}, "invalid_integer_range"),
        ({"minimumWidth": "0"}, "invalid_integer_range"),
        ({"minimumAspectRatio": "1.5"}, "invalid_aspect_ratio"),
        ({"minimumAspectRatio": "1:0"}, "invalid_aspect_ratio"),
        (
            {"minimumAspectRatio": "16:9", "maximumAspectRatio": "4:3"},
            "invalid_aspect_ratio_range",
        ),
        ({"candidateLimit": "1001"}, "invalid_execution_limits"),
        ({"activeTimeLimitSeconds": "NaN"}, "invalid_decimal"),
        ({"minimumAgeDays": "999999999999999999999"}, "invalid_age_range"),
    ],
)
def test_parse_query_form_rejects_invalid_values_with_stable_codes(updates, code):
    form = default_query_form_values()
    form.update(updates)

    with pytest.raises(QueryInputError) as captured:
        parse_query_form(
            form,
            mode=QueryMode.STANDARD,
            time_zone=QTimeZone.utc(),
        )

    assert captured.value.code == code


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("minimumScore", "9" * 5000, "invalid_integer"),
        ("minimumAspectRatio", f"1:{'9' * 5000}", "invalid_aspect_ratio"),
    ],
)
def test_huge_numbers_keep_field_level_diagnostics(field, value, code):
    form = default_query_form_values()
    form[field] = value

    with pytest.raises(QueryInputError) as captured:
        parse_query_form(form, mode=QueryMode.STANDARD, time_zone=QTimeZone.utc())

    assert captured.value.code == code
    assert captured.value.diagnostic.field == field


def test_parse_query_form_enforces_sort_mode_and_empty_selections_mean_none():
    form = default_query_form_values()
    form["sortField"] = "rank"

    with pytest.raises(QueryInputError) as captured:
        parse_query_form(form, mode=QueryMode.STANDARD, time_zone=QTimeZone.utc())
    assert captured.value.code == "rank_unavailable"

    parsed = parse_query_form(form, mode=QueryMode.RANK, time_zone=QTimeZone.utc())
    assert parsed.filter_spec.file_categories is None
    assert parsed.filter_spec.orientations is None
    assert parsed.sort_spec.field == ResultSortField.RANK


def test_rank_button_reparses_raw_form_for_its_target_mode():
    bridge, core, _ = _bridge()
    form = default_query_form_values()
    form["sortField"] = "rank"

    assert bridge.updateDraftForm(form) is False
    assert bridge.errorCode == "rank_unavailable"
    assert bridge.canExecuteStandard is False
    assert bridge.canExecuteGlobalRank is True
    assert bridge.executeRank(False) is True

    assert core.definition is not None
    assert core.definition.mode == QueryMode.RANK
    assert core.definition.sort_spec.field == ResultSortField.RANK


def test_windows_local_time_rejects_gap_and_fold_with_injected_timezone():
    zone = QTimeZone(b"America/New_York")
    assert zone.isValid()

    with pytest.raises(QueryInputError) as gap:
        parse_windows_local_datetime("2026-03-08 02:30", zone)
    with pytest.raises(QueryInputError) as fold:
        parse_windows_local_datetime("2026-11-01 01:30", zone)

    assert gap.value.code == "nonexistent_local_datetime"
    assert fold.value.code == "ambiguous_local_datetime"


def test_build_draft_definition_uses_existing_parser_and_plan_limits():
    form = parse_query_form(
        default_query_form_values(),
        mode=QueryMode.STANDARD,
        time_zone=QTimeZone.utc(),
    )

    definition = build_draft_definition(
        form,
        mode=QueryMode.STANDARD,
        normalized_expression="Cat dog",
    )
    assert definition.normalized_expression == "cat AND dog"

    expression = " OR ".join(f"tag_{index}" for index in range(17))
    with pytest.raises(QueryPlanError) as captured:
        build_draft_definition(
            form,
            mode=QueryMode.STANDARD,
            normalized_expression=expression,
        )
    assert getattr(captured.value, "code", None) == "branch_limit"


def test_bridge_executes_three_modes_and_keeps_expression_for_latest():
    bridge, core, editor = _bridge()

    assert bridge.executeStandard() is True
    assert core.start_calls[-1][0] == "standard"
    assert core.definition is not None
    assert core.definition.normalized_expression == "cat AND dog"

    assert bridge.executeRank(False) is True
    assert core.start_calls[-1][0] == "rank"
    assert core.definition is not None
    assert core.definition.normalized_expression is None

    assert bridge.executeRank(True) is True
    assert core.definition is not None
    assert core.definition.normalized_expression == "cat AND dog"

    assert bridge.executeLatest() is True
    assert core.start_calls[-1][0] == "latest"
    assert core.definition is not None
    assert core.definition.normalized_expression is None
    assert editor.canonical_text == "cat AND dog"


@pytest.mark.parametrize(
    ("mode", "execute"),
    [
        ("rank", lambda bridge: bridge.executeRank(False)),
        ("latest", lambda bridge: bridge.executeLatest()),
    ],
)
def test_global_modes_ignore_expression_draft_differences(mode, execute):
    bridge, core, _ = _bridge()
    core.synchronous_accept = True

    assert execute(bridge) is True
    assert bridge.draftMode == mode
    assert bridge.applyAdvancedText("wolf OR") is False

    assert bridge.executionConditionsChanged is False
    assert bridge.viewConditionsChanged is False
    assert bridge.canRefresh is True


def test_invalid_advanced_text_blocks_expression_modes_but_not_global_modes():
    bridge, core, editor = _bridge()

    assert bridge.applyAdvancedText("cat OR") is False
    assert editor.canonical_text == "cat AND dog"
    assert bridge.errorCode == "missing_operand"
    assert (bridge.errorStart, bridge.errorEnd) == (6, 6)
    assert bridge.canExecuteStandard is False
    assert bridge.executeStandard() is False
    assert core.start_calls == []

    assert bridge.executeRank(False) is True
    assert bridge.executeLatest() is True
    assert len(core.start_calls) == 2
    assert bridge.discardInvalidText() is True
    assert bridge.advancedText == "cat AND dog"


def test_conditional_rank_plan_error_reports_whole_expression_range():
    bridge, core, _ = _bridge("cat OR dog")

    assert bridge.setDraftMode("rank", True) is True
    assert bridge.canExecuteRank is False
    assert bridge.canExecuteGlobalRank is True
    assert bridge.canExecuteConditionalRank is False
    assert bridge.errorCode == "rank_requires_common_tag"
    assert (bridge.errorStart, bridge.errorEnd) == (0, len("cat OR dog"))
    assert bridge.executeRank(True) is False

    assert core.start_calls == []
    assert bridge.errorCode == "rank_requires_common_tag"
    assert (bridge.errorStart, bridge.errorEnd) == (0, len("cat OR dog"))


def test_visual_tree_commands_refresh_paths_and_enforce_artist_category():
    from danbooru_multi_search.core.artist_combinations import ArtistCombinationMode
    from danbooru_multi_search.ui import query_state

    assert query_state.ArtistCombinationMode is ArtistCombinationMode
    bridge, _, _ = _bridge("base")

    assert bridge.addVisualTag(None, "cat") is True
    assert bridge.canonicalExpression == "base AND cat"
    assert [row["path"] for row in bridge.queryTreeRows] == [[], [0], [1]]
    assert bridge.moveVisualNode([1], -1) is True
    assert bridge.canonicalExpression == "cat AND base"
    assert bridge.insertArtistGroup(
        None,
        [{"name": "artist_a", "category": 1}],
        "all",
    ) is True
    assert bridge.canonicalExpression == "cat AND base AND artist_a"

    assert bridge.insertArtistGroup(
        None,
        [{"name": "unknown_tag", "category": None}],
        "any",
    ) is False
    assert bridge.errorCode == "artist_category_required"


def test_visual_tree_slots_convert_invalid_qml_values_to_stable_errors():
    bridge, _, _ = _bridge("base")

    assert bridge.removeVisualNode([True]) is False
    assert bridge.errorCode == "invalid_tree_path"
    assert bridge.wrapVisualGroup([0], "xor", "cat", True) is False
    assert bridge.errorCode == "invalid_group_operator"
    assert bridge.wrapVisualGroup([], "or", "rating:g", True) is False
    assert bridge.errorCode == "invalid_tag"


def test_bridge_distinguishes_execution_and_view_drafts_without_network():
    bridge, core, _ = _bridge()
    core.synchronous_accept = True
    preparing: list[tuple[int, int, int]] = []
    bridge.viewUpdatePreparing.connect(
        lambda operation_id, execution_id, view_generation: preparing.append(
            (operation_id, execution_id, view_generation)
        )
    )

    assert bridge.executeStandard() is True
    assert bridge.executionConditionsChanged is False
    assert bridge.viewConditionsChanged is False
    assert bridge.canRefresh is True

    form = bridge.draftFormValues
    form["minimumScore"] = "10"
    assert bridge.updateDraftForm(form) is True
    assert bridge.executionConditionsChanged is False
    assert bridge.viewConditionsChanged is True
    assert bridge.canRefresh is False
    assert bridge.canApplyView is True

    start_count = len(core.start_calls)
    assert bridge.applyView() is True
    assert preparing == [(core.operation_id, core.execution_id, 0)]
    assert len(core.start_calls) == start_count
    assert core.update_view_calls == 1
    assert core.definition is not None
    assert core.definition.filter_spec.minimum_score == 10
    assert bridge.viewConditionsChanged is False
    assert bridge.canRefresh is True

    assert bridge.applyAdvancedText("wolf") is True
    assert bridge.executionConditionsChanged is True
    assert bridge.canApplyView is False
    assert bridge.canRefresh is False


def test_bridge_exposes_unaccepted_queue_failure_and_exact_retry():
    bridge, core, _ = _bridge()
    assert bridge.executeStandard() is True
    assert core.execution_id is not None
    failure = QueryExecutionFailure(
        QueryBranchPosition(2, 4),
        "cat dog",
        QueryExecutionErrorCode.QUEUE_REJECTED,
        None,
        "full",
    )
    core.executor.publish(
        _snapshot(
            core.execution_id,
            QueryExecutionState.FAILED,
            failure=failure,
        )
    )

    assert bridge.errorCode == "queue_rejected"
    assert bridge.failureErrorCode == "queue_rejected"
    assert bridge.errorText == "任务队列暂时无法接受请求"
    assert bridge.failureErrorText == "任务队列暂时无法接受请求"
    assert (bridge.failureBranch, bridge.failurePage) == (2, 4)
    assert bridge.canRetry is True
    assert bridge.retryFailedPage() is True
    assert core.retry_calls == 1
    assert bridge.queryState == "running"


def test_bridge_counts_limits_old_gallery_and_cancel_state():
    bridge, core, _ = _bridge()
    assert bridge.executeStandard() is True
    assert core.execution_id is not None
    snapshot = _snapshot(
        core.execution_id,
        QueryExecutionState.PARTIAL_LIMITED,
        counts=QueryExecutionCounts(80, 64, 20),
        limit_reason=QueryLimitReason.CANDIDATE_LIMIT,
    )
    core.publish_result(
        snapshot,
        visible_count=12,
        range_description="按已扫描的 64 个唯一候选排序",
        commit_gallery=True,
    )

    assert (bridge.remoteRows, bridge.uniqueCandidates) == (80, 64)
    assert (bridge.localMatches, bridge.visibleCount) == (20, 12)
    assert bridge.statusText == "已经达到候选数量上限"
    assert bridge.rangeDescription == "按已扫描的 64 个唯一候选排序"
    assert bridge.galleryMode == "standard"

    assert bridge.executeLatest() is True
    assert bridge.oldGalleryVisible is True
    assert bridge.canCancel is True
    assert bridge.cancel() is True
    assert bridge.queryState == "partial_cancelled"


def test_new_operation_state_signal_never_pairs_new_identity_with_old_visible_count():
    bridge, core, _ = _bridge()
    assert bridge.executeStandard() is True
    assert core.execution_id is not None
    core.publish_result(
        _snapshot(core.execution_id, QueryExecutionState.COMPLETE),
        visible_count=12,
        range_description="已完成",
        commit_gallery=True,
    )
    observed: list[tuple[int | None, int]] = []
    bridge.countsChanged.connect(
        lambda: observed.append((core.operation_id, bridge.visibleCount))
    )

    assert bridge.executeLatest() is True

    assert (2, 0) in observed
    assert all(visible == 0 for operation, visible in observed if operation == 2)


def test_real_m2_controller_result_commits_snapshot_and_visible_count_atomically():
    executor = _SignalOrderExecutor()
    core = DanbooruM2QueryController(
        "https://example.invalid",
        executor=executor,  # type: ignore[arg-type]
        tag_store=TagTranslationStore(),
        posts_model=DanbooruPostListModel(),
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    bridge = M2QueryBridgeController(
        core,
        query_editor=QueryTreeEditor("cat"),
        time_zone=QTimeZone.utc(),
    )
    observed: list[tuple[str, int, int, int, int]] = []
    bridge.countsChanged.connect(
        lambda: observed.append(
            (
                bridge.queryState,
                bridge.remoteRows,
                bridge.uniqueCandidates,
                bridge.localMatches,
                bridge.visibleCount,
            )
        )
    )
    assert bridge.executeStandard() is True
    observed.clear()

    executor.complete(
        DanbooruPost(
            id=1,
            preview_url="https://example.invalid/preview.jpg",
            large_file_url=None,
            file_url="https://example.invalid/file.jpg",
            score=1,
            fav_count=1,
            rating="g",
            width=100,
            height=100,
            file_ext="jpg",
            created_at="2026-01-01T00:00:00.000000Z",
            general_tags=("cat",),
        )
    )

    completed = ("complete", 1, 1, 1, 1)
    assert completed in observed
    assert observed.count(completed) == 1
    assert not any(
        visible == 1 and state != "complete"
        for state, _, _, _, visible in observed
    )
    assert (bridge.queryState, bridge.remoteRows, bridge.visibleCount) == (
        "complete",
        1,
        1,
    )
    core.shutdown()


def test_synchronous_acceptance_is_not_erased_after_start_returns():
    bridge, core, _ = _bridge()
    core.synchronous_accept = True

    assert bridge.executeStandard() is True
    assert bridge.canRefresh is True
    assert bridge.refresh() is True
    assert bridge.canRefresh is True


def test_stale_acceptance_after_synchronous_new_query_cannot_replace_identity():
    core = _FakeM2Controller()

    def replace_first_acceptance(value):
        if isinstance(value, GalleryQueryAccepted) and value.operation_id == 1:
            _complete_latest(core)

    core.queryAccepted.connect(replace_first_acceptance)
    bridge, _ = _make_bridge(core)
    core.start_standard("cat")
    core.accept_current()

    assert bridge.executionMode == "latest"
    assert bridge.galleryMode == "latest"
    assert bridge.setDraftMode("latest", False) is True
    assert bridge.canRefresh is True
    assert 1 not in bridge._definition_by_operation


def test_stale_view_event_cannot_overwrite_synchronously_completed_gallery():
    core = _FakeM2Controller()

    def replace_first_view(value):
        if isinstance(value, GalleryViewDefinitionChanged) and value.operation_id == 1:
            _complete_latest(core, range_description="新画廊")

    core.viewDefinitionChanged.connect(replace_first_view)
    bridge, _ = _make_bridge(core)
    assert bridge.executeStandard() is True
    core.accept_current()
    assert core.execution_id is not None
    core.publish_result(
        _snapshot(core.execution_id, QueryExecutionState.COMPLETE),
        visible_count=5,
        range_description="旧画廊",
        commit_gallery=True,
    )

    assert core.update_view(
        filter_spec=ResultFilterSpec(minimum_score=1),
        sort_spec=ResultSortSpec(),
    ) is True

    assert bridge.executionMode == "latest"
    assert bridge.galleryMode == "latest"
    assert bridge.rangeDescription == "新画廊"


def test_stale_result_event_cannot_overwrite_synchronously_completed_result():
    core = _FakeM2Controller()

    def replace_first_result(value):
        if isinstance(value, GalleryResultEvent) and value.operation_id == 1:
            _complete_latest(
                core,
                visible_count=7,
                range_description="新查询范围",
            )

    core.resultSummaryChanged.connect(replace_first_result)
    bridge, _ = _make_bridge(core)
    assert bridge.executeStandard() is True
    core.accept_current()
    assert core.execution_id is not None
    core.publish_result(
        _snapshot(
            core.execution_id,
            QueryExecutionState.COMPLETE,
            counts=QueryExecutionCounts(20, 18, 12),
        ),
        visible_count=4,
        range_description="旧查询范围",
        commit_gallery=True,
    )

    assert bridge.executionMode == "latest"
    assert bridge.galleryMode == "latest"
    assert bridge.visibleCount == 7
    assert bridge.rangeDescription == "新查询范围"


def test_begin_closing_rejects_all_new_commands_without_touching_core():
    bridge, core, _ = _bridge()
    bridge.beginClosing()

    assert bridge.executeStandard() is False
    assert bridge.executeRank(False) is False
    assert bridge.executeLatest() is False
    assert bridge.updateDraftForm(default_query_form_values()) is False
    assert core.start_calls == []
