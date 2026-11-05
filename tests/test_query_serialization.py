from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from fractions import Fraction

import pytest

from danbooru_multi_search.danbooru import (
    GalleryQueryDefinition,
    GalleryResultSummary,
    PostFileCategory,
    PostOrientation,
    QueryExecutionErrorCode,
    QueryExecutionLimits,
    QueryExecutionState,
    QueryLimitReason,
    QueryMode,
    QueryRating,
    ResultFilterSpec,
    ResultSortDirection,
    ResultSortField,
    ResultSortSpec,
    parse_query_expression,
)
from danbooru_multi_search.storage import (
    QuerySerializationError,
    decode_query_definition,
    decode_result_summary,
    encode_query_definition,
    encode_result_summary,
    query_definition_key,
    query_definition_tags,
)


def _definition(
    source: str | None = "cat AND dog",
    *,
    mode: QueryMode = QueryMode.STANDARD,
    filters: ResultFilterSpec | None = None,
    sorting: ResultSortSpec | None = None,
) -> GalleryQueryDefinition:
    expression = (
        parse_query_expression(source).normalized_text
        if source is not None
        else None
    )
    return GalleryQueryDefinition(
        mode=mode,
        normalized_expression=expression,
        rating=QueryRating.QUESTIONABLE,
        filter_spec=filters or ResultFilterSpec(),
        sort_spec=sorting or ResultSortSpec(),
        limits=QueryExecutionLimits(
            page_size=20,
            candidate_limit=100,
            branch_page_limit=3,
            active_time_limit_seconds=12.5,
        ),
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _summary(
    state: QueryExecutionState = QueryExecutionState.COMPLETE,
    *,
    limit_reason: QueryLimitReason | None = None,
    failure: QueryExecutionErrorCode | None = None,
    remote_rows: int = 10,
    unique_candidates: int = 8,
    local_matches: int = 6,
    visible_count: int = 4,
) -> GalleryResultSummary:
    return GalleryResultSummary(
        state=state,
        remote_rows=remote_rows,
        unique_candidates=unique_candidates,
        local_matches=local_matches,
        visible_count=visible_count,
        limit_reason=limit_reason,
        failure_error_code=failure,
    )


def test_full_definition_round_trip_is_canonical_and_immutable():
    definition = _definition(
        filters=ResultFilterSpec(
            minimum_score=-5,
            minimum_fav_count=0,
            published_from_utc=datetime(
                2020,
                1,
                2,
                3,
                4,
                5,
                6,
                tzinfo=timezone.utc,
            ),
            published_until_utc=datetime(
                2021,
                2,
                3,
                4,
                5,
                6,
                7,
                tzinfo=timezone.utc,
            ),
            minimum_age=timedelta(days=1, seconds=2, microseconds=3),
            maximum_age=timedelta(days=4, seconds=5, microseconds=6),
            file_categories=frozenset(
                {PostFileCategory.WEBM, PostFileCategory.STATIC}
            ),
            minimum_width=100,
            minimum_height=200,
            minimum_aspect_ratio=Fraction(1, 3),
            maximum_aspect_ratio=Fraction(16, 9),
            orientations=frozenset(
                {PostOrientation.PORTRAIT, PostOrientation.LANDSCAPE}
            ),
            blocked_tags=frozenset({"z_tag", "a_tag"}),
        ),
        sorting=ResultSortSpec(
            ResultSortField.SCORE,
            ResultSortDirection.DESCENDING,
        ),
    )

    encoded = encode_query_definition(definition)
    decoded = decode_query_definition(encoded)

    assert decoded == definition
    assert query_definition_key(definition) == encoded
    assert '"age_reference_time"' not in encoded
    assert '"active_time_limit_seconds":12.5' in encoded
    assert '"blocked_tags":["a_tag","z_tag"]' in encoded
    with pytest.raises(FrozenInstanceError):
        decoded.mode = QueryMode.LATEST


@pytest.mark.parametrize(
    "definition",
    [
        _definition("cat", mode=QueryMode.STANDARD),
        _definition(None, mode=QueryMode.RANK),
        _definition(None, mode=QueryMode.LATEST),
    ],
)
def test_three_query_modes_round_trip(definition):
    assert decode_query_definition(encode_query_definition(definition)) == definition


@pytest.mark.parametrize(
    "moment",
    [
        datetime(1, 1, 1, tzinfo=timezone.utc),
        datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc),
    ],
)
def test_utc_boundary_years_use_fixed_six_digit_fraction(moment):
    definition = _definition(
        filters=ResultFilterSpec(published_from_utc=moment)
    )

    assert decode_query_definition(encode_query_definition(definition)) == definition


