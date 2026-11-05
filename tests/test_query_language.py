from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from danbooru_multi_search.danbooru import (
    MAX_QUERY_CHARACTERS,
    AndNode,
    NotNode,
    OrNode,
    QueryParseError,
    SourceSpan,
    TagNode,
    analyze_query,
    format_query_expression,
    parse_query_expression,
)


def _diagnostic(source: str):
    with pytest.raises(QueryParseError) as captured:
        parse_query_expression(source)
    return captured.value.diagnostic


def test_operator_precedence_and_implicit_and_build_flat_immutable_nodes():
    parsed = parse_query_expression("NOT Cat AND dog bird OR fish")

    assert parsed.normalized_text == "NOT cat AND dog AND bird OR fish"
    assert isinstance(parsed.root, OrNode)
    left, right = parsed.root.operands
    assert isinstance(left, AndNode)
    assert [type(item) for item in left.operands] == [
        NotNode,
        TagNode,
        TagNode,
    ]
    assert right == TagNode("fish", SourceSpan(0, 0))
    with pytest.raises(FrozenInstanceError):
        right.name = "dog"


@pytest.mark.parametrize(
    ("source", "normalized"),
    [
        ("cat dog", "cat AND dog"),
        ("cat NOT dog", "cat AND NOT dog"),
        ("(cat)(dog)", "cat AND dog"),
        ("cat AND dog", "cat AND dog"),
        ("(cat)OR(dog)", "cat OR dog"),
        ("(fate_(series) OR dog)", "fate_(series) OR dog"),
        ("foo_(bar)", "foo_(bar)"),
    ],
)
def test_explicit_implicit_and_tight_parentheses_normalize(
    source,
    normalized,
):
    assert parse_query_expression(source).normalized_text == normalized


@pytest.mark.parametrize(
    "tag",
    [
        "fate/grand_order",
        "girls'_frontline",
        "honkai:_star_rail",
        "love_live!",
        "artist-name_(artist)",
        "candy",
        "not_tag",
        "foo_OR_bar",
    ],
)
def test_real_ascii_tag_punctuation_and_keyword_substrings_are_preserved(tag):
    assert parse_query_expression(tag.upper()).normalized_text == tag.casefold()


@pytest.mark.parametrize("source", ["AND", "or", "Not"])
def test_independent_operator_names_remain_reserved(source):
    diagnostic = _diagnostic(source)

    assert diagnostic.code in {"unexpected_operator", "missing_operand"}


@pytest.mark.parametrize(
    "source",
    [
        "rating:g",
        "order:score",
        "commentary:any",
        "random:1",
        "filesize:>1m",
        "pool_count:>0",
    ],
)
def test_text_metatags_are_rejected_with_a_stable_diagnostic(source):
    diagnostic = _diagnostic(source)

    assert diagnostic.code == "metatag_not_allowed"
    assert diagnostic.span == SourceSpan(0, len(source))


@pytest.mark.parametrize("source", ["-cat", "~cat", "cat*", "ſ", "Straße"])
def test_m1_invalid_tag_forms_remain_invalid(source):
    diagnostic = _diagnostic(source)

    assert diagnostic.code in {"invalid_tag", "non_ascii_character"}


@pytest.mark.parametrize(
    ("source", "code", "span"),
    [
        ("   ", "empty_expression", SourceSpan(0, 3)),
        ("()", "empty_group", SourceSpan(0, 2)),
        ("AND cat", "unexpected_operator", SourceSpan(0, 3)),
        ("cat OR", "missing_operand", SourceSpan(6, 6)),
        ("cat AND OR dog", "unexpected_operator", SourceSpan(8, 10)),
        ("cat NOT", "missing_operand", SourceSpan(7, 7)),
        ("cat)", "unexpected_closing_parenthesis", SourceSpan(3, 4)),
        ("(cat", "unclosed_group", SourceSpan(0, 1)),
        ("NOT", "missing_operand", SourceSpan(3, 3)),
    ],
)
def test_syntax_diagnostics_use_stable_codes_and_original_half_open_spans(
    source,
    code,
    span,
):
    diagnostic = _diagnostic(source)

    assert diagnostic.code == code
    assert diagnostic.span == span


def test_normalized_round_trip_preserves_semantics_order_duplicates_and_double_not():
    parsed = parse_query_expression("Cat cat NOT NOT dog OR (bird AND fish)")
    reparsed = parse_query_expression(parsed.normalized_text)

    assert parsed.root == reparsed.root
    assert parsed.normalized_text == (
        "cat AND cat AND NOT NOT dog OR bird AND fish"
    )
    assert [item.name for item in parsed.root.operands[0].operands[:2]] == [
        "cat",
        "cat",
    ]
    assert format_query_expression(reparsed) == parsed.normalized_text


