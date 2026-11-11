"""Immutable logical query definitions shared by M2 controllers and history."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum

from danbooru_multi_search.danbooru.query_types import QueryExecutionLimits
from danbooru_multi_search.danbooru.query_language import parse_query_expression
from danbooru_multi_search.danbooru.query_planning import QueryRating, RemoteOrder
from danbooru_multi_search.danbooru.result_processing import (
    ResultFilterSpec,
    ResultSortSpec,
    validate_result_configuration,
)


class QueryMode(StrEnum):
    STANDARD = "standard"
    RANK = "rank"
    LATEST = "latest"


@dataclass(frozen=True, slots=True)
class GalleryQueryDefinition:
    """One normalized query identity without execution-specific state."""

    mode: QueryMode
    normalized_expression: str | None
    rating: QueryRating
    filter_spec: ResultFilterSpec
    sort_spec: ResultSortSpec
    limits: QueryExecutionLimits

    def __post_init__(self) -> None:
        if not isinstance(self.mode, QueryMode):
            raise TypeError("mode must be a QueryMode")
        if not isinstance(self.rating, QueryRating):
            raise TypeError("rating must be a QueryRating")
        if not isinstance(self.filter_spec, ResultFilterSpec):
            raise TypeError("filter_spec must be a ResultFilterSpec")
        if self.filter_spec.age_reference_time is not None:
            raise ValueError("logical query definitions cannot store an age reference")
        if not isinstance(self.sort_spec, ResultSortSpec):
            raise TypeError("sort_spec must be a ResultSortSpec")
        if not isinstance(self.limits, QueryExecutionLimits):
            raise TypeError("limits must be QueryExecutionLimits")
        if self.mode == QueryMode.STANDARD and self.normalized_expression is None:
            raise ValueError("standard queries require a normalized expression")
        if self.mode == QueryMode.LATEST and self.normalized_expression is not None:
            raise ValueError("latest queries cannot contain an expression")
        if self.normalized_expression is not None:
            if not isinstance(self.normalized_expression, str):
                raise TypeError("normalized_expression must be text or None")
            parsed = parse_query_expression(self.normalized_expression)
            if parsed.normalized_text != self.normalized_expression:
                raise ValueError("normalized_expression must use canonical query text")
        runtime_filter = self.filter_spec
        if (
            runtime_filter.minimum_age is not None
            or runtime_filter.maximum_age is not None
        ):
            runtime_filter = replace(
                runtime_filter,
                age_reference_time=datetime(2000, 1, 1, tzinfo=timezone.utc),
            )
        configuration = validate_result_configuration(
            runtime_filter,
            self.sort_spec,
            remote_order=(
                RemoteOrder.RANK if self.mode == QueryMode.RANK else None
            ),
        )
        logical_filter = replace(
            configuration.filter_spec,
            age_reference_time=None,
        )
        object.__setattr__(self, "filter_spec", logical_filter)
        object.__setattr__(self, "sort_spec", configuration.sort_spec)
