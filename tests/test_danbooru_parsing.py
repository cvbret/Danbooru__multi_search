from __future__ import annotations

import json

import pytest

from danbooru_multi_search.danbooru import (
    DanbooruParseError,
    parse_posts_response,
)


def _post_mapping() -> dict[str, object]:
    return {
        "id": 12345,
        "preview_file_url": "https://cdn.example/preview.jpg",
        "large_file_url": "https://cdn.example/large.jpg",
        "file_url": "https://cdn.example/original.jpg",
        "score": 87,
        "fav_count": 42,
        "rating": "g",
        "image_width": 1600,
        "image_height": 1200,
        "file_ext": "jpg",
    }


def test_parse_post_covers_the_minimal_model_fields():
    posts = parse_posts_response(json.dumps([_post_mapping()]).encode("utf-8"))

    assert len(posts) == 1
    post = posts[0]
    assert post.id == 12345
    assert post.preview_url == "https://cdn.example/preview.jpg"
    assert post.large_file_url == "https://cdn.example/large.jpg"
    assert post.file_url == "https://cdn.example/original.jpg"
    assert post.display_url == post.preview_url
    assert post.score == 87
    assert post.fav_count == 42
    assert post.rating == "g"
    assert post.width == 1600
    assert post.height == 1200
    assert post.file_ext == "jpg"


def test_parse_empty_response():
    assert parse_posts_response(b"[]") == ()


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b'{"id": 1}',
    ],
)
def test_parse_rejects_invalid_json_roots(payload):
    with pytest.raises(DanbooruParseError):
        parse_posts_response(payload)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("id", "12345"),
        ("fav_count", -1),
        ("image_width", 0),
        ("file_ext", None),
        ("preview_file_url", 7),
    ],
)
def test_parse_rejects_invalid_fields(field_name, invalid_value):
    raw = _post_mapping()
    raw[field_name] = invalid_value

    with pytest.raises(DanbooruParseError, match=field_name):
        parse_posts_response(json.dumps([raw]).encode("utf-8"))