def test_source_ranges_do_not_change_semantic_ast_equality():
    explicit = parse_query_expression("cat AND dog")
    implicit = parse_query_expression("  cat   dog  ")

    assert explicit.root == implicit.root
    assert explicit == implicit
    assert explicit.span != implicit.span


def test_ast_nodes_keep_original_tag_and_operator_source_ranges():
    parsed = parse_query_expression("  cat AND NOT dog  ")
    assert isinstance(parsed.root, AndNode)
    cat, negated = parsed.root.operands
    assert isinstance(cat, TagNode)
    assert isinstance(negated, NotNode)
    assert isinstance(negated.operand, TagNode)

    assert parsed.span == SourceSpan(2, 17)
    assert parsed.root.span == SourceSpan(2, 17)
    assert cat.span == SourceSpan(2, 5)
    assert negated.span == SourceSpan(10, 17)
    assert negated.operand.span == SourceSpan(14, 17)

    grouped = parse_query_expression("NOT(cat) dog")
    assert isinstance(grouped.root, AndNode)
    grouped_not = grouped.root.operands[0]
    assert isinstance(grouped_not, NotNode)
    assert grouped_not.span == SourceSpan(0, 8)
    assert grouped_not.operand.span == SourceSpan(4, 7)


@pytest.mark.parametrize("source", ["NOT cat", "NOT (cat OR dog)"])
def test_wholly_negative_expression_is_rejected_after_parsing(source):
    diagnostic = _diagnostic(source)

    assert diagnostic.code == "pure_negative_expression"


def test_not_can_touch_a_query_group_before_pure_negative_validation():
    diagnostic = _diagnostic("NOT(cat)")

    assert diagnostic.code == "pure_negative_expression"
    assert diagnostic.span == SourceSpan(0, 8)


def test_positive_analysis_uses_effective_not_parity_and_preserves_occurrences():
    double_negative = parse_query_expression("NOT NOT cat")
    mixed = parse_query_expression("cat OR NOT dog")
    outer_positive = parse_query_expression("(cat OR NOT dog) AND 1girl")
    nested = parse_query_expression("NOT (cat AND NOT dog)")

    assert analyze_query(double_negative).positive_tags == ("cat",)
    assert analyze_query(mixed).positive_tags == ("cat",)
    assert analyze_query(mixed).negative_tags == ("dog",)
    assert analyze_query(outer_positive).positive_tags == ("cat", "1girl")
    assert analyze_query(nested).positive_tags == ("dog",)
    assert analyze_query(nested).negative_tags == ("cat",)


def test_character_limit_accepts_exact_boundary_and_rejects_the_next_character():
    exact = "cat" + " " * (MAX_QUERY_CHARACTERS - 3)

    assert parse_query_expression(exact).normalized_text == "cat"
    diagnostic = _diagnostic(exact + " ")
    assert diagnostic.code == "expression_too_long"
    assert diagnostic.span == SourceSpan(
        MAX_QUERY_CHARACTERS,
        MAX_QUERY_CHARACTERS + 1,
    )


def test_flattened_node_limit_accepts_128_and_rejects_129_nodes():
    allowed = " ".join(f"tag_{index}" for index in range(127))
    rejected = " ".join(f"tag_{index}" for index in range(128))

    assert isinstance(parse_query_expression(allowed).root, AndNode)
    diagnostic = _diagnostic(rejected)
    assert diagnostic.code == "node_limit_exceeded"


def test_nesting_limit_accepts_16_and_rejects_17_groups_or_not_nodes():
    groups_16 = "(" * 16 + "cat" + ")" * 16
    groups_17 = "(" * 17 + "cat" + ")" * 17
    not_16 = "NOT " * 16 + "cat"
    not_17 = "NOT " * 17 + "cat"

    assert parse_query_expression(groups_16).normalized_text == "cat"
    assert parse_query_expression(not_16).normalized_text == not_16.strip()
    assert _diagnostic(groups_17).code == "nesting_limit_exceeded"
    assert _diagnostic(not_17).code == "nesting_limit_exceeded"


def test_not_and_query_group_share_the_same_combined_depth_limit():
    allowed = "dog AND " + "NOT " * 15 + "(cat)"
    rejected = "dog AND " + "NOT " * 16 + "(cat)"

    assert parse_query_expression(allowed).normalized_text == (
        "dog AND " + "NOT " * 15 + "cat"
    )
    assert _diagnostic(rejected).code == "nesting_limit_exceeded"


def test_many_internal_tag_parentheses_do_not_consume_query_nesting_depth():
    tag = "tag" + "_(part)" * 16

    assert parse_query_expression(tag).normalized_text == tag
