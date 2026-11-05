"""QML-facing state and strict form conversion for M2 queries.

The bridge keeps raw interface concerns out of the query controller.  It turns
QML values into the immutable domain types, mirrors execution state, and
guards every command so Python exceptions never cross the QML boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import math
import re

from PySide6.QtCore import (
    QDate,
    QDateTime,
    QObject,
    Property,
    QTime,
    QTimeZone,
    Signal,
    Slot,
)

from danbooru_multi_search.danbooru.query_controller import (
    DanbooruM2QueryController,
    GalleryQueryAccepted,
    GalleryResultEvent,
    GalleryViewDefinitionChanged,
)
from danbooru_multi_search.danbooru.query_definition import (
    GalleryQueryDefinition,
    QueryMode,
)
from danbooru_multi_search.danbooru.query_execution import (
    ACTIVE_EXECUTION_STATES,
    DEFAULT_ACTIVE_TIME_LIMIT_SECONDS,
    DEFAULT_BRANCH_PAGE_LIMIT,
    DEFAULT_CANDIDATE_LIMIT,
    DEFAULT_EXECUTION_PAGE_SIZE,
    QueryExecutionErrorCode,
    QueryExecutionLimits,
    QueryExecutionSnapshot,
    QueryExecutionState,
    QueryLimitReason,
)
from danbooru_multi_search.danbooru.query_language import (
    AndNode,
    NotNode,
    OrNode,
    QueryNode,
    QueryParseError,
    SourceSpan,
    TagNode,
    parse_query_expression,
)
from danbooru_multi_search.danbooru.query_planning import (
    QueryPlanError,
    QueryRating,
    RemoteOrder,
    build_latest_query_plan,
    build_rank_query_plan,
    build_standard_query_plan,
    normalize_plan_rating,
)
from danbooru_multi_search.danbooru.result_processing import (
    PostFileCategory,
    PostOrientation,
    ProcessedQueryResults,
    ResultFilterSpec,
    ResultProcessingError,
    ResultSortDirection,
    ResultSortField,
    ResultSortSpec,
    validate_result_configuration,
)
from danbooru_multi_search.danbooru.tag_syntax import (
    TagSyntaxError,
    normalize_tag_name,
)
from danbooru_multi_search.ui.query_editor import (
    ArtistCombinationMode,
    ConfirmedArtistTag,
    QueryEditorError,
    QueryGroupOperator,
    QueryTreeEditor,
)


_LOCAL_DATETIME_PATTERN = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2}) "
    r"(?P<hour>\d{2}):(?P<minute>\d{2})$"
)
_INTEGER_PATTERN = re.compile(r"^[+-]?\d+$")
_POSITIVE_RATIO_PATTERN = re.compile(r"^(?P<numerator>\d+):(?P<denominator>\d+)$")
_POSITIVE_DECIMAL_PATTERN = re.compile(r"^(?:\d+(?:\.\d*)?|\.\d+)$")
_MAX_INTEGER_DIGITS = 64


_FORM_FIELDS = frozenset(
    {
        "rating",
        "minimumScore",
        "minimumFavCount",
        "publishedFrom",
        "publishedUntil",
        "minimumAgeDays",
        "maximumAgeDays",
        "fileCategories",
        "minimumWidth",
        "minimumHeight",
        "minimumAspectRatio",
        "maximumAspectRatio",
        "orientations",
        "blockedTags",
        "sortField",
        "sortDirection",
        "pageSize",
        "candidateLimit",
        "branchPageLimit",
        "activeTimeLimitSeconds",
    }
)


@dataclass(frozen=True, slots=True)
class QueryInputDiagnostic:
    """One stable bridge input error with an optional editor range."""

    code: str
    message: str
    field: str
    span: SourceSpan | None = None


class QueryInputError(ValueError):
    """Raised when a QML value cannot form an immutable domain value."""

    def __init__(self, diagnostic: QueryInputDiagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic

    @property
    def code(self) -> str:
        return self.diagnostic.code


@dataclass(frozen=True, slots=True)
class QueryFormState:
    """Validated non-expression values shared by all three query modes."""

    rating: QueryRating
    filter_spec: ResultFilterSpec
    sort_spec: ResultSortSpec
    limits: QueryExecutionLimits


def default_query_form_values() -> dict[str, object]:
    """Return a fresh QML-shaped form using the domain defaults."""

    return {
        "rating": QueryRating.ALL.value,
        "minimumScore": "",
        "minimumFavCount": "",
        "publishedFrom": "",
        "publishedUntil": "",
        "minimumAgeDays": "",
        "maximumAgeDays": "",
        "fileCategories": [],
        "minimumWidth": "",
        "minimumHeight": "",
        "minimumAspectRatio": "",
        "maximumAspectRatio": "",
        "orientations": [],
        "blockedTags": "",
        "sortField": ResultSortField.SOURCE_ORDER.value,
        "sortDirection": ResultSortDirection.ORIGINAL.value,
        "pageSize": str(DEFAULT_EXECUTION_PAGE_SIZE),
        "candidateLimit": str(DEFAULT_CANDIDATE_LIMIT),
        "branchPageLimit": str(DEFAULT_BRANCH_PAGE_LIMIT),
        "activeTimeLimitSeconds": f"{DEFAULT_ACTIVE_TIME_LIMIT_SECONDS:g}",
    }


def parse_query_form(
    values: Mapping[str, object],
    *,
    mode: QueryMode,
    time_zone: QTimeZone | None = None,
) -> QueryFormState:
    """Strictly convert one QML form without using truth-value fallbacks."""

    if not isinstance(values, Mapping):
        _input_fail("invalid_form", "查询表单必须是键值映射。", "form")
    if not isinstance(mode, QueryMode):
        _input_fail("invalid_mode", "查询模式无效。", "mode")
    if any(not isinstance(key, str) for key in values):
        _input_fail(
            "unknown_form_field",
            "查询表单字段名称必须是文本。",
            "form",
        )
    unknown = sorted(set(values) - _FORM_FIELDS)
    if unknown:
        _input_fail(
            "unknown_form_field",
            f"查询表单包含未知字段：{unknown[0]}。",
            str(unknown[0]),
        )

    source = default_query_form_values()
    source.update(dict(values))
    zone = QTimeZone.systemTimeZone() if time_zone is None else time_zone
    if not isinstance(zone, QTimeZone) or not zone.isValid():
        _input_fail("invalid_time_zone", "Windows 当前时区无效。", "timeZone")

    try:
        rating = normalize_plan_rating(_required_text(source["rating"], "rating"))
    except QueryPlanError as exc:
        _input_fail(exc.code, str(exc), "rating")

    minimum_age_days = _optional_integer_text(
        source["minimumAgeDays"],
        "minimumAgeDays",
        minimum=0,
    )
    maximum_age_days = _optional_integer_text(
        source["maximumAgeDays"],
        "maximumAgeDays",
        minimum=0,
    )
    if (
        minimum_age_days is not None
        and maximum_age_days is not None
        and minimum_age_days > maximum_age_days
    ):
        _input_fail(
            "invalid_age_range",
            "最小作品年龄不能大于最大作品年龄。",
            "minimumAgeDays",
        )

    minimum_aspect = _optional_ratio(
        source["minimumAspectRatio"],
        "minimumAspectRatio",
    )
    maximum_aspect = _optional_ratio(
        source["maximumAspectRatio"],
        "maximumAspectRatio",
    )
    if (
        minimum_aspect is not None
        and maximum_aspect is not None
        and minimum_aspect > maximum_aspect
    ):
        _input_fail(
            "invalid_aspect_ratio_range",
            "最小长宽比不能大于最大长宽比。",
            "minimumAspectRatio",
        )

    filter_spec = ResultFilterSpec(
        minimum_score=_optional_integer_text(
            source["minimumScore"],
            "minimumScore",
        ),
        minimum_fav_count=_optional_integer_text(
            source["minimumFavCount"],
            "minimumFavCount",
            minimum=0,
        ),
        published_from_utc=_optional_local_datetime(
            source["publishedFrom"],
            "publishedFrom",
            zone,
        ),
        published_until_utc=_optional_local_datetime(
            source["publishedUntil"],
            "publishedUntil",
            zone,
        ),
        minimum_age=(
            _age_days(minimum_age_days, "minimumAgeDays")
            if minimum_age_days is not None
            else None
        ),
        maximum_age=(
            _age_days(maximum_age_days, "maximumAgeDays")
            if maximum_age_days is not None
            else None
        ),
        file_categories=_optional_enum_selection(
            source["fileCategories"],
            PostFileCategory,
            "fileCategories",
        ),
        minimum_width=_optional_integer_text(
            source["minimumWidth"],
            "minimumWidth",
            minimum=1,
        ),
        minimum_height=_optional_integer_text(
            source["minimumHeight"],
            "minimumHeight",
            minimum=1,
        ),
        minimum_aspect_ratio=minimum_aspect,
        maximum_aspect_ratio=maximum_aspect,
        orientations=_optional_enum_selection(
            source["orientations"],
            PostOrientation,
            "orientations",
        ),
        blocked_tags=_parse_blocked_tags(source["blockedTags"]),
    )
    sort_spec = ResultSortSpec(
        field=_enum_text(source["sortField"], ResultSortField, "sortField"),
        direction=_enum_text(
            source["sortDirection"],
            ResultSortDirection,
            "sortDirection",
        ),
    )
    try:
        limits = QueryExecutionLimits(
            page_size=_required_integer_text(
                source["pageSize"],
                "pageSize",
            ),
            candidate_limit=_required_integer_text(
                source["candidateLimit"],
                "candidateLimit",
            ),
            branch_page_limit=_required_integer_text(
                source["branchPageLimit"],
                "branchPageLimit",
            ),
            active_time_limit_seconds=_required_decimal_text(
                source["activeTimeLimitSeconds"],
                "activeTimeLimitSeconds",
            ),
        )
    except QueryInputError:
        raise
    except (TypeError, ValueError) as exc:
        _input_fail(
            "invalid_execution_limits",
            f"执行限制无效：{exc}",
            "executionLimits",
        )

    runtime_filter = filter_spec
    if minimum_age_days is not None or maximum_age_days is not None:
        runtime_filter = replace(
            runtime_filter,
            age_reference_time=datetime(2000, 1, 1, tzinfo=timezone.utc),
        )
    try:
        validated = validate_result_configuration(
            runtime_filter,
            sort_spec,
            remote_order=(RemoteOrder.RANK if mode == QueryMode.RANK else None),
        )
    except ResultProcessingError as exc:
        _input_fail(exc.code, str(exc), "filters")
    logical_filter = replace(
        validated.filter_spec,
        age_reference_time=None,
    )
    return QueryFormState(rating, logical_filter, validated.sort_spec, limits)


def parse_windows_local_datetime(
    text: str,
    time_zone: QTimeZone,
    *,
    field: str = "datetime",
) -> datetime:
    """Convert an exact local minute, rejecting both gaps and folds."""

    if not isinstance(text, str) or _LOCAL_DATETIME_PATTERN.fullmatch(text) is None:
        _input_fail(
            "invalid_local_datetime",
            "时间必须使用 YYYY-MM-DD HH:mm 格式。",
            field,
        )
    if not isinstance(time_zone, QTimeZone) or not time_zone.isValid():
        _input_fail("invalid_time_zone", "Windows 当前时区无效。", field)
    match = _LOCAL_DATETIME_PATTERN.fullmatch(text)
    assert match is not None
    parts = {name: int(value) for name, value in match.groupdict().items()}
    date = QDate(parts["year"], parts["month"], parts["day"])
    local_time = QTime(parts["hour"], parts["minute"])
    if not date.isValid() or not local_time.isValid():
        _input_fail("invalid_local_datetime", "本地时间不存在。", field)

    rejected = QDateTime(
        date,
        local_time,
        time_zone,
        QDateTime.TransitionResolution.Reject,
    )
    if not rejected.isValid():
        before = QDateTime(
            date,
            local_time,
            time_zone,
            QDateTime.TransitionResolution.PreferBefore,
        )
        after = QDateTime(
            date,
            local_time,
            time_zone,
            QDateTime.TransitionResolution.PreferAfter,
        )
        both_preserve_wall_time = (
            before.isValid()
            and after.isValid()
            and before.date() == date
            and after.date() == date
            and before.time() == local_time
            and after.time() == local_time
            and before.toMSecsSinceEpoch() != after.toMSecsSinceEpoch()
        )
        if both_preserve_wall_time:
            _input_fail(
                "ambiguous_local_datetime",
                "该本地时间在夏令时切换时出现两次，请改用其他时间。",
                field,
            )
        _input_fail(
            "nonexistent_local_datetime",
            "该本地时间位于夏令时跳过区间，请改用其他时间。",
            field,
        )

    utc_value = rejected.toUTC()
    utc_date = utc_value.date()
    utc_time = utc_value.time()
    try:
        return datetime(
            utc_date.year(),
            utc_date.month(),
            utc_date.day(),
            utc_time.hour(),
            utc_time.minute(),
            utc_time.second(),
            utc_time.msec() * 1000,
            tzinfo=timezone.utc,
        )
    except (OverflowError, ValueError) as exc:
        _input_fail(
            "invalid_local_datetime",
            f"本地时间无法转换成 UTC：{exc}",
            field,
        )


def build_draft_definition(
    form: QueryFormState,
    *,
    mode: QueryMode,
    normalized_expression: str | None,
) -> GalleryQueryDefinition:
    """Validate one complete draft using only existing domain functions."""

    if not isinstance(form, QueryFormState):
        raise TypeError("form must be a QueryFormState")
    if not isinstance(mode, QueryMode):
        raise TypeError("mode must be a QueryMode")

    parsed = None
    expression = normalized_expression
    if mode == QueryMode.LATEST:
        expression = None
        build_latest_query_plan(rating=form.rating.value)
    elif mode == QueryMode.RANK and expression is None:
        build_rank_query_plan(None, rating=form.rating.value)
    else:
        parsed = parse_query_expression(expression)
        expression = parsed.normalized_text
        if mode == QueryMode.RANK:
            build_rank_query_plan(parsed, rating=form.rating.value)
        else:
            build_standard_query_plan(parsed, rating=form.rating.value)

    return GalleryQueryDefinition(
        mode=mode,
        normalized_expression=expression,
        rating=form.rating,
        filter_spec=form.filter_spec,
        sort_spec=form.sort_spec,
        limits=form.limits,
    )


def query_form_values_from_definition(
    definition: GalleryQueryDefinition,
    *,
    time_zone: QTimeZone,
) -> dict[str, object]:
    """Convert a stored definition back to the strict QML form shape."""

    if not isinstance(definition, GalleryQueryDefinition):
        raise TypeError("definition must be a GalleryQueryDefinition")
    if not isinstance(time_zone, QTimeZone) or not time_zone.isValid():
        _input_fail("invalid_time_zone", "Windows 当前时区无效。", "timeZone")
    spec = definition.filter_spec
    values = default_query_form_values()
    values.update(
        {
            "rating": definition.rating.value,
            "minimumScore": _optional_number_text(spec.minimum_score),
            "minimumFavCount": _optional_number_text(spec.minimum_fav_count),
            "publishedFrom": _utc_datetime_text(
                spec.published_from_utc,
                time_zone,
                "publishedFrom",
            ),
            "publishedUntil": _utc_datetime_text(
                spec.published_until_utc,
                time_zone,
                "publishedUntil",
            ),
            "minimumAgeDays": _timedelta_days_text(
                spec.minimum_age,
                "minimumAgeDays",
            ),
            "maximumAgeDays": _timedelta_days_text(
                spec.maximum_age,
                "maximumAgeDays",
            ),
            "fileCategories": (
                []
                if spec.file_categories is None
                else sorted(item.value for item in spec.file_categories)
            ),
            "minimumWidth": _optional_number_text(spec.minimum_width),
            "minimumHeight": _optional_number_text(spec.minimum_height),
            "minimumAspectRatio": _optional_fraction_text(
                spec.minimum_aspect_ratio
            ),
            "maximumAspectRatio": _optional_fraction_text(
                spec.maximum_aspect_ratio
            ),
            "orientations": (
                []
                if spec.orientations is None
                else sorted(item.value for item in spec.orientations)
            ),
            "blockedTags": "\n".join(sorted(spec.blocked_tags)),
            "sortField": definition.sort_spec.field.value,
            "sortDirection": definition.sort_spec.direction.value,
            "pageSize": str(definition.limits.page_size),
            "candidateLimit": str(definition.limits.candidate_limit),
            "branchPageLimit": str(definition.limits.branch_page_limit),
            "activeTimeLimitSeconds": (
                f"{definition.limits.active_time_limit_seconds:g}"
            ),
        }
    )
    # Reuse the forward converter so a stored definition cannot produce a form
    # that the selected query mode would reject.
    parse_query_form(values, mode=definition.mode, time_zone=time_zone)
    return values


_STATE_TEXT = {
    QueryExecutionState.IDLE: "尚未开始查询",
    QueryExecutionState.WAITING: "正在等待任务队列",
    QueryExecutionState.RUNNING: "正在扫描作品",
    QueryExecutionState.RETRY_WAIT: "正在等待自动重试",
    QueryExecutionState.COMPLETE: "有限扫描已经完成",
    QueryExecutionState.PARTIAL_CANCELLED: "查询已取消，现有结果已经保留",
    QueryExecutionState.PARTIAL_FAILED: "查询失败，现有结果已经保留",
    QueryExecutionState.FAILED: "查询失败，没有取得可用的新结果",
}
_LIMIT_TEXT = {
    QueryLimitReason.CANDIDATE_LIMIT: "已经达到候选数量上限",
    QueryLimitReason.PAGE_LIMIT: "已经达到每分支页数上限",
    QueryLimitReason.TIME_LIMIT: "已经达到活动时间上限",
}
_FAILURE_TEXT = {
    QueryExecutionErrorCode.AUTHENTICATION: "身份或访问权限失败",
    QueryExecutionErrorCode.RATE_LIMITED: "请求过于频繁",
    QueryExecutionErrorCode.HTTP: "HTTP 请求失败",
    QueryExecutionErrorCode.TIMEOUT: "请求超时",
    QueryExecutionErrorCode.NETWORK: "网络连接失败",
    QueryExecutionErrorCode.INVALID_RESPONSE: "响应数据无效",
    QueryExecutionErrorCode.REQUEST_START: "请求无法启动",
    QueryExecutionErrorCode.QUEUE_REJECTED: "任务队列暂时无法接受请求",
}


class M2QueryBridgeController(QObject):
    """Expose one guarded, QML-shaped boundary around the M2 controller."""

    draftChanged = Signal()
    stateChanged = Signal()
    errorChanged = Signal()
    availabilityChanged = Signal()
    countsChanged = Signal()
    galleryChanged = Signal()
    draftEdited = Signal()
    viewUpdatePreparing = Signal(int, int, int)
    viewUpdateAborted = Signal(int, int, int)

    def __init__(
        self,
        controller: DanbooruM2QueryController,
        *,
        query_editor: QueryTreeEditor | None = None,
        time_zone: QTimeZone | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._editor = query_editor or QueryTreeEditor()
        self._time_zone = QTimeZone.systemTimeZone() if time_zone is None else time_zone
        if not isinstance(self._time_zone, QTimeZone) or not self._time_zone.isValid():
            raise ValueError("time_zone must be a valid QTimeZone")
        self._raw_form = default_query_form_values()
        self._draft_mode = QueryMode.STANDARD
        self._rank_uses_expression = False
        self._form: QueryFormState | None = None
        self._form_error: QueryInputDiagnostic | None = None
        self._command_error: QueryInputDiagnostic | None = None
        self._snapshot: QueryExecutionSnapshot | None = None
        self._accepted_identity: tuple[int, int] | None = None
        self._definition_by_operation: dict[int, GalleryQueryDefinition] = {}
        self._range_by_operation: dict[int, str] = {}
        self._gallery_definition: GalleryQueryDefinition | None = None
        self._gallery_range = ""
        self._visible_count = 0
        self._last_result_operation_id: int | None = None
        self._loaded_history_definition: GalleryQueryDefinition | None = None
        self._history_load_generation = 0
        self._closing = False
        self._detached = False
        self._parse_current_form()

        controller.queryAccepted.connect(self._query_accepted)
        controller.viewDefinitionChanged.connect(self._view_changed)
        controller.resultSummaryChanged.connect(self._result_changed)
        controller.executor.snapshotChanged.connect(self._snapshot_changed)

    @property
    def core_controller(self) -> DanbooruM2QueryController:
        return self._controller

    @property
    def query_editor(self) -> QueryTreeEditor:
        return self._editor

    @property
    def draft_definition(self) -> GalleryQueryDefinition | None:
        return self._try_draft_definition()

    @property
    def execution_definition(self) -> GalleryQueryDefinition | None:
        return self._controller.definition

    @property
    def gallery_definition(self) -> GalleryQueryDefinition | None:
        return self._gallery_definition

    @property
    def loaded_history_definition(self) -> GalleryQueryDefinition | None:
        """Return the exact stored definition until the draft is edited."""

        return self._loaded_history_definition

    @Property(QObject, constant=True)
    def postsModel(self) -> QObject:
        return self._controller.posts_model

    @Property("QVariantList", notify=draftChanged)
    def queryTreeRows(self) -> list[dict[str, object]]:
        return _query_tree_rows(self._editor.root)

    @Property(str, notify=draftChanged)
    def advancedText(self) -> str:
        return self._editor.draft_text

    @Property(str, notify=draftChanged)
    def canonicalExpression(self) -> str:
        return self._editor.canonical_text

    @Property(bool, notify=draftChanged)
    def visualEditingEnabled(self) -> bool:
        return self._editor.visual_editing_enabled

    @Property(int, notify=draftChanged)
    def editorRevision(self) -> int:
        return self._editor.revision

    @Property(str, notify=draftChanged)
    def draftMode(self) -> str:
        return self._draft_mode.value

    @Property(bool, notify=draftChanged)
    def rankUsesExpression(self) -> bool:
        return self._rank_uses_expression

    @Property(str, constant=True)
    def timeZoneId(self) -> str:
        return bytes(self._time_zone.id()).decode("utf-8", errors="replace")

    @Property(int, notify=stateChanged)
    def currentOperationId(self) -> int:
        return self._controller.operation_id or 0

    @Property(int, notify=stateChanged)
    def currentExecutionId(self) -> int:
        return self._controller.execution_id or 0

    @Property(int, notify=stateChanged)
    def currentViewGeneration(self) -> int:
        return self._controller.view_generation

    @Property(int, notify=galleryChanged)
    def galleryOperationId(self) -> int:
        return self._controller.gallery_operation_id or 0

    @Property(bool, notify=stateChanged)
    def hasCurrentExecution(self) -> bool:
        return self._controller.definition is not None

    @Property(str, notify=stateChanged)
    def executionMode(self) -> str:
        definition = self._controller.definition
        return definition.mode.value if definition is not None else ""

    @Property(str, notify=stateChanged)
    def executionExpression(self) -> str:
        definition = self._controller.definition
        return definition.normalized_expression or "" if definition is not None else ""

    @Property(bool, notify=galleryChanged)
    def hasDisplayedGallery(self) -> bool:
        return self._gallery_definition is not None

    @Property(str, notify=galleryChanged)
    def galleryMode(self) -> str:
        definition = self._gallery_definition
        return definition.mode.value if definition is not None else ""

    @Property(str, notify=galleryChanged)
    def galleryExpression(self) -> str:
        definition = self._gallery_definition
        return definition.normalized_expression or "" if definition is not None else ""

    @Property(bool, notify=draftChanged)
    def executionConditionsChanged(self) -> bool:
        draft = self._try_draft_definition()
        current = self._controller.definition
        if draft is None or current is None:
            return draft != current
        return self._execution_signature(draft) != self._execution_signature(current)

    @Property(bool, notify=draftChanged)
    def viewConditionsChanged(self) -> bool:
        draft = self._try_draft_definition()
        current = self._controller.definition
        if draft is None or current is None:
            return False
        return self._view_signature(draft) != self._view_signature(current)

    @Property(bool, notify=errorChanged)
    def hasDraftError(self) -> bool:
        return self._current_input_error() is not None

    @Property(str, notify=errorChanged)
    def errorCode(self) -> str:
        error = self._current_input_error()
        if error is not None:
            return error.code
        failure = self._failure()
        return failure.error_code.value if failure is not None else ""

    @Property(str, notify=errorChanged)
    def inputErrorCode(self) -> str:
        error = self._current_input_error()
        return error.code if error is not None else ""

    @Property(str, notify=errorChanged)
    def failureErrorCode(self) -> str:
        failure = self._failure()
        return failure.error_code.value if failure is not None else ""

    @Property(str, notify=errorChanged)
    def errorText(self) -> str:
        error = self._current_input_error()
        if error is not None:
            return error.message
        failure = self._failure()
        return _FAILURE_TEXT.get(failure.error_code, "") if failure is not None else ""

    @Property(str, notify=errorChanged)
    def inputErrorText(self) -> str:
        error = self._current_input_error()
        return error.message if error is not None else ""

    @Property(str, notify=errorChanged)
    def failureErrorText(self) -> str:
        failure = self._failure()
        return _FAILURE_TEXT.get(failure.error_code, "") if failure is not None else ""

    @Property(str, notify=errorChanged)
    def errorField(self) -> str:
        error = self._current_input_error()
        return error.field if error is not None else ""

    @Property(int, notify=errorChanged)
    def errorStart(self) -> int:
        error = self._current_input_error()
        return error.span.start if error is not None and error.span is not None else -1

    @Property(int, notify=errorChanged)
    def errorEnd(self) -> int:
        error = self._current_input_error()
        return error.span.end if error is not None and error.span is not None else -1

    @Property(str, notify=stateChanged)
    def queryState(self) -> str:
        return self._snapshot.state.value if self._snapshot is not None else "idle"

    @Property(str, notify=stateChanged)
    def statusText(self) -> str:
        snapshot = self._snapshot
        if snapshot is None:
            return _STATE_TEXT[QueryExecutionState.IDLE]
        if snapshot.state == QueryExecutionState.PARTIAL_LIMITED:
            return _LIMIT_TEXT.get(snapshot.limit_reason, "查询已经在有限范围内停止")
        return _STATE_TEXT.get(snapshot.state, "查询状态已经更新")

    @Property(int, notify=countsChanged)
    def remoteRows(self) -> int:
        return self._snapshot.counts.remote_rows if self._snapshot is not None else 0

    @Property(int, notify=countsChanged)
    def uniqueCandidates(self) -> int:
        return self._snapshot.counts.unique_candidates if self._snapshot is not None else 0

    @Property(int, notify=countsChanged)
    def localMatches(self) -> int:
        return self._snapshot.counts.local_matches if self._snapshot is not None else 0

    @Property(int, notify=countsChanged)
    def visibleCount(self) -> int:
        if self._last_result_operation_id != self._controller.operation_id:
            return 0
        return self._visible_count

    @Property(str, notify=galleryChanged)
    def rangeDescription(self) -> str:
        return self._gallery_range

    @Property(bool, notify=galleryChanged)
    def oldGalleryVisible(self) -> bool:
        operation_id = self._controller.operation_id
        gallery_id = self._controller.gallery_operation_id
        return gallery_id is not None and operation_id is not None and gallery_id != operation_id

    @Property(int, notify=errorChanged)
    def failureBranch(self) -> int:
        failure = self._failure()
        return failure.position.branch_index if failure is not None else -1

    @Property(int, notify=errorChanged)
    def failurePage(self) -> int:
        failure = self._failure()
        return failure.position.page if failure is not None else 0

    @Property(int, notify=errorChanged)
    def failureHttpStatus(self) -> int:
        failure = self._failure()
        if failure is None or failure.http_status is None:
            return 0
        return failure.http_status

    @Property(bool, notify=availabilityChanged)
    def canExecuteStandard(self) -> bool:
        return not self._closing and self._can_build(QueryMode.STANDARD, True)

    @Property(bool, notify=availabilityChanged)
    def canExecuteRank(self) -> bool:
        return not self._closing and self._can_build(
            QueryMode.RANK,
            self._rank_uses_expression,
        )

    @Property(bool, notify=availabilityChanged)
    def canExecuteGlobalRank(self) -> bool:
        return not self._closing and self._can_build(QueryMode.RANK, False)

    @Property(bool, notify=availabilityChanged)
    def canExecuteConditionalRank(self) -> bool:
        return not self._closing and self._can_build(QueryMode.RANK, True)

    @Property(bool, notify=availabilityChanged)
    def canExecuteLatest(self) -> bool:
        return not self._closing and self._can_build(QueryMode.LATEST, False)

    @Property(bool, notify=availabilityChanged)
    def canApplyView(self) -> bool:
        return (
            not self._closing
            and self._controller.execution_id is not None
            and not self.executionConditionsChanged
            and self.viewConditionsChanged
            and self._try_draft_definition() is not None
        )

    @Property(bool, notify=availabilityChanged)
    def canRefresh(self) -> bool:
        operation_id = self._controller.operation_id
        execution_id = self._controller.execution_id
        return (
            not self._closing
            and operation_id is not None
            and execution_id is not None
            and self._accepted_identity == (operation_id, execution_id)
            and not self.executionConditionsChanged
            and not self.viewConditionsChanged
            and self._try_draft_definition() is not None
        )

    @Property(bool, notify=availabilityChanged)
    def canRetry(self) -> bool:
        snapshot = self._snapshot
        return (
            not self._closing
            and snapshot is not None
            and snapshot.failure is not None
            and snapshot.state
            in {QueryExecutionState.FAILED, QueryExecutionState.PARTIAL_FAILED}
            and not self.executionConditionsChanged
            and not self.viewConditionsChanged
            and self._try_draft_definition() is not None
        )

    @Property(bool, notify=availabilityChanged)
    def canCancel(self) -> bool:
        return (
            not self._closing
            and self._snapshot is not None
            and self._snapshot.state in ACTIVE_EXECUTION_STATES
        )

    @Slot("QVariantMap", result=bool)
    def updateDraftForm(self, values: Mapping[str, object]) -> bool:
        if self._closing:
            return False
        if not self._begin_draft_edit() or self._closing:
            return False
        if not isinstance(values, Mapping):
            self._form_error = QueryInputDiagnostic(
                "invalid_form",
                "查询表单必须是键值映射。",
                "form",
            )
            self._emit_all()
            return False
        self._raw_form = deepcopy(dict(values))
        result = self._parse_current_form()
        self._command_error = None
        self._emit_all()
        return result

    @Slot(str, bool, result=bool)
    def setDraftMode(self, mode: str, rank_uses_expression: bool = False) -> bool:
        if self._closing:
            return False
        if not self._begin_draft_edit() or self._closing:
            return False
        try:
            parsed_mode = QueryMode(mode)
        except (TypeError, ValueError):
            self._command_error = QueryInputDiagnostic(
                "invalid_mode",
                "查询模式无效。",
                "mode",
            )
            self._emit_all()
            return False
        if not isinstance(rank_uses_expression, bool):
            self._command_error = QueryInputDiagnostic(
                "invalid_mode",
                "热门查询的表达式开关必须是布尔值。",
                "rankUsesExpression",
            )
            self._emit_all()
            return False
        self._draft_mode = parsed_mode
        self._rank_uses_expression = (
            rank_uses_expression if parsed_mode == QueryMode.RANK else False
        )
        self._command_error = None
        self._parse_current_form()
        self._emit_all()
        return self._form is not None

    @Slot(object, result=bool)
    def loadDraftDefinition(self, definition: object) -> bool:
        """Load history into the draft without starting a query."""

        if self._closing:
            return False
        self._history_load_generation += 1
        self._loaded_history_definition = None
        if not isinstance(definition, GalleryQueryDefinition):
            self._set_exception_error(
                TypeError("definition must be a GalleryQueryDefinition")
            )
            return False
        try:
            values = query_form_values_from_definition(
                definition,
                time_zone=self._time_zone,
            )
            parsed_form = parse_query_form(
                values,
                mode=definition.mode,
                time_zone=self._time_zone,
            )
            if definition.normalized_expression is not None:
                accepted = self._editor.apply_advanced_text(
                    definition.normalized_expression
                )
                if not accepted:
                    diagnostic = self._editor_diagnostic()
                    assert diagnostic is not None
                    raise QueryInputError(diagnostic)
        except Exception as exc:
            self._set_exception_error(exc)
            return False
        self._raw_form = deepcopy(values)
        self._form = parsed_form
        self._form_error = None
        self._draft_mode = definition.mode
        self._rank_uses_expression = (
            definition.mode == QueryMode.RANK
            and definition.normalized_expression is not None
        )
        self._loaded_history_definition = definition
        self._command_error = None
        self._emit_all()
        return True

    @Slot(str, result=bool)
    def reexecuteLoadedHistory(self, display_text: str) -> bool:
        """Execute the untouched stored definition without a form round trip."""

        if self._closing or self._loaded_history_definition is None:
            return False
        try:
            self.reexecute_loaded_history(display_text)
        except Exception as exc:
            self._set_exception_error(exc)
            return False
        self._reset_new_operation_state()
        self._snapshot = self._controller.executor.snapshot
        self._command_error = None
        self._emit_all()
        return True

    def reexecute_loaded_history(self, display_text: str) -> int:
        """Python boundary that returns the operation identity to the session."""

        definition = self._loaded_history_definition
        if self._closing or definition is None:
            raise RuntimeError("There is no untouched history definition to execute.")
        return self._controller.reexecute_history(
            definition,
            display_text=display_text,
        )

    def clear_loaded_history_definition(self) -> None:
        """Forget the exact history payload without changing the visible draft."""

        self._history_load_generation += 1
        self._loaded_history_definition = None

    @Slot(str, result=bool)
    def applyAdvancedText(self, text: str) -> bool:
        if self._closing:
            return False
        if not self._begin_draft_edit() or self._closing:
            return False
        try:
            accepted = self._editor.apply_advanced_text(text)
        except Exception as exc:
            self._set_exception_error(exc)
            return False
        self._command_error = None
        self._emit_all()
        return accepted

    @Slot(result=bool)
    def discardInvalidText(self) -> bool:
        if self._closing:
            return False
        if not self._begin_draft_edit() or self._closing:
            return False
        changed = self._editor.discard_invalid_text()
        if changed:
            self._command_error = None
            self._emit_all()
        return changed

    @Slot("QVariant", str, result=bool)
    def addVisualTag(self, parent_path: object, name: str) -> bool:
        try:
            path = _variant_path(parent_path, allow_none=True)
        except Exception as exc:
            self._set_exception_error(exc)
            return False
        return self._edit_tree(
            self._editor.add_tag,
            path,
            name,
        )

    @Slot("QVariantList", result=bool)
    def removeVisualNode(self, path: Sequence[object]) -> bool:
        try:
            parsed_path = _variant_path(path)
        except Exception as exc:
            self._set_exception_error(exc)
            return False
        return self._edit_tree(self._editor.remove_node, parsed_path)

    @Slot("QVariantList", int, result=bool)
    def moveVisualNode(self, path: Sequence[object], direction: int) -> bool:
        try:
            parsed_path = _variant_path(path)
        except Exception as exc:
            self._set_exception_error(exc)
            return False
        return self._edit_tree(
            self._editor.move_node,
            parsed_path,
            direction,
        )

    @Slot("QVariantList", result=bool)
    def wrapVisualNot(self, path: Sequence[object]) -> bool:
        try:
            parsed_path = _variant_path(path)
        except Exception as exc:
            self._set_exception_error(exc)
            return False
        return self._edit_tree(self._editor.wrap_not, parsed_path)

    @Slot("QVariantList", result=bool)
    def unwrapVisualNot(self, path: Sequence[object]) -> bool:
        try:
            parsed_path = _variant_path(path)
        except Exception as exc:
            self._set_exception_error(exc)
            return False
        return self._edit_tree(self._editor.unwrap_not, parsed_path)

    @Slot("QVariantList", str, str, bool, result=bool)
    def wrapVisualGroup(
        self,
        path: Sequence[object],
        operator: str,
        sibling_tag: str,
        sibling_after: bool,
    ) -> bool:
        try:
            parsed_operator = QueryGroupOperator(operator)
        except (TypeError, ValueError):
            self._set_exception_error(
                QueryInputError(
                    QueryInputDiagnostic(
                        "invalid_group_operator",
                        "条件组类型无效。",
                        "operator",
                    )
                )
            )
            return False
        try:
            parsed_path = _variant_path(path)
        except Exception as exc:
            self._set_exception_error(exc)
            return False
        try:
            sibling = TagNode(sibling_tag, SourceSpan(0, 0))
        except Exception as exc:
            self._set_exception_error(
                QueryInputError(
                    QueryInputDiagnostic(
                        "invalid_tag",
                        f"标签无效：{exc}",
                        "tag",
                    )
                )
            )
            return False
        return self._edit_tree(
            self._editor.wrap_with_group,
            parsed_path,
            parsed_operator,
            sibling,
            sibling_after=sibling_after,
        )

    @Slot("QVariant", "QVariantList", str, result=bool)
    def insertArtistGroup(
        self,
        parent_path: object,
        artists: Sequence[object],
        mode: str,
    ) -> bool:
        try:
            parsed_mode = ArtistCombinationMode(mode)
            confirmed = tuple(_confirmed_artist(value) for value in artists)
            path = _variant_path(parent_path, allow_none=True)
        except Exception as exc:
            self._set_exception_error(exc)
            return False
        return self._edit_tree(
            self._editor.insert_artist_group,
            confirmed,
            parsed_mode,
            parent_path=path,
        )

    @Slot(result=bool)
    def executeStandard(self) -> bool:
        return self._execute(QueryMode.STANDARD, True)

    @Slot(bool, result=bool)
    def executeRank(self, with_expression: bool = False) -> bool:
        if not isinstance(with_expression, bool):
            self._set_exception_error(TypeError("with_expression must be bool"))
            return False
        return self._execute(QueryMode.RANK, with_expression)

    @Slot(result=bool)
    def executeLatest(self) -> bool:
        return self._execute(QueryMode.LATEST, False)

    @Slot(result=bool)
    def applyView(self) -> bool:
        if self._closing or not self.canApplyView:
            return False
        definition = self._try_draft_definition()
        if definition is None:
            return False
        operation_id = self._controller.operation_id
        execution_id = self._controller.execution_id
        view_generation = self._controller.view_generation
        if operation_id is None or execution_id is None:
            return False
        self.viewUpdatePreparing.emit(
            operation_id,
            execution_id,
            view_generation,
        )
        current_definition = self._try_draft_definition()
        if (
            self._closing
            or self._controller.operation_id != operation_id
            or self._controller.execution_id != execution_id
            or self._controller.view_generation != view_generation
            or not self.canApplyView
            or current_definition != definition
        ):
            self.viewUpdateAborted.emit(
                operation_id,
                execution_id,
                view_generation,
            )
            return False
        try:
            applied = self._controller.update_view(
                filter_spec=definition.filter_spec,
                sort_spec=definition.sort_spec,
            )
        except Exception as exc:
            self.viewUpdateAborted.emit(
                operation_id,
                execution_id,
                view_generation,
            )
            self._set_exception_error(exc)
            return False
        if not applied:
            self.viewUpdateAborted.emit(
                operation_id,
                execution_id,
                view_generation,
            )
            return False
        self._command_error = None
        self._emit_all()
        return True

    @Slot(result=bool)
    def refresh(self) -> bool:
        if not self.canRefresh:
            return False
        try:
            self._controller.refresh()
        except Exception as exc:
            self._set_exception_error(exc)
            return False
        self._reset_new_operation_state()
        self._snapshot = self._controller.executor.snapshot
        self._command_error = None
        self._emit_all()
        return True

    @Slot(result=bool)
    def retryFailedPage(self) -> bool:
        if not self.canRetry:
            return False
        try:
            retried = self._controller.retry_failed_page()
        except Exception as exc:
            self._set_exception_error(exc)
            return False
        if retried:
            self._command_error = None
            self._snapshot = self._controller.executor.snapshot
            self._emit_all()
        return retried

    @Slot(result=bool)
    def cancel(self) -> bool:
        if not self.canCancel:
            return False
        try:
            cancelled = self._controller.cancel()
        except Exception as exc:
            self._set_exception_error(exc)
            return False
        if cancelled:
            self._snapshot = self._controller.executor.snapshot
            self._emit_all()
        return cancelled

    @Slot()
    def beginClosing(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._emit_all()

    def _edit_tree(self, operation, *args, **kwargs) -> bool:
        if self._closing:
            return False
        if not self._begin_draft_edit() or self._closing:
            return False
        try:
            operation(*args, **kwargs)
        except Exception as exc:
            self._set_exception_error(exc)
            return False
        self._command_error = None
        self._emit_all()
        return True

    def _begin_draft_edit(self) -> bool:
        generation = self._history_load_generation
        self._loaded_history_definition = None
        self.draftEdited.emit()
        if generation != self._history_load_generation:
            return False
        self._loaded_history_definition = None
        return True

    def _execute(self, mode: QueryMode, with_expression: bool) -> bool:
        if self._closing:
            return False
        self._draft_mode = mode
        self._rank_uses_expression = with_expression if mode == QueryMode.RANK else False
        self._parse_current_form()
        try:
            definition = self._draft_definition()
        except Exception as exc:
            self._set_exception_error(exc)
            self._emit_all()
            return False
        try:
            if mode == QueryMode.STANDARD:
                assert definition.normalized_expression is not None
                self._controller.start_standard(
                    definition.normalized_expression,
                    display_text=definition.normalized_expression,
                    rating=definition.rating.value,
                    filter_spec=definition.filter_spec,
                    sort_spec=definition.sort_spec,
                    limits=definition.limits,
                )
            elif mode == QueryMode.RANK:
                self._controller.start_rank(
                    definition.normalized_expression,
                    display_text=(
                        definition.normalized_expression
                        if definition.normalized_expression is not None
                        else "近期热门"
                    ),
                    rating=definition.rating.value,
                    filter_spec=definition.filter_spec,
                    sort_spec=definition.sort_spec,
                    limits=definition.limits,
                )
            else:
                self._controller.start_latest(
                    rating=definition.rating.value,
                    filter_spec=definition.filter_spec,
                    sort_spec=definition.sort_spec,
                    limits=definition.limits,
                )
        except Exception as exc:
            self._set_exception_error(exc)
            return False
        self._reset_new_operation_state()
        self._snapshot = self._controller.executor.snapshot
        self._command_error = None
        self._emit_all()
        return True

    def _parse_current_form(self) -> bool:
        try:
            parsed = parse_query_form(
                self._raw_form,
                mode=self._draft_mode,
                time_zone=self._time_zone,
            )
        except QueryInputError as exc:
            self._form = None
            self._form_error = exc.diagnostic
            return False
        except (TypeError, ValueError) as exc:
            self._form = None
            self._form_error = QueryInputDiagnostic(
                "invalid_form",
                str(exc),
                "form",
            )
            return False
        self._form = parsed
        self._form_error = None
        return True

    def _reset_new_operation_state(self) -> None:
        operation_id = self._controller.operation_id
        execution_id = self._controller.execution_id
        if self._accepted_identity != (operation_id, execution_id):
            self._accepted_identity = None
        if self._last_result_operation_id != operation_id:
            self._visible_count = 0

    def _try_draft_definition(self) -> GalleryQueryDefinition | None:
        try:
            return self._draft_definition()
        except Exception:
            return None

    def _draft_definition(self) -> GalleryQueryDefinition:
        exact = self._loaded_history_definition
        if exact is not None and exact.mode == self._draft_mode:
            exact_uses_expression = (
                exact.normalized_expression is not None
                if exact.mode == QueryMode.RANK
                else exact.mode == QueryMode.STANDARD
            )
            requested_uses_expression = (
                self._rank_uses_expression
                if self._draft_mode == QueryMode.RANK
                else self._draft_mode == QueryMode.STANDARD
            )
            if exact_uses_expression == requested_uses_expression:
                return exact
        form = self._form
        if form is None:
            diagnostic = self._form_error or QueryInputDiagnostic(
                "invalid_form",
                "查询表单无效。",
                "form",
            )
            raise QueryInputError(diagnostic)
        expression: str | None = None
        if self._draft_mode == QueryMode.STANDARD or (
            self._draft_mode == QueryMode.RANK and self._rank_uses_expression
        ):
            editor_error = self._editor_diagnostic()
            if editor_error is not None:
                raise QueryInputError(editor_error)
            expression = self._editor_text()
        return build_draft_definition(
            form,
            mode=self._draft_mode,
            normalized_expression=expression,
        )

    def _can_build(self, mode: QueryMode, with_expression: bool) -> bool:
        try:
            form = parse_query_form(
                self._raw_form,
                mode=mode,
                time_zone=self._time_zone,
            )
        except (QueryInputError, TypeError, ValueError):
            return False
        expression: str | None = None
        if mode == QueryMode.STANDARD or (mode == QueryMode.RANK and with_expression):
            if self._editor_diagnostic() is not None:
                return False
            expression = self._editor_text()
        try:
            build_draft_definition(
                form,
                mode=mode,
                normalized_expression=expression,
            )
        except Exception:
            return False
        return True

    def _editor_text(self) -> str:
        editor = self._editor
        for name in ("canonical_text", "normalized_text", "draft_text"):
            value = getattr(editor, name, None)
            if isinstance(value, str):
                return value
        return ""

    def _editor_diagnostic(self) -> QueryInputDiagnostic | None:
        editor = self._editor
        diagnostic = getattr(editor, "text_diagnostic", None)
        if diagnostic is None:
            diagnostic = getattr(editor, "diagnostic", None)
        if diagnostic is None:
            return None
        span = getattr(diagnostic, "span", None)
        return QueryInputDiagnostic(
            str(getattr(diagnostic, "code", "invalid_expression")),
            str(getattr(diagnostic, "message", "查询表达式无效。")),
            "expression",
            span if isinstance(span, SourceSpan) else None,
        )

    def _current_input_error(self) -> QueryInputDiagnostic | None:
        return (
            self._command_error
            or self._form_error
            or self._editor_diagnostic()
            or self._definition_diagnostic()
        )

    def _definition_diagnostic(self) -> QueryInputDiagnostic | None:
        if self._form is None:
            return None
        try:
            self._draft_definition()
        except Exception as exc:
            return self._diagnostic_for_exception(exc)
        return None

    def _set_exception_error(self, exc: Exception) -> None:
        self._command_error = self._diagnostic_for_exception(exc)
        self._emit_all()

    def _diagnostic_for_exception(self, exc: Exception) -> QueryInputDiagnostic:
        if isinstance(exc, QueryInputError):
            diagnostic = exc.diagnostic
        elif isinstance(exc, QueryParseError):
            diagnostic = QueryInputDiagnostic(
                exc.code,
                str(exc),
                "expression",
                exc.span,
            )
        elif isinstance(exc, QueryPlanError):
            diagnostic = QueryInputDiagnostic(
                exc.code,
                str(exc),
                "expression",
                SourceSpan(0, len(self._editor_text())),
            )
        elif isinstance(exc, ResultProcessingError):
            diagnostic = QueryInputDiagnostic(exc.code, str(exc), "filters")
        elif isinstance(exc, QueryEditorError):
            diagnostic = QueryInputDiagnostic(
                exc.code,
                str(exc),
                "expression",
                exc.diagnostic.span,
            )
        elif isinstance(exc, RuntimeError):
            diagnostic = QueryInputDiagnostic(
                "operation_unavailable",
                str(exc),
                "operation",
            )
        else:
            diagnostic = QueryInputDiagnostic(
                "invalid_parameters",
                str(exc) or "查询参数无效。",
                "form",
            )
        return diagnostic

    def _query_accepted(self, value: object) -> None:
        if self._detached or not isinstance(value, GalleryQueryAccepted):
            return
        if not self._operation_is_current(value.operation_id, value.execution_id):
            return
        self._accepted_identity = (value.operation_id, value.execution_id)
        self._definition_by_operation[value.operation_id] = value.definition
        self._command_error = None
        self._emit_all(
            guard=lambda: self._operation_is_current(
                value.operation_id,
                value.execution_id,
            )
        )

    def _view_changed(self, value: object) -> None:
        if self._detached or not isinstance(value, GalleryViewDefinitionChanged):
            return
        if not self._operation_is_current(
            value.operation_id,
            value.execution_id,
            view_generation=value.view_generation,
        ):
            return
        if (
            value.applied_to_gallery
            and self._controller.gallery_operation_id != value.operation_id
        ):
            return
        self._definition_by_operation[value.operation_id] = value.definition
        if (
            value.applied_to_gallery
            and self._controller.gallery_operation_id == value.operation_id
        ):
            self._gallery_definition = value.definition
        self._emit_all(
            guard=lambda: (
                self._operation_is_current(
                    value.operation_id,
                    value.execution_id,
                    view_generation=value.view_generation,
                )
                and (
                    not value.applied_to_gallery
                    or self._controller.gallery_operation_id == value.operation_id
                )
            )
        )

    def _result_changed(self, value: object) -> None:
        if self._detached or not isinstance(value, GalleryResultEvent):
            return
        if not self._operation_is_current(
            value.operation_id,
            value.execution_id,
            view_generation=value.view_generation,
        ):
            return
        if value.gallery_operation_id != self._controller.gallery_operation_id:
            return
        processed = self._controller.last_processed
        if processed is None:
            return
        snapshot = processed.source_snapshot
        if not self._result_matches_snapshot(value, processed, snapshot):
            return
        if not self._operation_is_current(
            value.operation_id,
            value.execution_id,
            view_generation=value.view_generation,
        ):
            return
        if value.gallery_operation_id != self._controller.gallery_operation_id:
            return

        # The core emits resultSummaryChanged while it is handling the executor's
        # snapshotChanged signal.  Adopt that exact source snapshot before any
        # QML-facing signal so counts and visible rows change atomically.
        self._snapshot = snapshot
        self._definition_by_operation[value.operation_id] = value.definition
        self._visible_count = value.summary.visible_count
        self._last_result_operation_id = value.operation_id
        self._range_by_operation[value.operation_id] = processed.range_description
        gallery_id = value.gallery_operation_id
        if gallery_id is not None:
            self._gallery_definition = self._definition_by_operation.get(gallery_id)
            self._gallery_range = self._range_by_operation.get(gallery_id, "")
        self._emit_all(
            guard=lambda: (
                self._operation_is_current(
                    value.operation_id,
                    value.execution_id,
                    view_generation=value.view_generation,
                )
                and value.gallery_operation_id
                == self._controller.gallery_operation_id
            )
        )

    def _snapshot_changed(self, value: object) -> None:
        if self._detached or not isinstance(value, QueryExecutionSnapshot):
            return
        if self._controller.execution_id != value.execution_id:
            return
        if self._snapshot is value or self._snapshot == value:
            return
        self._snapshot = value
        self._emit_all(
            guard=lambda: self._controller.execution_id == value.execution_id
        )

    @staticmethod
    def _result_matches_snapshot(
        event: GalleryResultEvent,
        processed: ProcessedQueryResults,
        snapshot: QueryExecutionSnapshot,
    ) -> bool:
        if snapshot.execution_id != event.execution_id:
            return False
        summary = event.summary
        failure_code = (
            snapshot.failure.error_code if snapshot.failure is not None else None
        )
        return (
            processed.source_snapshot is snapshot
            and processed.visible_count == summary.visible_count
            and snapshot.state == summary.state
            and snapshot.counts.remote_rows == summary.remote_rows
            and snapshot.counts.unique_candidates == summary.unique_candidates
            and snapshot.counts.local_matches == summary.local_matches
            and snapshot.limit_reason == summary.limit_reason
            and failure_code == summary.failure_error_code
        )

    def _operation_is_current(
        self,
        operation_id: int,
        execution_id: int,
        *,
        view_generation: int | None = None,
    ) -> bool:
        if (
            self._detached
            or self._controller.operation_id != operation_id
            or self._controller.execution_id != execution_id
        ):
            return False
        return (
            view_generation is None
            or self._controller.view_generation == view_generation
        )

    def _failure(self):
        return self._snapshot.failure if self._snapshot is not None else None

    @staticmethod
    def _execution_signature(definition: GalleryQueryDefinition) -> tuple[object, ...]:
        return (
            definition.mode,
            definition.normalized_expression,
            definition.rating,
            definition.limits,
        )

    @staticmethod
    def _view_signature(definition: GalleryQueryDefinition) -> tuple[object, ...]:
        return (definition.filter_spec, definition.sort_spec)

    def _emit_all(self, *, guard=None) -> None:
        for signal in (
            self.draftChanged,
            self.stateChanged,
            self.errorChanged,
            self.availabilityChanged,
            self.countsChanged,
            self.galleryChanged,
        ):
            if guard is not None and not guard():
                return
            signal.emit()
            if guard is not None and not guard():
                return


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        _input_fail("invalid_text", "输入值必须是文本。", field)
    return value


def _optional_integer_text(
    value: object,
    field: str,
    *,
    minimum: int | None = None,
) -> int | None:
    text = _required_text(value, field)
    if text == "":
        return None
    if text != text.strip() or _INTEGER_PATTERN.fullmatch(text) is None:
        _input_fail("invalid_integer", "请输入完整整数。", field)
    digits = text.lstrip("+-")
    if len(digits) > _MAX_INTEGER_DIGITS:
        _input_fail("invalid_integer", "整数位数过多。", field)
    try:
        parsed = int(text)
    except ValueError:
        _input_fail("invalid_integer", "请输入完整整数。", field)
    if minimum is not None and parsed < minimum:
        _input_fail(
            "invalid_integer_range",
            f"该数值不能小于 {minimum}。",
            field,
        )
    return parsed


def _required_integer_text(value: object, field: str) -> int:
    parsed = _optional_integer_text(value, field, minimum=1)
    if parsed is None:
        _input_fail("required_value", "该数值不能为空。", field)
    return parsed


def _required_decimal_text(value: object, field: str) -> float:
    text = _required_text(value, field)
    if text != text.strip() or _POSITIVE_DECIMAL_PATTERN.fullmatch(text) is None:
        _input_fail("invalid_decimal", "请输入正数。", field)
    try:
        parsed = Decimal(text)
        converted = float(parsed)
    except (InvalidOperation, OverflowError, ValueError):
        _input_fail("invalid_decimal", "请输入有效正数。", field)
    if parsed <= 0 or not math.isfinite(converted):
        _input_fail("invalid_decimal", "请输入有效正数。", field)
    return converted


def _optional_ratio(value: object, field: str) -> Fraction | None:
    text = _required_text(value, field)
    if text == "":
        return None
    if text != text.strip():
        _input_fail("invalid_aspect_ratio", "长宽比必须使用正数 宽:高。", field)
    match = _POSITIVE_RATIO_PATTERN.fullmatch(text)
    if match is None:
        _input_fail("invalid_aspect_ratio", "长宽比必须使用正数 宽:高。", field)
    numerator_text = match.group("numerator")
    denominator_text = match.group("denominator")
    if (
        len(numerator_text) > _MAX_INTEGER_DIGITS
        or len(denominator_text) > _MAX_INTEGER_DIGITS
    ):
        _input_fail("invalid_aspect_ratio", "长宽比位数过多。", field)
    try:
        numerator = int(numerator_text)
        denominator = int(denominator_text)
    except ValueError:
        _input_fail("invalid_aspect_ratio", "长宽比必须使用正数 宽:高。", field)
    if numerator < 1 or denominator < 1:
        _input_fail("invalid_aspect_ratio", "长宽比两侧都必须大于零。", field)
    return Fraction(numerator, denominator)


def _age_days(value: int, field: str) -> timedelta:
    try:
        return timedelta(days=value)
    except OverflowError:
        _input_fail(
            "invalid_age_range",
            "作品年龄天数超出可处理范围。",
            field,
        )


def _optional_number_text(value: int | None) -> str:
    return "" if value is None else str(value)


def _optional_fraction_text(value) -> str:
    if value is None:
        return ""
    try:
        parsed = Fraction(value)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        _input_fail(
            "invalid_aspect_ratio",
            f"保存的长宽比无效：{exc}",
            "aspectRatio",
        )
    return f"{parsed.numerator}:{parsed.denominator}"


def _timedelta_days_text(value: timedelta | None, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, timedelta) or value < timedelta(0):
        _input_fail(
            "invalid_age_range",
            "保存的作品年龄无效。",
            field,
        )
    # The current form displays whole days.  The bridge separately retains the
    # exact immutable definition, so an untouched history re-execution never
    # uses this display-only truncation.
    return str(value // timedelta(days=1))


def _utc_datetime_text(
    value: datetime | None,
    time_zone: QTimeZone,
    field: str,
) -> str:
    if value is None:
        return ""
    if not isinstance(value, datetime):
        _input_fail("invalid_local_datetime", "保存的时间无效。", field)
    try:
        if value.utcoffset() is None:
            raise ValueError("time zone is missing")
        converted = QDateTime(value.astimezone(timezone.utc)).toTimeZone(time_zone)
    except (OverflowError, OSError, TypeError, ValueError) as exc:
        _input_fail(
            "invalid_local_datetime",
            f"保存的时间无法转换到 Windows 当前时区：{exc}",
            field,
        )
    if not converted.isValid():
        _input_fail(
            "invalid_local_datetime",
            "保存的时间无法转换到 Windows 当前时区。",
            field,
        )
    return converted.toString("yyyy-MM-dd HH:mm")


def _optional_local_datetime(
    value: object,
    field: str,
    time_zone: QTimeZone,
) -> datetime | None:
    text = _required_text(value, field)
    if text == "":
        return None
    return parse_windows_local_datetime(text, time_zone, field=field)


def _optional_enum_selection(value: object, enum_type, field: str):
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _input_fail("invalid_selection", "选择值必须是列表。", field)
    parsed = []
    for item in value:
        if not isinstance(item, str):
            _input_fail("invalid_selection", "选择列表包含非文本值。", field)
        try:
            parsed.append(enum_type(item))
        except ValueError:
            _input_fail("invalid_selection", f"选择列表包含无效值：{item}。", field)
    return frozenset(parsed) if parsed else None


def _enum_text(value: object, enum_type, field: str):
    text = _required_text(value, field)
    try:
        return enum_type(text)
    except ValueError:
        _input_fail("invalid_selection", f"字段 {field} 的值无效。", field)


def _parse_blocked_tags(value: object) -> frozenset[str]:
    text = _required_text(value, "blockedTags")
    if not text:
        return frozenset()
    tags: set[str] = set()
    for raw in re.split(r"[,\r\n]", text):
        candidate = raw.strip()
        if not candidate:
            continue
        try:
            tags.add(normalize_tag_name(candidate))
        except (TagSyntaxError, TypeError) as exc:
            _input_fail("invalid_blocked_tag", f"屏蔽标签无效：{exc}", "blockedTags")
    return frozenset(tags)


def _input_fail(
    code: str,
    message: str,
    field: str,
    span: SourceSpan | None = None,
) -> None:
    raise QueryInputError(QueryInputDiagnostic(code, message, field, span))


def _variant_path(
    value: object,
    *,
    allow_none: bool = False,
) -> tuple[int, ...] | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        _input_fail(
            "invalid_tree_path",
            "查询树路径必须是非负整数列表。",
            "treePath",
        )
    path: list[int] = []
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            _input_fail(
                "invalid_tree_path",
                "查询树路径必须是非负整数列表。",
                "treePath",
            )
        path.append(item)
    return tuple(path)


def _confirmed_artist(value: object) -> ConfirmedArtistTag:
    if not isinstance(value, Mapping):
        _input_fail(
            "artist_category_required",
            "画师标签必须来自已确认的建议或标签索引。",
            "artists",
        )
    if set(value) != {"name", "category"}:
        _input_fail(
            "artist_category_required",
            "画师标签必须同时包含名称和分类。",
            "artists",
        )
    return ConfirmedArtistTag(value["name"], value["category"])


def _query_tree_rows(root: QueryNode | None) -> list[dict[str, object]]:
    """Flatten a current tree for a future QML delegate without stale paths."""

    rows: list[dict[str, object]] = []

    def visit(node: QueryNode, path: tuple[int, ...], depth: int) -> None:
        if isinstance(node, TagNode):
            kind = "tag"
            label = node.name
            children: tuple[QueryNode, ...] = ()
        elif isinstance(node, NotNode):
            kind = "not"
            label = "NOT"
            children = (node.operand,)
        elif isinstance(node, AndNode):
            kind = "and"
            label = "AND"
            children = node.operands
        else:
            assert isinstance(node, OrNode)
            kind = "or"
            label = "OR"
            children = node.operands
        rows.append(
            {
                "path": list(path),
                "depth": depth,
                "kind": kind,
                "label": label,
                "childCount": len(children),
            }
        )
        for index, child in enumerate(children):
            visit(child, path + (index,), depth + 1)

    if root is not None:
        visit(root, (), 0)
    return rows
