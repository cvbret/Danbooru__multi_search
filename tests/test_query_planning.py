from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from danbooru_multi_search.danbooru import (
    MAX_QUERY_BRANCHES,
    QueryBranch,
    QueryPlan,
    QueryPlanError,
    QueryRating,
    RemoteOrder,
    RemoteQueryBranch,
    SearchItem,
    SearchItemKind,
    build_query_plan,
    build_latest_query_plan,
    build_rank_query_plan,
    build_standard_query_plan,
    expand_query_to_dnf,
    normalize_plan_rating,
    normalize_remote_order,
    parse_query_expression,
    select_candidate_tags,
    validate_search_item_budget,
)


def _parsed(source: str):
    return parse_query_expression(source)


def _branch_values(source: str):
    return tuple(
        (branch.positive_tags, branch.excluded_tags)
        for branch in expand_query_to_dnf(_parsed(source))
    )


def _error_code(callable_, *args, **kwargs) -> str:
    with pytest.raises(QueryPlanError) as captured:
        callable_(*args, **kwargs)
    return captured.value.code


def test_and_cartesian_product_uses_stable_outer_then_inner_order():
    assert _branch_values("(a OR b) AND (c OR d)") == (
        (frozenset({"a", "c"}), frozenset()),
        (frozenset({"a", "d"}), frozenset()),
        (frozenset({"b", "c"}), frozenset()),
        (frozenset({"b", "d"}), frozenset()),
    )


def test_de_morgan_preserves_operand_order_for_negated_and():
    assert _branch_values("base AND NOT (a AND b)") == (
        (frozenset({"base"}), frozenset({"a"})),
        (frozenset({"base"}), frozenset({"b"})),
    )


def test_de_morgan_negated_or_forms_one_branch_in_operand_order():
    assert _branch_values("base AND NOT (a OR b)") == (
        (frozenset({"base"}), frozenset({"a", "b"})),
    )


def test_double_negative_cancels_without_reordering_conditions():
    assert _branch_values("base AND NOT NOT dog") == (
        (frozenset({"base", "dog"}), frozenset()),
    )


def test_duplicate_branches_are_removed_at_first_occurrence():
    assert _branch_values("b OR a OR b OR a") == (
        (frozenset({"b"}), frozenset()),
        (frozenset({"a"}), frozenset()),
    )


def test_only_exact_duplicates_are_removed_without_absorption():
    assert _branch_values("a OR (a AND b)") == (
        (frozenset({"a"}), frozenset()),
        (frozenset({"a", "b"}), frozenset()),
    )


def test_contradictory_branches_are_removed_while_valid_branches_remain():
    assert _branch_values("(cat AND NOT cat) OR dog") == (
        (frozenset({"dog"}), frozenset()),
    )


def test_all_contradictory_branches_have_stable_root_error():
    assert (
        _error_code(expand_query_to_dnf, _parsed("cat AND NOT cat"))
        == "all_branches_contradictory"
    )


def test_pure_negative_remote_branch_rejects_the_whole_plan():
    assert (
        _error_code(expand_query_to_dnf, _parsed("cat OR NOT dog"))
        == "pure_negative_branch"
    )


def test_outer_positive_condition_makes_both_mixed_branches_executable():
    assert _branch_values("(cat OR NOT dog) AND 1girl") == (
        (frozenset({"cat", "1girl"}), frozenset()),
        (frozenset({"1girl"}), frozenset({"dog"})),
    )


def test_exactly_sixteen_unique_branches_are_allowed():
    source = " OR ".join(f"tag_{index}" for index in range(16))

    branches = expand_query_to_dnf(_parsed(source))

    assert len(branches) == MAX_QUERY_BRANCHES
    assert branches[0].positive_tags == frozenset({"tag_0"})
    assert branches[-1].positive_tags == frozenset({"tag_15"})


def test_seventeenth_unique_branch_fails_immediately():
    source = " OR ".join(f"tag_{index}" for index in range(17))

    assert _error_code(expand_query_to_dnf, _parsed(source)) == "branch_limit"


def test_seventeen_repeated_or_terms_count_as_one_branch():
    source = " OR ".join("cat" for _ in range(17))

    assert _branch_values(source) == (
        (frozenset({"cat"}), frozenset()),
    )