def test_offset_datetime_is_canonicalized_to_utc_identity():
    offset_time = datetime(
        2024,
        2,
        3,
        4,
        5,
        6,
        tzinfo=timezone(timedelta(hours=5, minutes=30)),
    )
    offset_definition = _definition(
        filters=ResultFilterSpec(published_from_utc=offset_time)
    )
    utc_definition = _definition(
        filters=ResultFilterSpec(
            published_from_utc=offset_time.astimezone(timezone.utc)
        )
    )

    encoded = encode_query_definition(offset_definition)

    assert query_definition_key(offset_definition) == query_definition_key(
        utc_definition
    )
    assert decode_query_definition(encoded) == utc_definition


def test_rank_mode_with_rank_sort_round_trips():
    definition = _definition(
        None,
        mode=QueryMode.RANK,
        sorting=ResultSortSpec(
            ResultSortField.RANK,
            ResultSortDirection.ORIGINAL,
        ),
    )

    assert decode_query_definition(encode_query_definition(definition)) == definition


def test_expression_structure_and_order_remain_part_of_query_identity():
    definitions = (
        _definition("cat AND dog"),
        _definition("cat OR dog"),
        _definition("cat AND NOT dog"),
        _definition("dog AND cat"),
        _definition("cat AND cat AND dog"),
        _definition("NOT NOT cat AND dog"),
    )

    assert len({query_definition_key(item) for item in definitions}) == len(definitions)
    assert all(query_definition_tags(item) == ("cat", "dog") for item in definitions)


def test_parser_normalization_makes_ascii_case_and_spacing_identity_equal():
    first = _definition(" CAT   and DOG ")
    second = _definition("cat AND dog")

    assert first == second
    assert query_definition_key(first) == query_definition_key(second)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda text: " " + text,
        lambda text: text.replace('"version":1', '"version":1,"version":1', 1),
        lambda text: text[:-1] + ',"unknown":0}',
        lambda text: text[:-1] + ',"unknown":NaN}',
        lambda text: text[:-1] + ',"unknown":-Infinity}',
        lambda text: text.replace('"page_size":20', '"page_size":true'),
        lambda text: text.replace(
            '"active_time_limit_seconds":12.5',
            '"active_time_limit_seconds":false',
        ),
        lambda text: text.replace(
            '"active_time_limit_seconds":12.5',
            '"active_time_limit_seconds":Infinity',
        ),
        lambda text: text.replace('"version":1', '"version":2', 1),
        lambda text: text.replace('"mode":"standard",', ""),
    ],
)
def test_definition_decoder_rejects_noncanonical_or_invalid_json(mutator):
    encoded = encode_query_definition(_definition())

    with pytest.raises(QuerySerializationError):
        decode_query_definition(mutator(encoded))


def test_nested_duplicate_key_is_rejected():
    encoded = encode_query_definition(_definition())
    damaged = encoded.replace(
        '"minimum_score":null',
        '"minimum_score":null,"minimum_score":null',
    )

    with pytest.raises(QuerySerializationError, match="duplicate"):
        decode_query_definition(damaged)


def test_noncanonical_fraction_duration_time_and_array_are_rejected():
    definition = _definition(
        filters=ResultFilterSpec(
            published_from_utc=datetime(2020, 1, 1, tzinfo=timezone.utc),
            minimum_age=timedelta(days=1),
            minimum_aspect_ratio=Fraction(1, 2),
            blocked_tags=frozenset({"a", "b"}),
        )
    )
    payload = json.loads(encode_query_definition(definition))
    variants = []

    fraction = json.loads(json.dumps(payload))
    fraction["filter"]["minimum_aspect_ratio"] = {
        "denominator": 4,
        "numerator": 2,
    }
    variants.append(fraction)

    duration = json.loads(json.dumps(payload))
    duration["filter"]["minimum_age"] = {
        "days": 0,
        "microseconds": 0,
        "seconds": 86_400,
    }
    variants.append(duration)

    timestamp = json.loads(json.dumps(payload))
    timestamp["filter"]["published_from_utc"] = "2020-01-01T00:00:00Z"
    variants.append(timestamp)

    array = json.loads(json.dumps(payload))
    array["filter"]["blocked_tags"] = ["b", "a"]
    variants.append(array)

    for variant in variants:
        with pytest.raises(QuerySerializationError):
            decode_query_definition(_canonical_json(variant))


