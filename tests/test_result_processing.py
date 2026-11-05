from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from fractions import Fraction

import pytest

from danbooru_multi_search.danbooru import (
    PostFileCategory,
    PostOrientation,
    ProcessedResultState,
    QueryExecutionCounts,
    QueryExecutionFailure,
    QueryExecutionSnapshot,
    QueryExecutionState,
    QueryExecutionErrorCode,
    QueryLimitReason,
    QueryRating,
    QueryResultSession,
    RemoteOrder,
    ResultFilterSpec,
    ResultProcessingError,
    ResultSortDirection,
    ResultSortField,
    ResultSortSpec,
    build_query_result_session,
    build_rank_query_plan,
    build_standard_query_plan,
    parse_query_expression,
    process_query_snapshot,
)
from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.danbooru.query_execution import QueryBranchPosition


EXECUTION_ID = 41


def _post(post_id: int, **changes) -> DanbooruPost:
    values = {
        "id": post_id,
        "preview_url": f"https://example.invalid/{post_id}.jpg",
        "large_file_url": None,
        "file_url": f"https://example.invalid/{post_id}-full.jpg",
        "score": 0,
        "fav_count": 0,
        "rating": "g",
        "width": 100,
        "height": 100,
        "file_ext": "jpg",
        "created_at": "2024-01-02T03:04:05.000000Z",
        "file_size": 100,
        "source": None,
        "artist_tags": (),
        "character_tags": (),
        "copyright_tags": (),
        "general_tags": ("cat",),
        "meta_tags": (),
    }
    values.update(changes)
    return DanbooruPost(**values)


def _snapshot(
    posts=(),
    *,
    execution_id: int = EXECUTION_ID,
    state: QueryExecutionState = QueryExecutionState.COMPLETE,
    unique_candidates: int | None = None,
    limit_reason: QueryLimitReason | None = None,
    failure: QueryExecutionFailure | None = None,
) -> QueryExecutionSnapshot:
    posts = tuple(posts)
    unique = len(posts) if unique_candidates is None else unique_candidates
    return QueryExecutionSnapshot(
        execution_id=execution_id,
        state=state,
        counts=QueryExecutionCounts(
            remote_rows=unique,
            unique_candidates=unique,
            local_matches=len(posts),
        ),
        posts=posts,
        branch_progress=(),
        limit_reason=limit_reason,
        failure=failure,
    )


def _session(
    *,
    execution_id: int = EXECUTION_ID,
    rating: QueryRating = QueryRating.ALL,
    rank: bool = False,
) -> QueryResultSession:
    return QueryResultSession(
        execution_id=execution_id,
        rating=rating,
        remote_order=RemoteOrder.RANK if rank else None,
    )


def _ids(result) -> list[int]:
    return [item.post.id for item in result.items]


def _process(
    posts,
    *,
    filters: ResultFilterSpec | None = None,
    sorting: ResultSortSpec | None = None,
    session: QueryResultSession | None = None,
):
    return process_query_snapshot(
        _snapshot(posts),
        session or _session(),
        filters,
        sorting,
    )


def test_build_session_retains_plan_rating_and_remote_order():
    ordinary = build_standard_query_plan(
        parse_query_expression("cat"),
        rating="sensitive",
    )
    ranked = build_rank_query_plan(None, rating="explicit")

    assert build_query_result_session(3, ordinary) == QueryResultSession(
        3,
        QueryRating.SENSITIVE,
        None,
    )
    assert build_query_result_session(4, ranked) == QueryResultSession(
        4,
        QueryRating.EXPLICIT,
        RemoteOrder.RANK,
    )


@pytest.mark.parametrize("execution_id", [0, -1, True, "1"])
def test_build_session_rejects_invalid_execution_identifier(execution_id):
    plan = build_standard_query_plan(parse_query_expression("cat"))

    with pytest.raises(ResultProcessingError) as caught:
        build_query_result_session(execution_id, plan)

    assert caught.value.code == "invalid_session"


