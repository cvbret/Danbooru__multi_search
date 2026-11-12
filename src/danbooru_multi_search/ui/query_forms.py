"""Strict query-form conversion and Qt local-time handling."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import math
import re

from PySide6.QtCore import (
    QDate,
    QDateTime,
    QTime,
    QTimeZone,
)

from danbooru_multi_search.danbooru.query_definition import (
    GalleryQueryDefinition,
    QueryMode,
)
from danbooru_multi_search.danbooru.query_language import (
    SourceSpan,
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
from danbooru_multi_search.danbooru.query_types import (
    DEFAULT_ACTIVE_TIME_LIMIT_SECONDS,
    DEFAULT_BRANCH_PAGE_LIMIT,
    DEFAULT_CANDIDATE_LIMIT,
    DEFAULT_EXECUTION_PAGE_SIZE,
    QueryExecutionLimits,
)
from danbooru_multi_search.danbooru.result_processing import (
    PostFileCategory,
    PostOrientation,
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