def test_integer_form_of_active_seconds_and_extreme_integer_are_rejected():
    encoded = encode_query_definition(
        GalleryQueryDefinition(
            QueryMode.STANDARD,
            "cat",
            QueryRating.ALL,
            ResultFilterSpec(),
            ResultSortSpec(),
            QueryExecutionLimits(),
        )
    )
    integer_seconds = encoded.replace(
        '"active_time_limit_seconds":60.0',
        '"active_time_limit_seconds":60',
    )
    huge_integer = encoded.replace('"page_size":200', '"page_size":' + "9" * 5000)

    with pytest.raises(QuerySerializationError):
        decode_query_definition(integer_seconds)
    with pytest.raises(QuerySerializationError):
        decode_query_definition(huge_integer)


def test_deep_json_is_converted_to_stable_serialization_error():
    encoded = encode_query_definition(_definition())
    damaged = encoded[:-1] + ',"unknown":' + "[" * 2000 + "0" + "]" * 2000 + "}"

    with pytest.raises(QuerySerializationError):
        decode_query_definition(damaged)


@pytest.mark.parametrize(
    "summary",
    [
        _summary(QueryExecutionState.WAITING),
        _summary(QueryExecutionState.RUNNING),
        _summary(QueryExecutionState.RETRY_WAIT),
        _summary(QueryExecutionState.COMPLETE),
        _summary(QueryExecutionState.PARTIAL_CANCELLED),
        _summary(
            QueryExecutionState.PARTIAL_LIMITED,
            limit_reason=QueryLimitReason.PAGE_LIMIT,
        ),
        _summary(
            QueryExecutionState.PARTIAL_FAILED,
            failure=QueryExecutionErrorCode.HTTP,
        ),
        _summary(
            QueryExecutionState.FAILED,
            failure=QueryExecutionErrorCode.INVALID_RESPONSE,
            local_matches=0,
            visible_count=0,
        ),
    ],
)
def test_result_summary_round_trip(summary):
    encoded = encode_result_summary(summary)

    assert decode_result_summary(encoded) == summary


@pytest.mark.parametrize(
    "summary",
    [
        GalleryResultSummary(
            QueryExecutionState.COMPLETE,
            True,
            0,
            0,
            0,
            None,
            None,
        ),
        GalleryResultSummary(
            QueryExecutionState.COMPLETE,
            1,
            2,
            0,
            0,
            None,
            None,
        ),
        _summary(
            QueryExecutionState.COMPLETE,
            limit_reason=QueryLimitReason.PAGE_LIMIT,
        ),
        _summary(QueryExecutionState.FAILED),
        _summary(QueryExecutionState.IDLE),
        _summary(
            QueryExecutionState.FAILED,
            failure=QueryExecutionErrorCode.HTTP,
        ),
        _summary(
            QueryExecutionState.PARTIAL_FAILED,
            failure=QueryExecutionErrorCode.HTTP,
            local_matches=0,
            visible_count=0,
        ),
    ],
)
def test_result_summary_encoder_rejects_invalid_values(summary):
    with pytest.raises((QuerySerializationError, TypeError)):
        encode_result_summary(summary)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda text: "\n" + text,
        lambda text: text.replace('"version":1', '"version":true'),
        lambda text: text.replace('"remote_rows":10', '"remote_rows":NaN'),
        lambda text: text[:-1] + ',"unknown":Infinity}',
        lambda text: text.replace('"state":"complete"', '"state":"unknown"'),
        lambda text: text.replace(
            '"visible_count":4',
            '"visible_count":4,"visible_count":4',
        ),
    ],
)
def test_result_summary_decoder_rejects_strict_json_violations(mutator):
    encoded = encode_result_summary(_summary())

    with pytest.raises(QuerySerializationError):
        decode_result_summary(mutator(encoded))