def test_minimum_score_distinguishes_missing_zero_and_negative_values():
    posts = [
        _post(1, score=None),
        _post(2, score=-1),
        _post(3, score=0),
        _post(4, score=1),
    ]

    assert _ids(
        _process(posts, filters=ResultFilterSpec(minimum_score=0))
    ) == [3, 4]
    assert _ids(_process(posts)) == [1, 2, 3, 4]


def test_minimum_favorite_count_distinguishes_missing_and_zero():
    posts = [
        _post(1, fav_count=None),
        _post(2, fav_count=0),
        _post(3, fav_count=2),
    ]

    assert _ids(
        _process(posts, filters=ResultFilterSpec(minimum_fav_count=0))
    ) == [2, 3]
    assert _ids(
        _process(posts, filters=ResultFilterSpec(minimum_fav_count=2))
    ) == [3]


@pytest.mark.parametrize(
    "filters",
    [
        ResultFilterSpec(minimum_score=True),
        ResultFilterSpec(minimum_fav_count=True),
        ResultFilterSpec(minimum_fav_count=-1),
    ],
)
def test_numeric_thresholds_reject_boolean_and_negative_favorite_count(filters):
    with pytest.raises(ResultProcessingError) as caught:
        _process([_post(1)], filters=filters)

    assert caught.value.code == "invalid_filter"


def test_publication_range_is_inclusive_and_normalizes_offsets_to_utc():
    posts = [
        _post(1, created_at="2024-01-01T00:00:00.000000Z"),
        _post(2, created_at="2024-01-02T00:00:00.000000Z"),
        _post(3, created_at="2024-01-03T00:00:00.000000Z"),
        _post(4, created_at=None),
    ]
    china = timezone(timedelta(hours=8))
    filters = ResultFilterSpec(
        published_from_utc=datetime(2024, 1, 1, 8, tzinfo=china),
        published_until_utc=datetime(2024, 1, 2, 8, tzinfo=china),
    )

    assert _ids(_process(posts, filters=filters)) == [1, 2]


def test_age_range_is_inclusive_and_rejects_future_and_missing_times():
    reference = datetime(2024, 1, 10, tzinfo=timezone.utc)
    posts = [
        _post(1, created_at="2024-01-08T00:00:00.000000Z"),
        _post(2, created_at="2024-01-05T00:00:00.000000Z"),
        _post(3, created_at="2024-01-04T23:59:59.999999Z"),
        _post(4, created_at="2024-01-11T00:00:00.000000Z"),
        _post(5, created_at=None),
    ]
    filters = ResultFilterSpec(
        minimum_age=timedelta(days=2),
        maximum_age=timedelta(days=5),
        age_reference_time=reference,
    )

    assert _ids(_process(posts, filters=filters)) == [1, 2]


def test_publication_and_age_ranges_are_combined_with_and():
    filters = ResultFilterSpec(
        published_from_utc=datetime(2024, 1, 5, tzinfo=timezone.utc),
        minimum_age=timedelta(days=3),
        maximum_age=timedelta(days=8),
        age_reference_time=datetime(2024, 1, 10, tzinfo=timezone.utc),
    )
    posts = [
        _post(1, created_at="2024-01-04T00:00:00.000000Z"),
        _post(2, created_at="2024-01-05T00:00:00.000000Z"),
        _post(3, created_at="2024-01-08T00:00:00.000000Z"),
    ]

    assert _ids(_process(posts, filters=filters)) == [2]