def test_inner_seventeenth_branch_fails_before_outer_contradiction_removes_it():
    inner = " OR ".join(f"tag_{index}" for index in range(17))
    source = f"({inner}) AND NOT tag_16"

    assert _error_code(expand_query_to_dnf, _parsed(source)) == "branch_limit"


def test_and_cartesian_product_obeys_sixteen_and_seventeen_branch_boundary():
    sixteen = "(a OR b OR c OR d) AND (e OR f OR g OR h)"
    over_limit = "(a OR b OR c OR d) AND (e OR f OR g OR h OR i)"

    assert len(expand_query_to_dnf(_parsed(sixteen))) == MAX_QUERY_BRANCHES
    assert _error_code(expand_query_to_dnf, _parsed(over_limit)) == (
        "branch_limit"
    )


def test_query_models_are_immutable_and_use_exact_set_identity():
    branch = QueryBranch(frozenset({"cat"}), frozenset({"dog"}))
    item = SearchItem(SearchItemKind.TAG, "cat")
    remote = RemoteQueryBranch(
        candidate_tags=("cat",),
        rating=QueryRating.ALL,
        remote_order=None,
        local_branch=branch,
    )
    plan = build_standard_query_plan(_parsed("cat"))

    assert branch == QueryBranch(frozenset({"cat"}), frozenset({"dog"}))
    with pytest.raises(FrozenInstanceError):
        branch.positive_tags = frozenset({"bird"})
    with pytest.raises(FrozenInstanceError):
        item.value = "dog"
    with pytest.raises(FrozenInstanceError):
        remote.rating = QueryRating.GENERAL
    with pytest.raises(FrozenInstanceError):
        plan.remote_branches = ()


def test_candidate_selection_prefers_known_small_then_lexical_then_unknown():
    tags = {"unknown_b", "known_b", "zero", "known_a", "unknown_a"}
    counts = {
        "known_b": 4,
        "unknown_a": None,
        "zero": 0,
        "known_a": 4,
    }

    assert select_candidate_tags(tags, counts) == ("zero", "known_a")
    assert select_candidate_tags(tags, counts, limit=1) == ("zero",)


def test_candidate_selection_is_independent_of_mapping_insertion_order():
    tags = {"cat", "dog", "bird"}
    first = {"cat": 10, "dog": 5, "bird": 5}
    second = {"bird": 5, "dog": 5, "cat": 10}

    assert select_candidate_tags(tags, first) == ("bird", "dog")
    assert select_candidate_tags(tags, second) == ("bird", "dog")


def test_post_count_lookup_uses_only_exact_normalized_keys():
    assert select_candidate_tags(
        {"cat", "dog"},
        {"CAT": 0, "dog": 3},
    ) == ("dog", "cat")


def test_missing_and_none_post_counts_are_both_unknown():
    assert select_candidate_tags(
        {"cat", "dog", "bird"},
        {"cat": None},
    ) == ("bird", "cat")


@pytest.mark.parametrize("invalid", [-1, True, False, 1.5, "1", object()])
def test_invalid_relevant_post_count_has_stable_error(invalid):
    assert (
        _error_code(select_candidate_tags, {"cat"}, {"cat": invalid})
        == "invalid_post_count"
    )


def test_irrelevant_invalid_post_count_is_ignored_and_mapping_is_unchanged():
    counts = {"cat": 2, "unused": True}
    before = counts.copy()

    assert select_candidate_tags({"cat"}, counts) == ("cat",)
    assert counts == before


def test_relevant_invalid_post_count_does_not_modify_input_mapping():
    counts = {"cat": -1, "dog": 2}
    before = counts.copy()

    assert (
        _error_code(select_candidate_tags, {"cat", "dog"}, counts)
        == "invalid_post_count"
    )
    assert counts == before


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", QueryRating.ALL),
        ("all", QueryRating.ALL),
        ("ALL", QueryRating.ALL),
        ("g", QueryRating.GENERAL),
        ("General", QueryRating.GENERAL),
        ("s", QueryRating.SENSITIVE),
        ("SENSITIVE", QueryRating.SENSITIVE),
        ("q", QueryRating.QUESTIONABLE),
        ("Questionable", QueryRating.QUESTIONABLE),
        ("e", QueryRating.EXPLICIT),
        ("EXPLICIT", QueryRating.EXPLICIT),
    ],
)
def test_rating_aliases_normalize_to_fixed_values(value, expected):
    assert normalize_plan_rating(value) == expected


