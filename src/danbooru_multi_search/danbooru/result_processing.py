"""Pure local filtering and sorting for completed query snapshot rows."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from fractions import Fraction
from numbers import Rational

from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.danbooru.query_types import (
    ACTIVE_EXECUTION_STATES,
    QueryExecutionSnapshot,
    QueryExecutionState,
)
from danbooru_multi_search.danbooru.query_planning import (
    QueryPlan,
    QueryRating,
    RemoteOrder,
)
from danbooru_multi_search.danbooru.tag_syntax import (
    TagSyntaxError,
    normalize_post_tag,
    normalize_tag_name,
)


class PostFileCategory(StrEnum):
    STATIC = "static"
    GIF = "gif"
    WEBM = "webm"
    MP4 = "mp4"


class PostOrientation(StrEnum):
    LANDSCAPE = "landscape"
    PORTRAIT = "portrait"
    SQUARE = "square"


class ResultSortField(StrEnum):
    SOURCE_ORDER = "source_order"
    SCORE = "score"
    FAV_COUNT = "fav_count"
    CREATED_AT = "created_at"
    POST_ID = "post_id"
    RANK = "rank"


class ResultSortDirection(StrEnum):
    ORIGINAL = "original"
    ASCENDING = "ascending"
    DESCENDING = "descending"


class ProcessedResultState(StrEnum):
    IDLE = "idle"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ResultProcessingDiagnostic:
    code: str
    message: str


class ResultProcessingError(ValueError):
    """Report one stable local result-processing error."""

    def __init__(self, diagnostic: ResultProcessingDiagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic

    @property
    def code(self) -> str:
        return self.diagnostic.code


@dataclass(frozen=True, slots=True)
class QueryResultSession:
    """Immutable execution identity and remote semantics for one query run."""

    execution_id: int
    rating: QueryRating
    remote_order: RemoteOrder | None


@dataclass(frozen=True, slots=True)
class ResultFilterSpec:
    """Immutable local filters; validation occurs in the processing entry point."""

    minimum_score: int | None = None
    minimum_fav_count: int | None = None
    published_from_utc: datetime | None = None
    published_until_utc: datetime | None = None
    minimum_age: timedelta | None = None
    maximum_age: timedelta | None = None
    age_reference_time: datetime | None = None
    file_categories: frozenset[PostFileCategory] | None = None
    minimum_width: int | None = None
    minimum_height: int | None = None
    minimum_aspect_ratio: Rational | None = None
    maximum_aspect_ratio: Rational | None = None
    orientations: frozenset[PostOrientation] | None = None
    blocked_tags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ResultSortSpec:
    field: ResultSortField = ResultSortField.SOURCE_ORDER
    direction: ResultSortDirection = ResultSortDirection.ORIGINAL


@dataclass(frozen=True, slots=True)
class ValidatedResultConfiguration:
    """Normalized public configuration ready for one execution or view."""

    filter_spec: ResultFilterSpec
    sort_spec: ResultSortSpec


@dataclass(frozen=True, slots=True)
class ProcessedPost:
    post: DanbooruPost
    session_rank: int | None


@dataclass(frozen=True, slots=True)
class ProcessedQueryResults:
    """A local view retaining the untouched execution snapshot as its source."""

    source_snapshot: QueryExecutionSnapshot
    items: tuple[ProcessedPost, ...]
    visible_count: int
    filtered_out_count: int
    result_state: ProcessedResultState
    range_description: str

    @property
    def posts(self) -> tuple[DanbooruPost, ...]:
        return tuple(item.post for item in self.items)


@dataclass(frozen=True, slots=True)
class _ValidatedFilter:
    minimum_score: int | None
    minimum_fav_count: int | None
    published_from_utc: datetime | None
    published_until_utc: datetime | None
    minimum_age: timedelta | None
    maximum_age: timedelta | None
    age_reference_time: datetime | None
    file_categories: frozenset[PostFileCategory] | None
    minimum_width: int | None
    minimum_height: int | None
    minimum_aspect_ratio: Fraction | None
    maximum_aspect_ratio: Fraction | None
    orientations: frozenset[PostOrientation] | None
    blocked_tags: frozenset[str]


@dataclass(frozen=True, slots=True)
class _ValidatedPost:
    post: DanbooruPost
    created_at_utc: datetime | None
    tags: frozenset[str]


_STATIC_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp", "avif"})
_RATING_VALUES = {
    QueryRating.GENERAL: "g",
    QueryRating.SENSITIVE: "s",
    QueryRating.QUESTIONABLE: "q",
    QueryRating.EXPLICIT: "e",
}
_PARTIAL_STATES = frozenset(
    {
        QueryExecutionState.PARTIAL_CANCELLED,
        QueryExecutionState.PARTIAL_LIMITED,
        QueryExecutionState.PARTIAL_FAILED,
    }
)


def build_query_result_session(
    execution_id: int,
    plan: QueryPlan,
) -> QueryResultSession:
    """Bind a validated query plan to the execution identifier returned at start."""

    if (
        not isinstance(execution_id, int)
        or isinstance(execution_id, bool)
        or execution_id < 1
    ):
        _fail("invalid_session", "查询结果会话的执行编号必须是正整数。")
    if not isinstance(plan, QueryPlan):
        _fail("invalid_session", "查询结果会话必须由有效查询计划建立。")
    return QueryResultSession(
        execution_id=execution_id,
        rating=plan.rating,
        remote_order=plan.remote_order,
    )


def process_query_snapshot(
    snapshot: QueryExecutionSnapshot,
    session: QueryResultSession,
    filter_spec: ResultFilterSpec | None = None,
    sort_spec: ResultSortSpec | None = None,
) -> ProcessedQueryResults:
    """Validate, locally filter, and deterministically sort one full snapshot."""

    _validate_session_and_snapshot(session, snapshot)
    configuration = validate_result_configuration(
        filter_spec,
        sort_spec,
        remote_order=session.remote_order,
    )
    validated_filter = _validate_filter_spec(configuration.filter_spec)
    validated_sort = configuration.sort_spec

    validated_posts = _validate_posts(snapshot.posts)
    ranked_session = session.remote_order == RemoteOrder.RANK
    retained: list[ProcessedPost] = []
    created_at_by_id: dict[int, datetime | None] = {}
    for position, validated_post in enumerate(validated_posts, start=1):
        created_at_by_id[validated_post.post.id] = validated_post.created_at_utc
        if not _matches_rating(validated_post.post, session.rating):
            continue
        if not _matches_filters(validated_post, validated_filter):
            continue
        retained.append(
            ProcessedPost(
                post=validated_post.post,
                session_rank=position if ranked_session else None,
            )
        )

    items = _sort_items(retained, validated_sort, created_at_by_id)
    return ProcessedQueryResults(
        source_snapshot=snapshot,
        items=items,
        visible_count=len(items),
        filtered_out_count=len(snapshot.posts) - len(items),
        result_state=_result_state(snapshot.state),
        range_description=_range_description(
            snapshot,
            ranked_session=ranked_session,
            sort_field=validated_sort.field,
        ),
    )


def validate_result_configuration(
    filter_spec: ResultFilterSpec | None = None,
    sort_spec: ResultSortSpec | None = None,
    *,
    remote_order: RemoteOrder | None,
) -> ValidatedResultConfiguration:
    """Validate in the fixed filter, sort, then rank-applicability order."""

    validated_filter = _validate_filter_spec(
        ResultFilterSpec() if filter_spec is None else filter_spec
    )
    validated_sort = _validate_sort_spec(
        ResultSortSpec() if sort_spec is None else sort_spec
    )
    if (
        validated_sort.field == ResultSortField.RANK
        and remote_order != RemoteOrder.RANK
    ):
        _fail(
            "rank_unavailable",
            "当前查询会话没有 Danbooru 近期热门顺序。",
        )
    return ValidatedResultConfiguration(
        filter_spec=_public_filter_spec(validated_filter),
        sort_spec=validated_sort,
    )


def _validate_session_and_snapshot(
    session: QueryResultSession,
    snapshot: QueryExecutionSnapshot,
) -> None:
    if not isinstance(session, QueryResultSession):
        _fail("invalid_session", "查询结果会话类型无效。")
    if (
        not isinstance(session.execution_id, int)
        or isinstance(session.execution_id, bool)
        or session.execution_id < 1
        or not isinstance(session.rating, QueryRating)
        or not (
            session.remote_order is None
            or (
                isinstance(session.remote_order, RemoteOrder)
                and session.remote_order == RemoteOrder.RANK
            )
        )
    ):
        _fail("invalid_session", "查询结果会话内容无效。")
    if not isinstance(snapshot, QueryExecutionSnapshot):
        _fail("invalid_session", "查询执行快照类型无效。")
    if (
        not isinstance(snapshot.execution_id, int)
        or isinstance(snapshot.execution_id, bool)
        or snapshot.execution_id < 1
    ):
        _fail("invalid_session", "查询执行快照的执行编号无效。")
    if snapshot.execution_id != session.execution_id:
        _fail("stale_snapshot", "查询执行快照不属于当前结果会话。")


def _validate_filter_spec(spec: ResultFilterSpec) -> _ValidatedFilter:
    if not isinstance(spec, ResultFilterSpec):
        _fail("invalid_filter", "本地筛选配置类型无效。")
    minimum_score = _optional_integer(
        spec.minimum_score,
        "最低得分",
        allow_negative=True,
    )
    minimum_fav_count = _optional_integer(
        spec.minimum_fav_count,
        "最低收藏数量",
        allow_negative=False,
    )
    published_from = _optional_utc_datetime(
        spec.published_from_utc,
        "发布时间起点",
    )
    published_until = _optional_utc_datetime(
        spec.published_until_utc,
        "发布时间终点",
    )
    if (
        published_from is not None
        and published_until is not None
        and published_from > published_until
    ):
        _fail("invalid_filter", "发布时间起点不能晚于终点。")

    minimum_age = _optional_nonnegative_timedelta(
        spec.minimum_age,
        "最小作品年龄",
    )
    maximum_age = _optional_nonnegative_timedelta(
        spec.maximum_age,
        "最大作品年龄",
    )
    if (
        minimum_age is not None
        and maximum_age is not None
        and minimum_age > maximum_age
    ):
        _fail("invalid_filter", "最小作品年龄不能大于最大作品年龄。")
    age_reference = _optional_utc_datetime(
        spec.age_reference_time,
        "作品年龄参考时间",
    )
    if (minimum_age is not None or maximum_age is not None) and age_reference is None:
        _fail("invalid_filter", "启用作品年龄筛选时必须提供参考时间。")

    file_categories = _optional_enum_set(
        spec.file_categories,
        PostFileCategory,
        "文件格式类别",
    )
    minimum_width = _optional_positive_integer(spec.minimum_width, "最低宽度")
    minimum_height = _optional_positive_integer(spec.minimum_height, "最低高度")
    minimum_aspect = _optional_positive_fraction(
        spec.minimum_aspect_ratio,
        "最小长宽比",
    )
    maximum_aspect = _optional_positive_fraction(
        spec.maximum_aspect_ratio,
        "最大长宽比",
    )
    if (
        minimum_aspect is not None
        and maximum_aspect is not None
        and minimum_aspect > maximum_aspect
    ):
        _fail("invalid_filter", "最小长宽比不能大于最大长宽比。")
    orientations = _optional_enum_set(
        spec.orientations,
        PostOrientation,
        "图片方向",
    )
    blocked_tags = _normalize_blocked_tags(spec.blocked_tags)
    return _ValidatedFilter(
        minimum_score=minimum_score,
        minimum_fav_count=minimum_fav_count,
        published_from_utc=published_from,
        published_until_utc=published_until,
        minimum_age=minimum_age,
        maximum_age=maximum_age,
        age_reference_time=age_reference,
        file_categories=file_categories,
        minimum_width=minimum_width,
        minimum_height=minimum_height,
        minimum_aspect_ratio=minimum_aspect,
        maximum_aspect_ratio=maximum_aspect,
        orientations=orientations,
        blocked_tags=blocked_tags,
    )


def _public_filter_spec(value: _ValidatedFilter) -> ResultFilterSpec:
    return ResultFilterSpec(
        minimum_score=value.minimum_score,
        minimum_fav_count=value.minimum_fav_count,
        published_from_utc=value.published_from_utc,
        published_until_utc=value.published_until_utc,
        minimum_age=value.minimum_age,
        maximum_age=value.maximum_age,
        age_reference_time=value.age_reference_time,
        file_categories=value.file_categories,
        minimum_width=value.minimum_width,
        minimum_height=value.minimum_height,
        minimum_aspect_ratio=value.minimum_aspect_ratio,
        maximum_aspect_ratio=value.maximum_aspect_ratio,
        orientations=value.orientations,
        blocked_tags=value.blocked_tags,
    )


def _validate_sort_spec(spec: ResultSortSpec) -> ResultSortSpec:
    if not isinstance(spec, ResultSortSpec):
        _fail("invalid_sort", "本地排序配置类型无效。")
    if not isinstance(spec.field, ResultSortField):
        _fail("invalid_sort", "本地排序字段无效。")
    if not isinstance(spec.direction, ResultSortDirection):
        _fail("invalid_sort", "本地排序方向无效。")
    original_only = {
        ResultSortField.SOURCE_ORDER,
        ResultSortField.RANK,
    }
    if spec.field in original_only:
        if spec.direction != ResultSortDirection.ORIGINAL:
            _fail("invalid_sort", "来源顺序和热门顺序只允许保留原顺序。")
    elif spec.direction not in {
        ResultSortDirection.ASCENDING,
        ResultSortDirection.DESCENDING,
    }:
        _fail("invalid_sort", "当前排序字段必须选择升序或降序。")
    return spec


def _validate_posts(posts: Sequence[DanbooruPost]) -> tuple[_ValidatedPost, ...]:
    if not isinstance(posts, tuple):
        _fail("invalid_post", "查询执行快照的作品集合必须是元组。")
    validated: list[_ValidatedPost] = []
    seen_ids: set[int] = set()
    for index, post in enumerate(posts):
        if not isinstance(post, DanbooruPost):
            _invalid_post(index, "作品类型无效")
        if (
            not isinstance(post.id, int)
            or isinstance(post.id, bool)
            or post.id < 1
        ):
            _invalid_post(index, "作品编号必须是正整数")
        if post.id in seen_ids:
            _invalid_post(index, "作品编号在执行快照中重复")
        seen_ids.add(post.id)
        if post.score is not None and (
            not isinstance(post.score, int) or isinstance(post.score, bool)
        ):
            _invalid_post(index, "得分必须是整数或缺失")
        if post.fav_count is not None and (
            not isinstance(post.fav_count, int)
            or isinstance(post.fav_count, bool)
            or post.fav_count < 0
        ):
            _invalid_post(index, "收藏数量必须是非负整数或缺失")
        if not isinstance(post.rating, str) or post.rating not in {
            "g",
            "s",
            "q",
            "e",
        }:
            _invalid_post(index, "内容分级无效")
        if (
            not isinstance(post.width, int)
            or isinstance(post.width, bool)
            or post.width < 1
        ):
            _invalid_post(index, "图片宽度必须是正整数")
        if (
            not isinstance(post.height, int)
            or isinstance(post.height, bool)
            or post.height < 1
        ):
            _invalid_post(index, "图片高度必须是正整数")
        if (
            not isinstance(post.file_ext, str)
            or not post.file_ext
            or post.file_ext != post.file_ext.strip().casefold()
        ):
            _invalid_post(index, "文件扩展名必须是规范化非空文本")
        try:
            created_at = _parse_post_created_at(post.created_at)
        except (TypeError, ValueError) as exc:
            _invalid_post(index, f"发布时间无效：{exc}")
        tags = _validated_post_tags(post, index)
        validated.append(_ValidatedPost(post, created_at, tags))
    return tuple(validated)


def _validated_post_tags(post: DanbooruPost, index: int) -> frozenset[str]:
    seen: set[str] = set()
    for group_name in (
        "artist_tags",
        "character_tags",
        "copyright_tags",
        "general_tags",
        "meta_tags",
    ):
        group = getattr(post, group_name)
        if not isinstance(group, tuple):
            _invalid_post(index, f"{group_name} 必须是标签元组")
        for tag in group:
            if not isinstance(tag, str):
                _invalid_post(index, f"{group_name} 包含非文本标签")
            try:
                normalized = normalize_post_tag(tag)
            except TagSyntaxError as exc:
                _invalid_post(index, f"{group_name} 包含非法标签：{exc}")
            if normalized != tag:
                _invalid_post(index, f"{group_name} 包含未规范化标签")
            if tag in seen:
                _invalid_post(index, f"标签 {tag} 在作品中重复")
            seen.add(tag)
    return frozenset(seen)


def _matches_rating(post: DanbooruPost, rating: QueryRating) -> bool:
    expected = _RATING_VALUES.get(rating)
    return expected is None or post.rating == expected


def _matches_filters(post: _ValidatedPost, spec: _ValidatedFilter) -> bool:
    value = post.post
    if spec.minimum_score is not None and (
        value.score is None or value.score < spec.minimum_score
    ):
        return False
    if spec.minimum_fav_count is not None and (
        value.fav_count is None or value.fav_count < spec.minimum_fav_count
    ):
        return False
    has_time_filter = (
        spec.published_from_utc is not None
        or spec.published_until_utc is not None
        or spec.minimum_age is not None
        or spec.maximum_age is not None
    )
    if has_time_filter and post.created_at_utc is None:
        return False
    if (
        spec.published_from_utc is not None
        and post.created_at_utc is not None
        and post.created_at_utc < spec.published_from_utc
    ):
        return False
    if (
        spec.published_until_utc is not None
        and post.created_at_utc is not None
        and post.created_at_utc > spec.published_until_utc
    ):
        return False
    if spec.minimum_age is not None or spec.maximum_age is not None:
        assert spec.age_reference_time is not None
        assert post.created_at_utc is not None
        age = spec.age_reference_time - post.created_at_utc
        if age < timedelta(0):
            return False
        if spec.minimum_age is not None and age < spec.minimum_age:
            return False
        if spec.maximum_age is not None and age > spec.maximum_age:
            return False
    if spec.file_categories is not None and not matches_file_category(
        value.file_ext,
        spec.file_categories,
    ):
        return False
    if spec.minimum_width is not None and value.width < spec.minimum_width:
        return False
    if spec.minimum_height is not None and value.height < spec.minimum_height:
        return False
    ratio = Fraction(value.width, value.height)
    if spec.minimum_aspect_ratio is not None and ratio < spec.minimum_aspect_ratio:
        return False
    if spec.maximum_aspect_ratio is not None and ratio > spec.maximum_aspect_ratio:
        return False
    if spec.orientations is not None:
        orientation = _post_orientation(value)
        if orientation not in spec.orientations:
            return False
    if spec.blocked_tags and post.tags & spec.blocked_tags:
        return False
    return True


def matches_file_category(
    extension: str,
    categories: frozenset[PostFileCategory],
) -> bool:
    """Match the shared static/GIF/WebM/MP4 categories for a file extension."""
    return (
        PostFileCategory.STATIC in categories and extension in _STATIC_EXTENSIONS
    ) or (
        PostFileCategory.GIF in categories and extension == "gif"
    ) or (
        PostFileCategory.WEBM in categories and extension == "webm"
    ) or (
        PostFileCategory.MP4 in categories and extension == "mp4"
    )


def _post_orientation(post: DanbooruPost) -> PostOrientation:
    if post.width > post.height:
        return PostOrientation.LANDSCAPE
    if post.height > post.width:
        return PostOrientation.PORTRAIT
    return PostOrientation.SQUARE


def _sort_items(
    items: list[ProcessedPost],
    spec: ResultSortSpec,
    created_at_by_id: dict[int, datetime | None],
) -> tuple[ProcessedPost, ...]:
    if spec.field in {ResultSortField.SOURCE_ORDER, ResultSortField.RANK}:
        return tuple(items)
    reverse = spec.direction == ResultSortDirection.DESCENDING
    if spec.field == ResultSortField.POST_ID:
        return tuple(
            sorted(items, key=lambda item: item.post.id, reverse=reverse)
        )

    by_id_descending = sorted(items, key=lambda item: item.post.id, reverse=True)
    if spec.field == ResultSortField.SCORE:
        value_of = lambda item: item.post.score
    elif spec.field == ResultSortField.FAV_COUNT:
        value_of = lambda item: item.post.fav_count
    else:
        value_of = lambda item: created_at_by_id[item.post.id]
    present = [item for item in by_id_descending if value_of(item) is not None]
    missing = [item for item in by_id_descending if value_of(item) is None]
    present.sort(key=value_of, reverse=reverse)
    return tuple(present + missing)


def _range_description(
    snapshot: QueryExecutionSnapshot,
    *,
    ranked_session: bool,
    sort_field: ResultSortField,
) -> str:
    if snapshot.state == QueryExecutionState.FAILED and not snapshot.posts:
        return ""
    scanned = snapshot.counts.unique_candidates
    if snapshot.posts:
        description = f"按已扫描的 {scanned} 个唯一候选排序"
        if ranked_session and sort_field in {
            ResultSortField.SOURCE_ORDER,
            ResultSortField.RANK,
        }:
            description += "；Danbooru 近期热门顺序（已扫描范围）"
        return description
    if scanned > 0:
        return f"已扫描 {scanned} 个唯一候选，当前没有表达式命中作品"
    return ""


def _result_state(state: QueryExecutionState) -> ProcessedResultState:
    if state in ACTIVE_EXECUTION_STATES:
        return ProcessedResultState.IN_PROGRESS
    if state in _PARTIAL_STATES:
        return ProcessedResultState.PARTIAL
    if state == QueryExecutionState.COMPLETE:
        return ProcessedResultState.COMPLETE
    if state == QueryExecutionState.FAILED:
        return ProcessedResultState.FAILED
    return ProcessedResultState.IDLE


def _optional_integer(
    value: int | None,
    label: str,
    *,
    allow_negative: bool,
) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        _fail("invalid_filter", f"{label}必须是整数或 None。")
    if not allow_negative and value < 0:
        _fail("invalid_filter", f"{label}不能是负数。")
    return value


def _optional_positive_integer(value: int | None, label: str) -> int | None:
    parsed = _optional_integer(value, label, allow_negative=False)
    if parsed is not None and parsed < 1:
        _fail("invalid_filter", f"{label}必须是正整数或 None。")
    return parsed


def _optional_nonnegative_timedelta(
    value: timedelta | None,
    label: str,
) -> timedelta | None:
    if value is None:
        return None
    if not isinstance(value, timedelta) or value < timedelta(0):
        _fail("invalid_filter", f"{label}必须是非负 timedelta 或 None。")
    return value


def _optional_utc_datetime(
    value: datetime | None,
    label: str,
) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        _fail("invalid_filter", f"{label}必须是带时区的 datetime 或 None。")
    try:
        offset = value.utcoffset()
    except (OverflowError, OSError, TypeError, ValueError) as exc:
        _fail("invalid_filter", f"{label}无法转换成 UTC：{exc}")
    if offset is None:
        _fail("invalid_filter", f"{label}必须包含时区。")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, OSError, TypeError, ValueError) as exc:
        _fail("invalid_filter", f"{label}无法转换成 UTC：{exc}")


def _optional_positive_fraction(
    value: Rational | None,
    label: str,
) -> Fraction | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Rational):
        _fail("invalid_filter", f"{label}必须是正有理数或 None。")
    try:
        parsed = Fraction(value)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        _fail("invalid_filter", f"{label}无效：{exc}")
    if parsed <= 0:
        _fail("invalid_filter", f"{label}必须大于零。")
    return parsed


def _optional_enum_set(value, enum_type, label: str):
    if value is None:
        return None
    if not isinstance(value, frozenset) or not value:
        _fail("invalid_filter", f"{label}必须是非空 frozenset 或 None。")
    if any(not isinstance(item, enum_type) for item in value):
        _fail("invalid_filter", f"{label}包含无效值。")
    return value


def _normalize_blocked_tags(value: frozenset[str]) -> frozenset[str]:
    if not isinstance(value, frozenset):
        _fail("invalid_filter", "屏蔽标签必须是 frozenset。")
    if any(not isinstance(tag, str) for tag in value):
        _fail("invalid_filter", "屏蔽标签必须全部是文本。")
    normalized: set[str] = set()
    for tag in sorted(value):
        try:
            normalized.add(normalize_tag_name(tag))
        except TagSyntaxError as exc:
            _fail("invalid_filter", f"屏蔽标签无效：{exc}")
    return frozenset(normalized)


def _parse_post_created_at(value: str | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("必须是规范化 UTC 文本")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("必须是有效的 ISO 8601 时间") from exc
    canonical = (
        f"{parsed.year:04d}-{parsed.month:02d}-{parsed.day:02d}T"
        f"{parsed.hour:02d}:{parsed.minute:02d}:{parsed.second:02d}."
        f"{parsed.microsecond:06d}Z"
    )
    if value != canonical:
        raise ValueError("必须使用固定六位小数的 UTC 格式")
    return parsed


def _invalid_post(index: int, message: str) -> None:
    _fail("invalid_post", f"索引 {index} 的作品无效：{message}。")


def _fail(code: str, message: str) -> None:
    raise ResultProcessingError(ResultProcessingDiagnostic(code, message))