@pytest.mark.parametrize(
    "filters",
    [
        ResultFilterSpec(
            published_from_utc=datetime(2024, 1, 2, tzinfo=timezone.utc),
            published_until_utc=datetime(2024, 1, 1, tzinfo=timezone.utc),
        ),
        ResultFilterSpec(published_from_utc=datetime(2024, 1, 1)),
        ResultFilterSpec(minimum_age=timedelta(days=-1)),
        ResultFilterSpec(
            minimum_age=timedelta(days=2),
            maximum_age=timedelta(days=1),
            age_reference_time=datetime(2024, 1, 2, tzinfo=timezone.utc),
        ),
        ResultFilterSpec(minimum_age=timedelta(0)),
    ],
)
def test_invalid_time_filter_configuration_is_rejected(filters):
    with pytest.raises(ResultProcessingError) as caught:
        _process([_post(1)], filters=filters)

    assert caught.value.code == "invalid_filter"


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (PostFileCategory.STATIC, [1, 2, 3, 4, 5]),
        (PostFileCategory.GIF, [6]),
        (PostFileCategory.WEBM, [7]),
        (PostFileCategory.MP4, [8]),
    ],
)
def test_file_categories_match_exact_extensions(category, expected):
    extensions = [
        "jpg",
        "jpeg",
        "png",
        "webp",
        "avif",
        "gif",
        "webm",
        "mp4",
        "zip",
        "swf",
        "jpgx",
    ]
    posts = [_post(index, file_ext=ext) for index, ext in enumerate(extensions, 1)]

    result = _process(
        posts,
        filters=ResultFilterSpec(file_categories=frozenset({category})),
    )

    assert _ids(result) == expected


def test_file_category_union_uses_or_and_no_filter_keeps_unknown_formats():
    posts = [_post(1, file_ext="jpg"), _post(2, file_ext="gif"), _post(3, file_ext="bin")]
    filters = ResultFilterSpec(
        file_categories=frozenset(
            {PostFileCategory.STATIC, PostFileCategory.GIF}
        )
    )

    assert _ids(_process(posts, filters=filters)) == [1, 2]
    assert _ids(_process(posts)) == [1, 2, 3]


def test_empty_file_category_set_is_invalid():
    with pytest.raises(ResultProcessingError) as caught:
        _process(
            [_post(1)],
            filters=ResultFilterSpec(file_categories=frozenset()),
        )

    assert caught.value.code == "invalid_filter"


def test_minimum_dimensions_and_fraction_ratio_use_inclusive_boundaries():
    posts = [
        _post(1, width=200, height=100),
        _post(2, width=201, height=100),
        _post(3, width=199, height=100),
        _post(4, width=200, height=99),
    ]
    filters = ResultFilterSpec(
        minimum_width=200,
        minimum_height=100,
        minimum_aspect_ratio=Fraction(2, 1),
        maximum_aspect_ratio=Fraction(2, 1),
    )

    assert _ids(_process(posts, filters=filters)) == [1]


def test_extreme_fraction_ratio_is_compared_exactly():
    denominator = 10**80
    posts = [
        _post(1, width=1, height=denominator),
        _post(2, width=1, height=denominator + 1),
    ]
    filters = ResultFilterSpec(minimum_aspect_ratio=Fraction(1, denominator))

    assert _ids(_process(posts, filters=filters)) == [1]


@pytest.mark.parametrize(
    ("orientation", "expected"),
    [
        (PostOrientation.LANDSCAPE, [1]),
        (PostOrientation.PORTRAIT, [2]),
        (PostOrientation.SQUARE, [3]),
    ],
)
def test_orientation_uses_strict_integer_comparisons(orientation, expected):
    posts = [
        _post(1, width=101, height=100),
        _post(2, width=100, height=101),
        _post(3, width=100, height=100),
    ]

    result = _process(
        posts,
        filters=ResultFilterSpec(orientations=frozenset({orientation})),
    )

    assert _ids(result) == expected


def test_orientation_union_uses_or_while_other_filter_categories_use_and():
    posts = [
        _post(1, score=5, width=101, height=100),
        _post(2, score=5, width=100, height=100),
        _post(3, score=1, width=100, height=101),
    ]
    filters = ResultFilterSpec(
        minimum_score=5,
        orientations=frozenset(
            {PostOrientation.LANDSCAPE, PostOrientation.PORTRAIT}
        ),
    )

    assert _ids(_process(posts, filters=filters)) == [1]


@pytest.mark.parametrize(
    "filters",
    [
        ResultFilterSpec(minimum_width=0),
        ResultFilterSpec(minimum_height=True),
        ResultFilterSpec(minimum_aspect_ratio=0),
        ResultFilterSpec(minimum_aspect_ratio=-1),
        ResultFilterSpec(minimum_aspect_ratio=0.5),
        ResultFilterSpec(
            minimum_aspect_ratio=Fraction(2),
            maximum_aspect_ratio=Fraction(1),
        ),
        ResultFilterSpec(orientations=frozenset()),
    ],
)
def test_invalid_dimension_ratio_and_orientation_configuration_is_rejected(filters):
    with pytest.raises(ResultProcessingError) as caught:
        _process([_post(1)], filters=filters)

    assert caught.value.code == "invalid_filter"