@pytest.mark.parametrize(
    "value",
    [None, 1, "safe", "rating:g", "ſ", "ſensitive", "Ｇ"],
)
def test_invalid_rating_has_stable_error(value):
    assert _error_code(normalize_plan_rating, value) == "invalid_rating"


def test_remote_order_accepts_only_absent_or_exact_rank():
    assert normalize_remote_order(None) is None
    assert normalize_remote_order("") is None
    assert normalize_remote_order("rank") == RemoteOrder.RANK
    for invalid in ("Rank", " rank", "rank ", "score", 1):
        assert _error_code(normalize_remote_order, invalid) == (
            "invalid_remote_order"
        )


def test_standard_plan_preserves_dnf_and_builds_ordered_api_items():
    parsed = _parsed("cat AND dog AND bird AND NOT male")
    plan = build_standard_query_plan(
        parsed,
        post_counts={"cat": 30, "dog": 20, "bird": 10},
        rating="g",
    )

    assert plan.expression == parsed.root
    assert plan.dnf_branches == (
        QueryBranch(
            frozenset({"cat", "dog", "bird"}),
            frozenset({"male"}),
        ),
    )
    assert plan.remote_order is None
    remote = plan.remote_branches[0]
    assert remote.local_branch == plan.dnf_branches[0]
    assert remote.candidate_tags == ("bird", "dog")
    assert remote.api_tags == "bird dog rating:g"
    assert tuple(item.value for item in remote.search_items) == (
        "bird",
        "dog",
        "rating:g",
    )
    assert tuple(item.value for item in remote.charged_items) == (
        "bird",
        "dog",
    )
    assert "male" not in remote.api_tags


def test_standard_plan_creates_one_remote_branch_per_valid_dnf_branch():
    plan = build_standard_query_plan(
        _parsed("(cat OR dog) AND 1girl"),
        post_counts={"cat": 5, "dog": 3, "1girl": 20},
    )

    assert tuple(branch.api_tags for branch in plan.remote_branches) == (
        "cat 1girl",
        "dog 1girl",
    )
    assert all(branch.remote_order is None for branch in plan.remote_branches)


def test_search_items_are_the_single_source_for_api_and_charged_order():
    remote = RemoteQueryBranch(
        candidate_tags=("cat",),
        rating=QueryRating.EXPLICIT,
        remote_order=RemoteOrder.RANK,
    )

    assert remote.api_tags == "cat rating:e order:rank"
    assert tuple(item.value for item in remote.search_items) == (
        "cat",
        "rating:e",
        "order:rank",
    )
    assert tuple(item.value for item in remote.charged_items) == (
        "cat",
        "order:rank",
    )


def test_standalone_budget_treats_rating_as_free_and_rejects_three_charged():
    allowed = (
        SearchItem(SearchItemKind.TAG, "cat"),
        SearchItem(SearchItemKind.RATING, "rating:g"),
        SearchItem(SearchItemKind.ORDER, "order:rank"),
    )
    assert validate_search_item_budget(allowed) == (
        allowed[0],
        allowed[2],
    )

    over_budget = (
        SearchItem(SearchItemKind.TAG, "cat"),
        SearchItem(SearchItemKind.TAG, "dog"),
        SearchItem(SearchItemKind.ORDER, "order:rank"),
    )
    assert (
        _error_code(validate_search_item_budget, over_budget)
        == "search_item_limit"
    )


def test_remote_branch_cannot_represent_three_charged_search_items():
    assert (
        _error_code(
            RemoteQueryBranch,
            candidate_tags=("cat", "dog"),
            rating=QueryRating.ALL,
            remote_order=RemoteOrder.RANK,
        )
        == "search_item_limit"
    )


def test_remote_branch_with_three_candidates_uses_unified_budget_error():
    assert (
        _error_code(
            RemoteQueryBranch,
            candidate_tags=("cat", "dog", "bird"),
            rating=QueryRating.ALL,
            remote_order=None,
        )
        == "search_item_limit"
    )


def test_tagless_rank_plan_has_one_remote_stream_and_no_local_expression():
    plan = build_rank_query_plan(None, rating="s")

    assert plan.expression is None
    assert plan.dnf_branches == ()
    assert plan.is_rank
    assert len(plan.remote_branches) == 1
    remote = plan.remote_branches[0]
    assert remote.local_branch is None
    assert remote.candidate_tags == ()
    assert remote.api_tags == "rating:s order:rank"
    assert tuple(item.value for item in remote.charged_items) == (
        "order:rank",
    )


