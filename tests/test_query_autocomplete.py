from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication

from danbooru_multi_search.danbooru.autocomplete import (
    DanbooruTagSuggestionController,
    locate_active_tag_fragment,
    locate_query_tag_fragment,
    replace_active_tag_fragment,
    replace_query_tag_fragment,
)
from danbooru_multi_search.danbooru.tag_index import TagIndexSnapshot
from danbooru_multi_search.danbooru.tags import TagSuggestion


APPLICATION = QGuiApplication.instance() or QGuiApplication([])


@pytest.mark.parametrize(
    ("text", "needle", "offset", "expected"),
    [
        (
            "cat AND (dog OR wolf)",
            "cat",
            1,
            (0, 3, "cat"),
        ),
        (
            "cat AND (dog OR wolf)",
            "dog",
            1,
            (9, 12, "dog"),
        ),
        (
            "cat AND (dog OR wolf)",
            "wolf",
            2,
            (16, 20, "wolf"),
        ),
        (
            "cat dog bird",
            "dog",
            2,
            (4, 7, "dog"),
        ),
        (
            "touhou_(pc-98) AND cat",
            "pc-98",
            2,
            (0, 14, "touhou_(pc-98)"),
        ),
        (
            "and_(symbol) OR cat",
            "symbol",
            1,
            (0, 12, "and_(symbol)"),
        ),
        (
            "cat AND (东方 OR dog)",
            "东方",
            1,
            (9, 11, "东方"),
        ),
    ],
)
def test_query_fragment_locates_tags_in_boolean_expression(
    text,
    needle,
    offset,
    expected,
):
    cursor = text.index(needle) + offset

    fragment = locate_query_tag_fragment(text, cursor)

    assert (fragment.start, fragment.end, fragment.fragment) == expected


@pytest.mark.parametrize("operator", ["AND", "or", "NoT"])
def test_query_fragment_never_treats_independent_operator_as_tag(operator):
    text = f"cat {operator} dog"
    cursor = text.index(operator) + 1

    fragment = locate_query_tag_fragment(text, cursor)

    assert fragment.is_empty is True
    assert (fragment.start, fragment.end) == (cursor, cursor)


def test_query_fragment_handles_operators_adjacent_to_query_parentheses():
    text = "(cat)OR(dog)"

    operator = locate_query_tag_fragment(text, text.index("OR") + 1)
    dog = locate_query_tag_fragment(text, text.index("dog") + 1)

    assert operator.is_empty is True
    assert (dog.start, dog.end, dog.fragment) == (8, 11, "dog")


def test_query_fragment_distinguishes_query_and_internal_parentheses():
    text = "(touhou_(pc-98) OR cat)"

    opening = locate_query_tag_fragment(text, 0)
    internal = locate_query_tag_fragment(text, text.index("pc-98") + 2)
    before_closing = locate_query_tag_fragment(text, text.rindex(")") - 1)
    after_closing = locate_query_tag_fragment(text, len(text))

    assert opening.is_empty is True
    assert internal.fragment == "touhou_(pc-98)"
    assert before_closing.fragment == "cat"
    assert after_closing.is_empty is True


def test_query_fragment_uses_only_immediately_adjacent_token_at_whitespace():
    text = "cat  dog"

    after_cat = locate_query_tag_fragment(text, 3)
    between_spaces = locate_query_tag_fragment(text, 4)

    assert after_cat.fragment == "cat"
    assert between_spaces.is_empty is True


@pytest.mark.parametrize(
    ("text", "cursor"),
    [
        ("", 0),
        ("cat AND dog", 4),
        ("cat AND dog", 7),
        ("cat AND (dog)", 8),
    ],
)
def test_query_fragment_reports_empty_in_insertion_and_group_positions(text, cursor):
    fragment = locate_query_tag_fragment(text, cursor)

    assert fragment.is_empty is True
    assert fragment.start == cursor
    assert fragment.end == cursor


def test_query_fragment_rejects_invalid_argument_types_and_cursor_ranges():
    with pytest.raises(TypeError):
        locate_query_tag_fragment(None, 0)
    with pytest.raises(TypeError):
        locate_query_tag_fragment("cat", True)
    with pytest.raises(ValueError):
        locate_query_tag_fragment("cat", 4)


def test_query_replacement_preserves_operators_groups_and_spacing():
    text = "cat  AND (do OR touhou_(pc-98))"
    cursor = text.index("do") + 1

    replacement = replace_query_tag_fragment(text, cursor, "dog")

    assert replacement.text == "cat  AND (dog OR touhou_(pc-98))"
    assert replacement.cursor == text.index("do") + 3
    assert replacement.changed is True
    assert replacement.should_search is False


def test_query_replacement_accepts_chinese_alias_fragment_and_real_tag_punctuation():
    text = "cat AND (东方 OR dog)"
    cursor = text.index("东方") + 1

    replacement = replace_query_tag_fragment(
        text,
        cursor,
        "fate/grand_order",
    )

    assert replacement.text == "cat AND (fate/grand_order OR dog)"
    assert replacement.cursor == text.index("东方") + len("fate/grand_order")
    assert replacement.should_search is False


@pytest.mark.parametrize(
    ("cursor_text", "tag_name"),
    [
        ("cat AND dog", "AND"),
        ("cat OR dog", "rating:g"),
        ("cat NOT dog", "two tags"),
    ],
)
def test_query_replacement_rejects_operator_metatag_and_multiple_tags(
    cursor_text,
    tag_name,
):
    cursor = cursor_text.rindex("dog") + 1

    replacement = replace_query_tag_fragment(cursor_text, cursor, tag_name)

    assert replacement.text == cursor_text
    assert replacement.cursor == cursor
    assert replacement.changed is False


def test_query_controller_entry_supports_more_than_two_expression_tokens():
    snapshot = TagIndexSnapshot.build(
        (
            TagSuggestion("wolf", ("狼",), 20, 0),
            TagSuggestion("wolf_girl", ("狼女",), 10, 0),
        )
    )
    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        index_snapshot=snapshot,
        connect_application_quit=False,
    )
    text = "cat AND dog OR wo"

    controller.updateInput(text, len(text))
    assert controller.suggestionsModel.items == ()

    controller.updateQueryInput(text, len(text))
    assert [item.name for item in controller.suggestionsModel.items] == [
        "wolf",
        "wolf_girl",
    ]

    applied = controller.applyQuerySuggestion(text, len(text), 0)
    assert applied == {
        "text": "cat AND dog OR wolf",
        "cursor": len("cat AND dog OR wolf"),
        "changed": True,
        "shouldSearch": False,
    }
    controller.shutdown()


def test_query_suggestion_model_exposes_numeric_category_for_artist_confirmation():
    snapshot = TagIndexSnapshot.build(
        (TagSuggestion("artist_name", ("画师",), 50, 1),)
    )
    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        index_snapshot=snapshot,
        connect_application_quit=False,
    )

    controller.updateQueryInput("base AND artist", len("base AND artist"))

    model = controller.suggestionsModel
    roles = {
        bytes(name).decode("ascii"): role
        for role, name in model.roleNames().items()
    }
    assert model.data(model.index(0, 0), roles["category"]) == 1
    controller.shutdown()


def test_m1_locator_and_replacement_keep_their_two_tag_contract():
    fragment = locate_active_tag_fragment("cat dog bird", 5)
    replacement = replace_active_tag_fragment("cat dog bird", 5, "wolf")

    assert fragment.fragment == "dog"
    assert fragment.token_count == 3
    assert replacement.changed is False