@pytest.mark.parametrize(
    ("group_name", "tag"),
    [
        ("artist_tags", "sample_artist"),
        ("character_tags", "sample_character"),
        ("copyright_tags", "sample_series"),
        ("general_tags", "sample_general"),
        ("meta_tags", "sample_meta"),
    ],
)
def test_blocked_tags_match_each_complete_tag_group_exactly(group_name, tag):
    tag_groups = {"general_tags": (), group_name: (tag,)}
    post = _post(1, **tag_groups)

    blocked = _process(
        [post],
        filters=ResultFilterSpec(blocked_tags=frozenset({tag.upper()})),
    )
    near_match = _process(
        [post],
        filters=ResultFilterSpec(blocked_tags=frozenset({tag + "_extra"})),
    )

    assert blocked.visible_count == 0
    assert _ids(near_match) == [1]


def test_invalid_blocked_tag_returns_invalid_filter():
    with pytest.raises(ResultProcessingError) as caught:
        _process(
            [_post(1)],
            filters=ResultFilterSpec(blocked_tags=frozenset({"score:>1"})),
        )

    assert caught.value.code == "invalid_filter"


@pytest.mark.parametrize(
    ("rating", "expected"),
    [
        (QueryRating.ALL, [1, 2, 3, 4]),
        (QueryRating.GENERAL, [1]),
        (QueryRating.SENSITIVE, [2]),
        (QueryRating.QUESTIONABLE, [3]),
        (QueryRating.EXPLICIT, [4]),
    ],
)
@pytest.mark.parametrize("rank", [False, True])
def test_session_rating_is_rechecked_for_ordinary_and_rank_queries(
    rating,
    expected,
    rank,
):
    posts = [
        _post(1, rating="g"),
        _post(2, rating="s"),
        _post(3, rating="q"),
        _post(4, rating="e"),
    ]

    result = _process(posts, session=_session(rating=rating, rank=rank))

    assert _ids(result) == expected


@pytest.mark.parametrize(
    ("field", "ascending", "descending"),
    [
        (ResultSortField.SCORE, [1, 4, 3, 2], [4, 3, 1, 2]),
        (ResultSortField.FAV_COUNT, [1, 4, 3, 2], [4, 3, 1, 2]),
    ],
)
def test_numeric_sorting_keeps_missing_last_and_id_descending_ties(
    field,
    ascending,
    descending,
):
    keyword = "score" if field == ResultSortField.SCORE else "fav_count"
    posts = [
        _post(1, **{keyword: -1 if keyword == "score" else 0}),
        _post(2, **{keyword: None}),
        _post(3, **{keyword: 5}),
        _post(4, **{keyword: 5}),
    ]

    assert _ids(
        _process(
            posts,
            sorting=ResultSortSpec(field, ResultSortDirection.ASCENDING),
        )
    ) == ascending
    assert _ids(
        _process(
            posts,
            sorting=ResultSortSpec(field, ResultSortDirection.DESCENDING),
        )
    ) == descending


def test_created_at_sorting_keeps_missing_last_and_id_descending_ties():
    posts = [
        _post(1, created_at="2024-01-01T00:00:00.000000Z"),
        _post(2, created_at=None),
        _post(3, created_at="2024-01-02T00:00:00.000000Z"),
        _post(4, created_at="2024-01-02T00:00:00.000000Z"),
    ]

    assert _ids(
        _process(
            posts,
            sorting=ResultSortSpec(
                ResultSortField.CREATED_AT,
                ResultSortDirection.ASCENDING,
            ),
        )
    ) == [1, 4, 3, 2]
    assert _ids(
        _process(
            posts,
            sorting=ResultSortSpec(
                ResultSortField.CREATED_AT,
                ResultSortDirection.DESCENDING,
            ),
        )
    ) == [4, 3, 1, 2]


