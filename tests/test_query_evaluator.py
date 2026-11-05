from __future__ import annotations

from danbooru_multi_search.danbooru import (
    DanbooruPost,
    evaluate_post_query,
    evaluate_query,
    parse_query_expression,
    post_tag_set,
)


def _post(**overrides) -> DanbooruPost:
    values = {
        "id": 42,
        "preview_url": "https://example.invalid/preview.jpg",
        "large_file_url": None,
        "file_url": None,
        "score": 3,
        "fav_count": 4,
        "rating": "g",
        "width": 100,
        "height": 200,
        "file_ext": "jpg",
        "artist_tags": ("sample_artist",),
        "character_tags": ("sample_character",),
        "copyright_tags": ("sample_series",),
        "general_tags": ("cat", "blue_sky"),
        "meta_tags": ("highres",),
    }
    values.update(overrides)
    return DanbooruPost(**values)


def test_post_tag_set_combines_all_five_groups_without_mutating_the_post():
    post = _post()
    before = post.to_mapping()

    tags = post_tag_set(post)

    assert tags == frozenset(
        {
            "sample_artist",
            "sample_character",
            "sample_series",
            "cat",
            "blue_sky",
            "highres",
        }
    )
    assert post.to_mapping() == before


def test_each_complete_post_tag_category_can_match_locally():
    post = _post()

    for tag in post_tag_set(post):
        assert evaluate_post_query(parse_query_expression(tag), post) is True


def test_boolean_evaluation_handles_and_or_not_and_double_negative():
    post = _post()

    assert evaluate_post_query(
        parse_query_expression(
            "sample_artist AND (cat OR dog) AND NOT male"
        ),
        post,
    )
    assert evaluate_post_query(parse_query_expression("NOT NOT highres"), post)
    assert not evaluate_post_query(
        parse_query_expression("cat AND NOT blue_sky"),
        post,
    )


def test_generic_evaluation_and_cross_category_duplicates_use_set_semantics():
    parsed = parse_query_expression("cat AND NOT dog")
    post = _post(artist_tags=("cat",), general_tags=("cat", "blue_sky"))

    assert evaluate_query(parsed, frozenset({"cat", "blue_sky"}))
    assert evaluate_post_query(parsed, post)
