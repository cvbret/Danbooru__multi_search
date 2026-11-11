"""Canonical JSON identities for M2 query definitions and result summaries."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from typing import Any

from danbooru_multi_search.danbooru.query_definition import (
    GalleryQueryDefinition,
    QueryMode,
)
from danbooru_multi_search.danbooru.query_types import (
    GalleryResultSummary,
    QueryExecutionErrorCode,
    QueryExecutionLimits,
    QueryExecutionState,
    QueryLimitReason,
)
from danbooru_multi_search.danbooru.query_language import (
    analyze_query,
    parse_query_expression,
)
from danbooru_multi_search.danbooru.query_planning import QueryRating
from danbooru_multi_search.danbooru.result_processing import (
    PostFileCategory,
    PostOrientation,
    ResultFilterSpec,
    ResultSortDirection,
    ResultSortField,
    ResultSortSpec,
)
from danbooru_multi_search.danbooru.tag_syntax import normalize_tag_name


QUERY_DEFINITION_VERSION = 1
RESULT_SUMMARY_VERSION = 1
_UTC_PATTERN = re.compile(
    r"(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"T(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"\.(?P<microsecond>[0-9]{6})Z"
)

_DEFINITION_FIELDS = frozenset(
    {
        "filter",
        "limits",
        "mode",
        "normalized_expression",
        "rating",
        "sort",
        "version",
    }
)
_FILTER_FIELDS = frozenset(
    {
        "blocked_tags",
        "file_categories",
        "maximum_age",
        "maximum_aspect_ratio",
        "minimum_age",
        "minimum_aspect_ratio",
        "minimum_fav_count",
        "minimum_height",
        "minimum_score",
        "minimum_width",
        "orientations",
        "published_from_utc",
        "published_until_utc",
    }
)
_SORT_FIELDS = frozenset({"direction", "field"})
_LIMIT_FIELDS = frozenset(
    {
        "active_time_limit_seconds",
        "branch_page_limit",
        "candidate_limit",
        "page_size",
    }
)
_TIMEDELTA_FIELDS = frozenset({"days", "microseconds", "seconds"})
_FRACTION_FIELDS = frozenset({"denominator", "numerator"})
_SUMMARY_FIELDS = frozenset(
    {
        "failure_error_code",
        "limit_reason",
        "local_matches",
        "remote_rows",
        "state",
        "unique_candidates",
        "version",
        "visible_count",
    }
)


class QuerySerializationError(ValueError):
    """Report one strict query-definition or result-summary format error."""


def encode_query_definition(definition: GalleryQueryDefinition) -> str:
    """Return the sole canonical version-1 JSON representation."""

    if not isinstance(definition, GalleryQueryDefinition):
        raise TypeError("definition must be a GalleryQueryDefinition")
    filter_spec = definition.filter_spec
    if filter_spec.age_reference_time is not None:
        raise QuerySerializationError("Query definitions cannot store age_reference_time")
    payload = {
        "filter": {
            "blocked_tags": sorted(filter_spec.blocked_tags),
            "file_categories": _encode_enum_set(filter_spec.file_categories),
            "maximum_age": _encode_timedelta(filter_spec.maximum_age),
            "maximum_aspect_ratio": _encode_fraction(
                filter_spec.maximum_aspect_ratio
            ),
            "minimum_age": _encode_timedelta(filter_spec.minimum_age),
            "minimum_aspect_ratio": _encode_fraction(
                filter_spec.minimum_aspect_ratio
            ),
            "minimum_fav_count": filter_spec.minimum_fav_count,
            "minimum_height": filter_spec.minimum_height,
            "minimum_score": filter_spec.minimum_score,
            "minimum_width": filter_spec.minimum_width,
            "orientations": _encode_enum_set(filter_spec.orientations),
            "published_from_utc": _encode_datetime(
                filter_spec.published_from_utc
            ),
            "published_until_utc": _encode_datetime(
                filter_spec.published_until_utc
            ),
        },
        "limits": {
            "active_time_limit_seconds": float(
                definition.limits.active_time_limit_seconds
            ),
            "branch_page_limit": definition.limits.branch_page_limit,
            "candidate_limit": definition.limits.candidate_limit,
            "page_size": definition.limits.page_size,
        },
        "mode": definition.mode.value,
        "normalized_expression": definition.normalized_expression,
        "rating": definition.rating.value,
        "sort": {
            "direction": definition.sort_spec.direction.value,
            "field": definition.sort_spec.field.value,
        },
        "version": QUERY_DEFINITION_VERSION,
    }
    return _dump_canonical(payload)


def decode_query_definition(encoded: str) -> GalleryQueryDefinition:
    """Decode canonical JSON and reject any noncanonical equivalent text."""

    payload = _load_object(encoded, "query definition")
    _expect_fields(payload, _DEFINITION_FIELDS, "query definition")
    _expect_version(payload["version"], QUERY_DEFINITION_VERSION, "definition")
    try:
        mode = QueryMode(_expect_text(payload["mode"], "mode"))
        expression = payload["normalized_expression"]
        if expression is not None and not isinstance(expression, str):
            raise QuerySerializationError(
                "normalized_expression must be text or null"
            )
        rating = QueryRating(_expect_text(payload["rating"], "rating"))
        filter_spec = _decode_filter(payload["filter"])
        sort_spec = _decode_sort(payload["sort"])
        limits = _decode_limits(payload["limits"])
        definition = GalleryQueryDefinition(
            mode=mode,
            normalized_expression=expression,
            rating=rating,
            filter_spec=filter_spec,
            sort_spec=sort_spec,
            limits=limits,
        )
    except QuerySerializationError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise QuerySerializationError(f"Invalid query definition: {exc}") from exc
    if encode_query_definition(definition) != encoded:
        raise QuerySerializationError("Query definition JSON is not canonical")
    return definition


def query_definition_key(definition: GalleryQueryDefinition) -> str:
    """Use canonical definition JSON directly as the collision-free identity."""

    return encode_query_definition(definition)


def query_definition_tags(
    definition: GalleryQueryDefinition,
) -> tuple[str, ...]:
    """Return every unique expression tag, including tags under NOT."""

    if not isinstance(definition, GalleryQueryDefinition):
        raise TypeError("definition must be a GalleryQueryDefinition")
    expression = definition.normalized_expression
    if expression is None:
        return ()
    analysis = analyze_query(parse_query_expression(expression))
    return tuple(sorted(set(analysis.positive_tags + analysis.negative_tags)))


def encode_result_summary(summary: GalleryResultSummary) -> str:
    """Return canonical version-1 JSON after validating summary invariants."""

    _validate_summary(summary)
    payload = {
        "failure_error_code": (
            summary.failure_error_code.value
            if summary.failure_error_code is not None
            else None
        ),
        "limit_reason": (
            summary.limit_reason.value if summary.limit_reason is not None else None
        ),
        "local_matches": summary.local_matches,
        "remote_rows": summary.remote_rows,
        "state": summary.state.value,
        "unique_candidates": summary.unique_candidates,
        "version": RESULT_SUMMARY_VERSION,
        "visible_count": summary.visible_count,
    }
    return _dump_canonical(payload)


def decode_result_summary(encoded: str) -> GalleryResultSummary:
    """Decode one canonical result summary with strict enum and number checks."""

    payload = _load_object(encoded, "result summary")
    _expect_fields(payload, _SUMMARY_FIELDS, "result summary")
    _expect_version(payload["version"], RESULT_SUMMARY_VERSION, "summary")
    try:
        state = QueryExecutionState(_expect_text(payload["state"], "state"))
        limit_value = payload["limit_reason"]
        failure_value = payload["failure_error_code"]
        summary = GalleryResultSummary(
            state=state,
            remote_rows=_nonnegative_integer(
                payload["remote_rows"],
                "remote_rows",
            ),
            unique_candidates=_nonnegative_integer(
                payload["unique_candidates"],
                "unique_candidates",
            ),
            local_matches=_nonnegative_integer(
                payload["local_matches"],
                "local_matches",
            ),
            visible_count=_nonnegative_integer(
                payload["visible_count"],
                "visible_count",
            ),
            limit_reason=(
                None
                if limit_value is None
                else QueryLimitReason(_expect_text(limit_value, "limit_reason"))
            ),
            failure_error_code=(
                None
                if failure_value is None
                else QueryExecutionErrorCode(
                    _expect_text(failure_value, "failure_error_code")
                )
            ),
        )
        _validate_summary(summary)
    except QuerySerializationError:
        raise
    except (TypeError, ValueError) as exc:
        raise QuerySerializationError(f"Invalid result summary: {exc}") from exc
    if encode_result_summary(summary) != encoded:
        raise QuerySerializationError("Result summary JSON is not canonical")
    return summary


def encode_normalized_tags(tags: tuple[str, ...]) -> str:
    """Encode a sorted unique tag tuple; empty tuples are valid for M2."""

    canonical = _canonical_tags(tags)
    return _dump_canonical(list(canonical))


def decode_normalized_tags(
    encoded: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    """Strictly decode the redundant normalized-tag array."""

    payload = _load_json(encoded, "normalized tags")
    if not isinstance(payload, list) or any(
        not isinstance(item, str) for item in payload
    ):
        raise QuerySerializationError("Normalized tags must be a JSON string array")
    try:
        canonical = _canonical_tags(tuple(payload))
    except (TypeError, ValueError) as exc:
        raise QuerySerializationError(f"Invalid normalized tags: {exc}") from exc
    if not allow_empty and not canonical:
        raise QuerySerializationError("At least one normalized tag is required")
    if encode_normalized_tags(canonical) != encoded:
        raise QuerySerializationError("Normalized tags JSON is not canonical")
    return canonical


def _decode_filter(value: object) -> ResultFilterSpec:
    mapping = _expect_object(value, "filter")
    _expect_fields(mapping, _FILTER_FIELDS, "filter")
    return ResultFilterSpec(
        minimum_score=_optional_integer(mapping["minimum_score"], "minimum_score"),
        minimum_fav_count=_optional_integer(
            mapping["minimum_fav_count"],
            "minimum_fav_count",
        ),
        published_from_utc=_decode_datetime(
            mapping["published_from_utc"],
            "published_from_utc",
        ),
        published_until_utc=_decode_datetime(
            mapping["published_until_utc"],
            "published_until_utc",
        ),
        minimum_age=_decode_timedelta(mapping["minimum_age"], "minimum_age"),
        maximum_age=_decode_timedelta(mapping["maximum_age"], "maximum_age"),
        age_reference_time=None,
        file_categories=_decode_enum_set(
            mapping["file_categories"],
            PostFileCategory,
            "file_categories",
        ),
        minimum_width=_optional_integer(mapping["minimum_width"], "minimum_width"),
        minimum_height=_optional_integer(
            mapping["minimum_height"],
            "minimum_height",
        ),
        minimum_aspect_ratio=_decode_fraction(
            mapping["minimum_aspect_ratio"],
            "minimum_aspect_ratio",
        ),
        maximum_aspect_ratio=_decode_fraction(
            mapping["maximum_aspect_ratio"],
            "maximum_aspect_ratio",
        ),
        orientations=_decode_enum_set(
            mapping["orientations"],
            PostOrientation,
            "orientations",
        ),
        blocked_tags=frozenset(
            _expect_text_list(mapping["blocked_tags"], "blocked_tags")
        ),
    )


def _decode_sort(value: object) -> ResultSortSpec:
    mapping = _expect_object(value, "sort")
    _expect_fields(mapping, _SORT_FIELDS, "sort")
    try:
        return ResultSortSpec(
            field=ResultSortField(_expect_text(mapping["field"], "sort.field")),
            direction=ResultSortDirection(
                _expect_text(mapping["direction"], "sort.direction")
            ),
        )
    except ValueError as exc:
        raise QuerySerializationError(f"Invalid sort configuration: {exc}") from exc


def _decode_limits(value: object) -> QueryExecutionLimits:
    mapping = _expect_object(value, "limits")
    _expect_fields(mapping, _LIMIT_FIELDS, "limits")
    seconds = mapping["active_time_limit_seconds"]
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise QuerySerializationError(
            "active_time_limit_seconds must be a finite JSON number"
        )
    if not math.isfinite(float(seconds)):
        raise QuerySerializationError(
            "active_time_limit_seconds must be a finite JSON number"
        )
    return QueryExecutionLimits(
        page_size=_integer(mapping["page_size"], "page_size"),
        candidate_limit=_integer(mapping["candidate_limit"], "candidate_limit"),
        branch_page_limit=_integer(
            mapping["branch_page_limit"],
            "branch_page_limit",
        ),
        active_time_limit_seconds=float(seconds),
    )


def _validate_summary(summary: GalleryResultSummary) -> None:
    if not isinstance(summary, GalleryResultSummary):
        raise TypeError("summary must be a GalleryResultSummary")
    if not isinstance(summary.state, QueryExecutionState):
        raise QuerySerializationError("summary state is invalid")
    remote_rows = _nonnegative_integer(summary.remote_rows, "remote_rows")
    unique = _nonnegative_integer(summary.unique_candidates, "unique_candidates")
    local = _nonnegative_integer(summary.local_matches, "local_matches")
    visible = _nonnegative_integer(summary.visible_count, "visible_count")
    if not remote_rows >= unique >= local >= visible:
        raise QuerySerializationError(
            "Summary counts must satisfy remote >= unique >= local >= visible"
        )
    if summary.state == QueryExecutionState.IDLE:
        raise QuerySerializationError(
            "Accepted query summaries cannot use the idle state"
        )
    if summary.state == QueryExecutionState.PARTIAL_LIMITED:
        if not isinstance(summary.limit_reason, QueryLimitReason):
            raise QuerySerializationError(
                "partial_limited summaries require limit_reason"
            )
    elif summary.limit_reason is not None:
        raise QuerySerializationError(
            "Only partial_limited summaries may contain limit_reason"
        )
    failed_states = {
        QueryExecutionState.FAILED,
        QueryExecutionState.PARTIAL_FAILED,
    }
    if summary.state in failed_states:
        if not isinstance(summary.failure_error_code, QueryExecutionErrorCode):
            raise QuerySerializationError(
                "Failed summaries require failure_error_code"
            )
        if summary.state == QueryExecutionState.FAILED and local != 0:
            raise QuerySerializationError(
                "failed summaries cannot contain local matches"
            )
        if summary.state == QueryExecutionState.PARTIAL_FAILED and local == 0:
            raise QuerySerializationError(
                "partial_failed summaries require local matches"
            )
    elif summary.failure_error_code is not None:
        raise QuerySerializationError(
            "Only failed summaries may contain failure_error_code"
        )


def _encode_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise QuerySerializationError("Stored datetimes must be timezone-aware")
    try:
        utc = value.astimezone(timezone.utc)
    except (OverflowError, OSError, TypeError, ValueError) as exc:
        raise QuerySerializationError(f"Stored datetime cannot become UTC: {exc}") from exc
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}T"
        f"{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}."
        f"{utc.microsecond:06d}Z"
    )


def _decode_datetime(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    text = _expect_text(value, label)
    match = _UTC_PATTERN.fullmatch(text)
    if match is None:
        raise QuerySerializationError(f"{label} must use fixed UTC text")
    try:
        return datetime(
            *(int(match[name]) for name in (
                "year",
                "month",
                "day",
                "hour",
                "minute",
                "second",
                "microsecond",
            )),
            tzinfo=timezone.utc,
        )
    except ValueError as exc:
        raise QuerySerializationError(f"{label} is not a valid UTC datetime") from exc


def _encode_timedelta(value: timedelta | None) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, timedelta):
        raise QuerySerializationError("Stored ages must be timedelta values")
    return {
        "days": value.days,
        "microseconds": value.microseconds,
        "seconds": value.seconds,
    }


def _decode_timedelta(value: object, label: str) -> timedelta | None:
    if value is None:
        return None
    mapping = _expect_object(value, label)
    _expect_fields(mapping, _TIMEDELTA_FIELDS, label)
    days = _integer(mapping["days"], f"{label}.days")
    seconds = _integer(mapping["seconds"], f"{label}.seconds")
    microseconds = _integer(
        mapping["microseconds"],
        f"{label}.microseconds",
    )
    if days < 0 or not 0 <= seconds < 86_400 or not 0 <= microseconds < 1_000_000:
        raise QuerySerializationError(f"{label} is not a canonical nonnegative duration")
    return timedelta(days=days, seconds=seconds, microseconds=microseconds)


def _encode_fraction(value: object) -> dict[str, int] | None:
    if value is None:
        return None
    try:
        fraction = Fraction(value)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise QuerySerializationError(f"Stored aspect ratio is invalid: {exc}") from exc
    return {
        "denominator": fraction.denominator,
        "numerator": fraction.numerator,
    }


def _decode_fraction(value: object, label: str) -> Fraction | None:
    if value is None:
        return None
    mapping = _expect_object(value, label)
    _expect_fields(mapping, _FRACTION_FIELDS, label)
    numerator = _integer(mapping["numerator"], f"{label}.numerator")
    denominator = _integer(mapping["denominator"], f"{label}.denominator")
    if numerator <= 0 or denominator <= 0:
        raise QuerySerializationError(f"{label} must be a positive fraction")
    return Fraction(numerator, denominator)


def _encode_enum_set(value: frozenset | None) -> list[str] | None:
    if value is None:
        return None
    return sorted(item.value for item in value)


def _decode_enum_set(value: object, enum_type, label: str):
    if value is None:
        return None
    items = _expect_text_list(value, label)
    try:
        return frozenset(enum_type(item) for item in items)
    except ValueError as exc:
        raise QuerySerializationError(f"{label} contains an unknown value") from exc


def _canonical_tags(tags: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(tags, tuple):
        raise TypeError("tags must be a tuple")
    return tuple(sorted({normalize_tag_name(tag) for tag in tags}))


def _dump_canonical(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise QuerySerializationError(f"Unable to encode canonical JSON: {exc}") from exc


def _load_object(encoded: str, label: str) -> dict[str, Any]:
    value = _load_json(encoded, label)
    return _expect_object(value, label)


def _load_json(encoded: str, label: str) -> object:
    if not isinstance(encoded, str):
        raise TypeError(f"{label} JSON must be text")

    def reject_constant(value: str):
        raise QuerySerializationError(f"{label} JSON contains {value}")

    def reject_duplicate_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise QuerySerializationError(
                    f"{label} JSON contains duplicate key {key!r}"
                )
            result[key] = value
        return result

    def parse_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise QuerySerializationError(f"{label} JSON contains a non-finite number")
        return parsed

    try:
        return json.loads(
            encoded,
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_constant,
            parse_float=parse_float,
        )
    except QuerySerializationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise QuerySerializationError(f"Invalid {label} JSON: {exc}") from exc


def _expect_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise QuerySerializationError(f"{label} must be a JSON object")
    return value


def _expect_fields(
    mapping: dict[str, Any],
    expected: frozenset[str],
    label: str,
) -> None:
    actual = frozenset(mapping)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise QuerySerializationError(
            f"{label} fields are invalid; missing={missing}, unknown={unknown}"
        )


def _expect_version(value: object, expected: int, label: str) -> None:
    if type(value) is not int or value != expected:
        raise QuerySerializationError(f"Unsupported {label} version")


def _expect_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise QuerySerializationError(f"{label} must be text")
    return value


def _expect_text_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise QuerySerializationError(f"{label} must be a JSON string array")
    return tuple(value)


def _integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise QuerySerializationError(f"{label} must be an integer")
    return value


def _nonnegative_integer(value: object, label: str) -> int:
    parsed = _integer(value, label)
    if parsed < 0:
        raise QuerySerializationError(f"{label} cannot be negative")
    return parsed


def _optional_integer(value: object, label: str) -> int | None:
    return None if value is None else _integer(value, label)