def test_post_identifier_sort_uses_selected_direction():
    posts = [_post(3), _post(1), _post(2)]

    assert _ids(
        _process(
            posts,
            sorting=ResultSortSpec(
                ResultSortField.POST_ID,
                ResultSortDirection.ASCENDING,
            ),
        )
    ) == [1, 2, 3]
    assert _ids(
        _process(
            posts,
            sorting=ResultSortSpec(
                ResultSortField.POST_ID,
                ResultSortDirection.DESCENDING,
            ),
        )
    ) == [3, 2, 1]


def test_source_order_is_preserved():
    posts = [_post(3), _post(1), _post(2)]

    assert _ids(_process(posts)) == [3, 1, 2]


@pytest.mark.parametrize(
    "sorting",
    [
        ResultSortSpec(
            ResultSortField.SOURCE_ORDER,
            ResultSortDirection.ASCENDING,
        ),
        ResultSortSpec(ResultSortField.RANK, ResultSortDirection.DESCENDING),
        ResultSortSpec(ResultSortField.SCORE, ResultSortDirection.ORIGINAL),
        ResultSortSpec("score", ResultSortDirection.ASCENDING),
    ],
)
def test_invalid_sort_combinations_are_rejected(sorting):
    with pytest.raises(ResultProcessingError) as caught:
        _process([_post(1)], sorting=sorting)

    assert caught.value.code == "invalid_sort"


def test_rank_uses_prefilter_positions_and_keeps_gaps():
    posts = [_post(10, score=5), _post(20, score=1), _post(30, score=9)]
    result = _process(
        posts,
        session=_session(rank=True),
        filters=ResultFilterSpec(minimum_score=5),
        sorting=ResultSortSpec(
            ResultSortField.RANK,
            ResultSortDirection.ORIGINAL,
        ),
    )

    assert _ids(result) == [10, 30]
    assert [item.session_rank for item in result.items] == [1, 3]


def test_rank_value_survives_other_sorting_and_can_restore_source_order():
    posts = [_post(10, score=5), _post(20, score=1), _post(30, score=9)]
    session = _session(rank=True)
    score_result = _process(
        posts,
        session=session,
        sorting=ResultSortSpec(
            ResultSortField.SCORE,
            ResultSortDirection.DESCENDING,
        ),
    )
    rank_result = _process(
        posts,
        session=session,
        sorting=ResultSortSpec(
            ResultSortField.RANK,
            ResultSortDirection.ORIGINAL,
        ),
    )

    assert _ids(score_result) == [30, 10, 20]
    assert {item.post.id: item.session_rank for item in score_result.items} == {
        10: 1,
        20: 2,
        30: 3,
    }
    assert _ids(rank_result) == [10, 20, 30]


def test_ordinary_session_rejects_rank_sort():
    with pytest.raises(ResultProcessingError) as caught:
        _process(
            [_post(1)],
            sorting=ResultSortSpec(
                ResultSortField.RANK,
                ResultSortDirection.ORIGINAL,
            ),
        )

    assert caught.value.code == "rank_unavailable"


def test_new_execution_builds_a_new_rank_sequence():
    old = process_query_snapshot(
        _snapshot([_post(10), _post(20)], execution_id=1),
        _session(execution_id=1, rank=True),
    )
    new = process_query_snapshot(
        _snapshot([_post(20), _post(10)], execution_id=2),
        _session(execution_id=2, rank=True),
    )

    assert [(item.post.id, item.session_rank) for item in old.items] == [(10, 1), (20, 2)]
    assert [(item.post.id, item.session_rank) for item in new.items] == [(20, 1), (10, 2)]


def test_new_rank_page_keeps_existing_positions_and_appends_new_positions():
    session = _session(rank=True)
    first = process_query_snapshot(
        _snapshot([_post(10), _post(20)]),
        session,
    )
    second = process_query_snapshot(
        _snapshot([_post(10), _post(20), _post(30)]),
        session,
    )

    assert [(item.post.id, item.session_rank) for item in first.items] == [
        (10, 1),
        (20, 2),
    ]
    assert [(item.post.id, item.session_rank) for item in second.items] == [
        (10, 1),
        (20, 2),
        (30, 3),
    ]


