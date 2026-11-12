from __future__ import annotations

import pytest

from danbooru_multi_search.danbooru import (
    SourceSpan,
    TagNode,
    format_query_expression,
    parse_query_expression,
)
from danbooru_multi_search.ui.query_editor import (
    ArtistCombinationMode,
    ConfirmedArtistTag,
    QueryEditorError,
    QueryGroupOperator,
    QueryTreeEditor,
    build_artist_subtree,
    confirmed_artist_tags,
)


def _tag(name: str) -> TagNode:
    return TagNode(name, SourceSpan(0, 0))


def test_artist_mode_public_import_is_the_pure_core_enum():
    from danbooru_multi_search.core.artist_combinations import ArtistCombinationMode as CoreMode

    assert ArtistCombinationMode is CoreMode


def test_empty_editor_accepts_first_tag_and_returns_to_empty_after_removal():
    editor = QueryTreeEditor()

    assert editor.root is None
    assert editor.canonical_text == ""
    assert editor.snapshot.revision == 0

    editor.add_tag(None, "Cat")
    assert editor.canonical_text == "cat"

    editor.remove_node(())
    assert editor.root is None
    assert editor.canonical_text == editor.draft_text == ""
    assert editor.snapshot.revision == 2


def test_valid_advanced_text_replaces_tree_with_canonical_nested_expression():
    editor = QueryTreeEditor("cat")

    accepted = editor.apply_advanced_text(" (Cat OR dog) fish ")

    assert accepted is True
    assert editor.canonical_text == "(cat OR dog) AND fish"
    assert editor.draft_text == editor.canonical_text
    assert editor.root == parse_query_expression(editor.canonical_text).root
    assert editor.text_diagnostic is None


def test_invalid_advanced_text_preserves_raw_text_and_last_valid_tree():
    editor = QueryTreeEditor("cat AND dog")
    old_root = editor.root

    accepted = editor.apply_advanced_text("cat OR")

    assert accepted is False
    assert editor.root == old_root
    assert editor.canonical_text == "cat AND dog"
    assert editor.draft_text == "cat OR"
    assert editor.text_diagnostic is not None
    assert editor.text_diagnostic.code == "missing_operand"
    assert editor.text_diagnostic.span == SourceSpan(6, 6)
    assert editor.visual_editing_enabled is False


def test_invalid_text_blocks_visual_edits_until_explicit_discard():
    editor = QueryTreeEditor("cat")
    editor.apply_advanced_text("cat AND")

    with pytest.raises(QueryEditorError) as captured:
        editor.add_tag(None, "dog")
    assert captured.value.code == "invalid_text_pending"
    assert editor.draft_text == "cat AND"

    assert editor.discard_invalid_text() is True
    assert editor.draft_text == "cat"
    assert editor.discard_invalid_text() is False
    editor.add_tag(None, "dog")
    assert editor.canonical_text == "cat AND dog"


def test_wrap_move_and_remove_nested_group_keep_canonical_semantics():
    editor = QueryTreeEditor("cat AND dog")

    editor.wrap_with_group((1,), QueryGroupOperator.OR, _tag("fish"))
    assert editor.canonical_text == "cat AND (dog OR fish)"

    editor.move_node((1, 1), -1)
    assert editor.canonical_text == "cat AND (fish OR dog)"

    editor.remove_node((1, 1))
    assert editor.canonical_text == "cat AND fish"
    editor.remove_node((1,))
    assert editor.canonical_text == "cat"


def test_not_wrap_and_unwrap_validate_atomically():
    editor = QueryTreeEditor("base AND artist_a")

    editor.wrap_not((1,))
    assert editor.canonical_text == "base AND NOT artist_a"
    editor.unwrap_not((1,))
    assert editor.canonical_text == "base AND artist_a"

    single = QueryTreeEditor("cat")
    with pytest.raises(QueryEditorError) as captured:
        single.wrap_not(())
    assert captured.value.code == "pure_negative_expression"
    assert single.canonical_text == "cat"


def test_group_insertion_uses_selected_group_or_root_and_group():
    editor = QueryTreeEditor("cat OR dog")
    nested = parse_query_expression("fish AND bird")

    editor.insert_subtree((), nested, index=1)
    assert editor.canonical_text == "cat OR fish AND bird OR dog"

    editor.insert_subtree(None, _tag("1girl"))
    assert editor.canonical_text == "(cat OR fish AND bird OR dog) AND 1girl"


def test_visual_edit_rejects_seventeenth_dnf_branch_without_state_change():
    source = " OR ".join(f"tag_{index}" for index in range(16))
    editor = QueryTreeEditor(source)
    before = editor.snapshot

    with pytest.raises(QueryEditorError) as captured:
        editor.insert_subtree((), _tag("tag_16"))

    assert captured.value.code == "branch_limit"
    assert editor.snapshot == before


def test_artist_shortcuts_build_all_any_and_exclude_as_ordinary_ast():
    artists = confirmed_artist_tags((("Artist_A", 1), ("artist_b", 1)))

    assert (
        format_query_expression(
            build_artist_subtree(artists, ArtistCombinationMode.ALL)
        )
        == "artist_a AND artist_b"
    )
    assert (
        format_query_expression(
            build_artist_subtree(artists, ArtistCombinationMode.ANY)
        )
        == "artist_a OR artist_b"
    )
    assert (
        format_query_expression(
            build_artist_subtree(artists, ArtistCombinationMode.EXCLUDE)
        )
        == "NOT artist_a AND NOT artist_b"
    )


def test_artist_exclusion_inserts_under_positive_root_and_deduplicates():
    editor = QueryTreeEditor("base")
    artist = ConfirmedArtistTag("artist_a", 1)

    editor.insert_artist_group(
        (artist, artist, ConfirmedArtistTag("artist_b", 1)),
        ArtistCombinationMode.EXCLUDE,
    )

    assert editor.canonical_text == "base AND NOT artist_a AND NOT artist_b"


@pytest.mark.parametrize("category", [None, 0, 3, True, 1.0])
def test_artist_shortcut_rejects_unconfirmed_or_non_artist_categories(category):
    with pytest.raises(QueryEditorError) as captured:
        ConfirmedArtistTag("some_tag", category)  # type: ignore[arg-type]

    assert captured.value.code == "artist_category_required"


def test_empty_artist_group_and_invalid_mode_have_stable_errors():
    with pytest.raises(QueryEditorError) as empty:
        build_artist_subtree((), ArtistCombinationMode.ALL)
    assert empty.value.code == "empty_artist_group"

    with pytest.raises(QueryEditorError) as invalid:
        build_artist_subtree((ConfirmedArtistTag("artist_a", 1),), "all")  # type: ignore[arg-type]
    assert invalid.value.code == "invalid_artist_mode"


def test_text_plan_failure_preserves_whole_source_span_and_valid_tree():
    editor = QueryTreeEditor("base")
    source = " OR ".join(f"tag_{index}" for index in range(17))

    assert editor.apply_advanced_text(source) is False
    assert editor.text_diagnostic is not None
    assert editor.text_diagnostic.code == "branch_limit"
    assert editor.text_diagnostic.span == SourceSpan(0, len(source))
    assert editor.canonical_text == "base"