def test_tagless_rank_plan_without_rating_sends_only_rank_order():
    plan = build_rank_query_plan(None)

    assert plan.remote_branches[0].api_tags == "order:rank"


def test_rank_plan_keeps_full_ast_and_dnf_while_using_one_common_candidate():
    parsed = _parsed("(cat OR dog) AND 1girl AND (solo OR outdoors)")
    plan = build_rank_query_plan(
        parsed,
        post_counts={
            "cat": 3,
            "dog": 4,
            "1girl": 20,
            "solo": 5,
            "outdoors": 6,
        },
        rating="q",
    )

    assert plan.expression == parsed.root
    assert len(plan.dnf_branches) == 4
    assert len(plan.remote_branches) == 1
    remote = plan.remote_branches[0]
    assert remote.candidate_tags == ("1girl",)
    assert remote.api_tags == "1girl rating:q order:rank"
    assert tuple(item.value for item in remote.charged_items) == (
        "1girl",
        "order:rank",
    )


def test_rank_plan_selects_the_best_of_multiple_common_tags():
    plan = build_rank_query_plan(
        _parsed("common_a AND common_b AND (cat OR dog)"),
        post_counts={"common_a": 7, "common_b": 2},
    )

    assert plan.remote_branches[0].candidate_tags == ("common_b",)


def test_rank_plan_without_a_common_positive_tag_has_stable_error():
    assert (
        _error_code(build_rank_query_plan, _parsed("cat OR dog"))
        == "rank_requires_common_tag"
    )


@pytest.mark.parametrize(
    ("source", "post_counts", "rating", "order", "expected"),
    [
        (
            "cat AND NOT cat",
            {"cat": -1},
            "bad",
            "bad",
            "all_branches_contradictory",
        ),
        (
            "cat OR NOT dog",
            {"cat": -1},
            "bad",
            "bad",
            "pure_negative_branch",
        ),
        ("cat OR dog", {"cat": -1}, "bad", "bad", "invalid_post_count"),
        ("cat OR dog", {}, "bad", "bad", "invalid_rating"),
        ("cat OR dog", {}, "g", "bad", "invalid_remote_order"),
        ("cat OR dog", {}, "g", "rank", "rank_requires_common_tag"),
    ],
)
def test_plan_root_errors_follow_the_fixed_priority(
    source,
    post_counts,
    rating,
    order,
    expected,
):
    assert (
        _error_code(
            build_query_plan,
            _parsed(source),
            post_counts=post_counts,
            rating=rating,
            remote_order=order,
        )
        == expected
    )


def test_branch_limit_precedes_all_later_plan_validation():
    source = " OR ".join(f"tag_{index}" for index in range(17))

    assert (
        _error_code(
            build_query_plan,
            _parsed(source),
            post_counts={"tag_0": -1},
            rating="bad",
            remote_order="bad",
        )
        == "branch_limit"
    )


def test_invalid_post_count_precedes_rank_common_tag_validation():
    assert (
        _error_code(
            build_rank_query_plan,
            _parsed("cat OR dog"),
            post_counts={"cat": True},
        )
        == "invalid_post_count"
    )


@pytest.mark.parametrize(
    ("rating", "api_tags"),
    [
        ("all", ""),
        ("general", "rating:g"),
        ("sensitive", "rating:s"),
        ("questionable", "rating:q"),
        ("explicit", "rating:e"),
    ],
)
def test_latest_plan_has_one_empty_default_order_branch(rating, api_tags):
    plan = build_latest_query_plan(rating=rating)

    assert plan.expression is None
    assert plan.dnf_branches == ()
    assert plan.remote_order is None
    assert plan.is_latest is True
    assert len(plan.remote_branches) == 1
    branch = plan.remote_branches[0]
    assert branch.candidate_tags == ()
    assert branch.local_branch is None
    assert branch.remote_order is None
    assert branch.api_tags == api_tags


def test_build_query_plan_none_remains_missing_expression():
    assert _error_code(build_query_plan, None) == "missing_expression"


def test_latest_detection_rejects_a_malformed_tagless_plan():
    rank = build_rank_query_plan(None)
    malformed = QueryPlan(
        expression=None,
        dnf_branches=(),
        remote_branches=rank.remote_branches,
        rating=rank.rating,
        remote_order=None,
    )

    assert malformed.is_latest is False