def test_stale_snapshot_precedes_invalid_filter_sort_and_post_errors():
    invalid_post = replace(_post(1), width=0)
    with pytest.raises(ResultProcessingError) as caught:
        process_query_snapshot(
            _snapshot([invalid_post], execution_id=1),
            _session(execution_id=2),
            ResultFilterSpec(minimum_fav_count=-1),
            ResultSortSpec("bad", "bad"),
        )

    assert caught.value.code == "stale_snapshot"


def test_invalid_filter_precedes_invalid_sort_and_unavailable_rank():
    with pytest.raises(ResultProcessingError) as caught:
        _process(
            [_post(1)],
            filters=ResultFilterSpec(minimum_fav_count=-1),
            sorting=ResultSortSpec(
                ResultSortField.RANK,
                ResultSortDirection.ORIGINAL,
            ),
        )

    assert caught.value.code == "invalid_filter"


def test_filter_field_validation_order_is_stable():
    filters = ResultFilterSpec(minimum_score=True, minimum_fav_count=-1)

    with pytest.raises(ResultProcessingError) as first:
        _process([_post(1)], filters=filters)
    with pytest.raises(ResultProcessingError) as second:
        _process([_post(1)], filters=filters)

    assert first.value.code == second.value.code == "invalid_filter"
    assert str(first.value) == str(second.value)
    assert "最低得分" in str(first.value)


def test_invalid_sort_precedes_unavailable_rank_and_invalid_post():
    with pytest.raises(ResultProcessingError) as caught:
        _process(
            [replace(_post(1), width=0)],
            sorting=ResultSortSpec(
                ResultSortField.RANK,
                ResultSortDirection.DESCENDING,
            ),
        )

    assert caught.value.code == "invalid_sort"


def test_rank_unavailable_precedes_invalid_post():
    with pytest.raises(ResultProcessingError) as caught:
        _process(
            [replace(_post(1), width=0)],
            sorting=ResultSortSpec(
                ResultSortField.RANK,
                ResultSortDirection.ORIGINAL,
            ),
        )

    assert caught.value.code == "rank_unavailable"


@pytest.mark.parametrize(
    "post",
    [
        replace(_post(1), id=True),
        replace(_post(1), score=True),
        replace(_post(1), fav_count=-1),
        replace(_post(1), rating="all"),
        replace(_post(1), rating=[]),
        replace(_post(1), width=0),
        replace(_post(1), height=True),
        replace(_post(1), file_ext=" JPG "),
        replace(_post(1), created_at="2024-01-01T00:00:00Z"),
        replace(_post(1), general_tags=("CAT",)),
    ],
)
def test_directly_constructed_invalid_post_returns_stable_error(post):
    with pytest.raises(ResultProcessingError) as caught:
        _process([post])

    assert caught.value.code == "invalid_post"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (QueryExecutionState.WAITING, ProcessedResultState.IN_PROGRESS),
        (QueryExecutionState.RUNNING, ProcessedResultState.IN_PROGRESS),
        (QueryExecutionState.RETRY_WAIT, ProcessedResultState.IN_PROGRESS),
        (QueryExecutionState.COMPLETE, ProcessedResultState.COMPLETE),
        (QueryExecutionState.PARTIAL_CANCELLED, ProcessedResultState.PARTIAL),
        (QueryExecutionState.PARTIAL_LIMITED, ProcessedResultState.PARTIAL),
        (QueryExecutionState.PARTIAL_FAILED, ProcessedResultState.PARTIAL),
        (QueryExecutionState.FAILED, ProcessedResultState.FAILED),
    ],
)
def test_execution_state_is_classified_without_rewriting_source(state, expected):
    failure = QueryExecutionFailure(
        position=QueryBranchPosition(0, 1),
        api_tags="cat",
        error_code=QueryExecutionErrorCode.HTTP,
        http_status=500,
        message="failure",
    )
    snapshot = _snapshot(
        [_post(1)],
        state=state,
        unique_candidates=7,
        limit_reason=(
            QueryLimitReason.CANDIDATE_LIMIT
            if state == QueryExecutionState.PARTIAL_LIMITED
            else None
        ),
        failure=(
            failure
            if state in {QueryExecutionState.PARTIAL_FAILED, QueryExecutionState.FAILED}
            else None
        ),
    )

    result = process_query_snapshot(snapshot, _session())

    assert result.source_snapshot is snapshot
    assert result.source_snapshot.counts is snapshot.counts
    assert result.source_snapshot.limit_reason is snapshot.limit_reason
    assert result.source_snapshot.failure is snapshot.failure
    assert result.result_state == expected
    assert result.range_description == "按已扫描的 7 个唯一候选排序"


def test_range_description_uses_unique_candidates_and_survives_zero_visible_items():
    snapshot = _snapshot([_post(1, score=0)], unique_candidates=9)

    result = process_query_snapshot(
        snapshot,
        _session(),
        ResultFilterSpec(minimum_score=1),
    )

    assert result.visible_count == 0
    assert result.filtered_out_count == 1
    assert result.range_description == "按已扫描的 9 个唯一候选排序"
    assert snapshot.counts == QueryExecutionCounts(9, 9, 1)


def test_rank_range_description_marks_only_scanned_popular_order():
    result = process_query_snapshot(
        _snapshot([_post(1)], unique_candidates=12),
        _session(rank=True),
    )

    assert result.range_description == (
        "按已扫描的 12 个唯一候选排序；"
        "Danbooru 近期热门顺序（已扫描范围）"
    )
    assert "全站" not in result.range_description


def test_rank_session_uses_generic_range_description_after_local_score_sort():
    result = process_query_snapshot(
        _snapshot([_post(1)], unique_candidates=12),
        _session(rank=True),
        sort_spec=ResultSortSpec(
            ResultSortField.SCORE,
            ResultSortDirection.DESCENDING,
        ),
    )

    assert result.range_description == "按已扫描的 12 个唯一候选排序"


def test_empty_expression_matches_use_fixed_range_description():
    snapshot = _snapshot([], unique_candidates=5)

    result = process_query_snapshot(snapshot, _session())

    assert result.range_description == "已扫描 5 个唯一候选，当前没有表达式命中作品"


def test_failed_empty_result_only_retains_failure_information():
    failure = QueryExecutionFailure(
        position=QueryBranchPosition(0, 1),
        api_tags="cat",
        error_code=QueryExecutionErrorCode.NETWORK,
        http_status=None,
        message="offline",
    )
    snapshot = _snapshot(
        [],
        state=QueryExecutionState.FAILED,
        unique_candidates=5,
        failure=failure,
    )

    result = process_query_snapshot(snapshot, _session())

    assert result.range_description == ""
    assert result.source_snapshot.failure is failure


def test_new_page_snapshot_is_reprocessed_and_resorted():
    session = _session()
    sorting = ResultSortSpec(
        ResultSortField.SCORE,
        ResultSortDirection.DESCENDING,
    )
    first = process_query_snapshot(
        _snapshot([_post(1, score=1)]),
        session,
        sort_spec=sorting,
    )
    second = process_query_snapshot(
        _snapshot([_post(1, score=1), _post(2, score=10)]),
        session,
        sort_spec=sorting,
    )

    assert _ids(first) == [1]
    assert _ids(second) == [2, 1]


def test_processing_is_deterministic_and_does_not_modify_inputs():
    posts = (_post(1, score=1), _post(2, score=2))
    snapshot = _snapshot(posts)
    filters = ResultFilterSpec(blocked_tags=frozenset({"DOG", "dog"}))
    sorting = ResultSortSpec(
        ResultSortField.SCORE,
        ResultSortDirection.DESCENDING,
    )

    first = process_query_snapshot(snapshot, _session(), filters, sorting)
    second = process_query_snapshot(snapshot, _session(), filters, sorting)

    assert first == second
    assert snapshot.posts == posts
    assert filters.blocked_tags == frozenset({"DOG", "dog"})
    assert sorting == ResultSortSpec(
        ResultSortField.SCORE,
        ResultSortDirection.DESCENDING,
    )
