from __future__ import annotations

import json

import pytest

from danbooru_multi_search.danbooru import (
    DanbooruParseError,
    parse_posts_response,
)
from danbooru_multi_search.danbooru.parsing import parse_complete_posts_response


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
    assert post.created_at is None
    assert post.file_size is None
    assert post.source is None
    assert post.artist_tags == ()


def test_parse_empty_response():
    assert parse_posts_response(b"[]") == ()


@pytest.mark.parametrize(
    ("score", "fav_count"),
    [
        (None, None),
        (0, 0),
        (-12, 3),
    ],
)
def test_parse_preserves_missing_zero_and_negative_popularity(score, fav_count):
    raw = _post_mapping()
    raw["score"] = score
    raw["fav_count"] = fav_count

    post = parse_posts_response(json.dumps([raw]).encode("utf-8"))[0]

    assert post.score == score
    assert post.fav_count == fav_count


def test_parse_media_kind_is_derived_from_the_file_extension():
    raw = _post_mapping()
    raw["file_ext"] = "webm"

    post = parse_posts_response(json.dumps([raw]).encode("utf-8"))[0]

    assert post.media_kind == "video"


@pytest.mark.parametrize(
    ("file_ext", "expected"),
    [
        (" JPG ", "image"),
        ("jpeg", "image"),
        ("png", "image"),
        ("webp", "image"),
        ("avif", "image"),
        ("gif", "gif"),
        ("webm", "video"),
        ("mp4", "video"),
        ("zip", "ugoira"),
        ("swf", "flash"),
        ("bin", "unknown"),
    ],
)
def test_parse_normalizes_extension_and_distinguishes_media_kinds(
    file_ext,
    expected,
):
    raw = _post_mapping()
    raw["file_ext"] = file_ext

    post = parse_posts_response(json.dumps([raw]).encode("utf-8"))[0]

    assert post.file_ext == file_ext.strip().lower()
    assert post.media_kind == expected


def test_parse_detail_fields_and_all_tag_groups():
    raw = _post_mapping()
    raw.update(
        {
            "created_at": "2024-03-01T01:02:03.4+08:30",
            "file_size": 1_234_567,
            "source": "  file://example/source  ",
            "tag_string_artist": "Sample_Artist",
            "tag_string_character": "hero rival",
            "tag_string_copyright": "fictional_series",
            "tag_string_general": "blue_sky  cloud",
            "tag_string_meta": "highres",
        }
    )

    post = parse_posts_response(json.dumps([raw]).encode("utf-8"))[0]

    assert post.created_at == "2024-02-29T16:32:03.400000Z"
    assert post.file_size == 1_234_567
    assert post.source == "  file://example/source  "
    assert post.artist_tags == ("sample_artist",)
    assert post.character_tags == ("hero", "rival")
    assert post.copyright_tags == ("fictional_series",)
    assert post.general_tags == ("blue_sky", "cloud")
    assert post.meta_tags == ("highres",)


@pytest.mark.parametrize(
    ("created_at", "expected"),
    [
        (None, None),
        ("2024-01-02T03:04:05Z", "2024-01-02T03:04:05.000000Z"),
        (
            "2024-01-02T03:04:05.123456-02:30",
            "2024-01-02T05:34:05.123456Z",
        ),
    ],
)
def test_parse_created_at_normalizes_to_fixed_utc(created_at, expected):
    raw = _post_mapping()
    raw["created_at"] = created_at

    post = parse_posts_response(json.dumps([raw]).encode("utf-8"))[0]

    assert post.created_at == expected


@pytest.mark.parametrize(
    "created_at",
    [
        "2024-01-02",
        "2024-01-02T03:04:05",
        "2024-01-02T03:04:05.1234567Z",
        "2024-01-02T03:04:05+0800",
        " 2024-01-02T03:04:05Z",
        "2024-02-30T03:04:05Z",
        "2024-01-02T03:04:05+24:00",
        "2024-01-02T03:04:05+00:60",
        "2024-01-02T03:04:05+12:99",
        "2024-01-02T03:04:05-23:60",
        "0001-01-01T00:00:00+14:00",
        "9999-12-31T23:59:59-14:00",
    ],
)
def test_parse_rejects_invalid_created_at(created_at):
    raw = _post_mapping()
    raw["created_at"] = created_at

    with pytest.raises(DanbooruParseError, match="created_at"):
        parse_posts_response(json.dumps([raw]).encode("utf-8"))


@pytest.mark.parametrize("file_size", [0, 1024, None])
def test_parse_file_size_preserves_zero_positive_and_missing(file_size):
    raw = _post_mapping()
    raw["file_size"] = file_size

    post = parse_posts_response(json.dumps([raw]).encode("utf-8"))[0]

    assert post.file_size == file_size


@pytest.mark.parametrize("file_size", [-1, True, "10"])
def test_parse_rejects_invalid_file_size(file_size):
    raw = _post_mapping()
    raw["file_size"] = file_size

    with pytest.raises(DanbooruParseError, match="file_size"):
        parse_posts_response(json.dumps([raw]).encode("utf-8"))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("ordinary text", "ordinary text"),
        (" <b>plain</b> ", " <b>plain</b> "),
    ],
)
def test_parse_source_preserves_nonblank_original(source, expected):
    raw = _post_mapping()
    raw["source"] = source

    post = parse_posts_response(json.dumps([raw]).encode("utf-8"))[0]

    assert post.source == expected


def test_parse_rejects_invalid_or_oversized_source():
    raw = _post_mapping()
    raw["source"] = 123
    with pytest.raises(DanbooruParseError, match="source"):
        parse_posts_response(json.dumps([raw]).encode("utf-8"))

    raw["source"] = "x" * 1201
    with pytest.raises(DanbooruParseError, match="source"):
        parse_posts_response(json.dumps([raw]).encode("utf-8"))


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("tag_string_artist", 3),
        ("tag_string_artist", "猫"),
        ("tag_string_artist", "cat\tdog"),
        ("tag_string_artist", "cat\ndog"),
        ("tag_string_artist", "cat\x00dog"),
        ("tag_string_artist", "cat*"),
    ],
)
def test_parse_rejects_invalid_tag_group_fields(field_name, value):
    raw = _post_mapping()
    raw[field_name] = value

    with pytest.raises(DanbooruParseError, match=field_name):
        parse_posts_response(json.dumps([raw]).encode("utf-8"))


@pytest.mark.parametrize(
    "groups",
    [
        {"tag_string_general": "cat cat"},
        {
            "tag_string_artist": "Sample_Tag",
            "tag_string_character": "sample_tag",
        },
    ],
)
def test_parse_rejects_normalized_duplicates_across_all_tag_groups(groups):
    raw = _post_mapping()
    raw.update(groups)

    with pytest.raises(DanbooruParseError, match="duplicate tag"):
        parse_posts_response(json.dumps([raw]).encode("utf-8"))


def test_parse_missing_null_and_blank_tag_groups_become_empty_tuples():
    raw = _post_mapping()
    raw.update(
        {
            "tag_string_artist": None,
            "tag_string_character": "",
            "tag_string_copyright": "   ",
        }
    )

    post = parse_posts_response(json.dumps([raw]).encode("utf-8"))[0]

    assert post.artist_tags == ()
    assert post.character_tags == ()
    assert post.copyright_tags == ()
    assert post.general_tags == ()
    assert post.meta_tags == ()


def test_complete_parser_accepts_all_five_explicit_string_fields():
    raw = _post_mapping()
    raw.update(
        {
            "tag_string_artist": "sample_artist",
            "tag_string_character": "",
            "tag_string_copyright": "   ",
            "tag_string_general": "cat blue_sky",
            "tag_string_meta": "highres",
        }
    )

    post = parse_complete_posts_response(json.dumps([raw]).encode("utf-8"))[0]

    assert post.artist_tags == ("sample_artist",)
    assert post.character_tags == ()
    assert post.copyright_tags == ()
    assert post.general_tags == ("cat", "blue_sky")
    assert post.meta_tags == ("highres",)


@pytest.mark.parametrize(
    "field_name",
    [
        "tag_string_artist",
        "tag_string_character",
        "tag_string_copyright",
        "tag_string_general",
        "tag_string_meta",
    ],
)
@pytest.mark.parametrize(
    "invalid_value",
    [pytest.param("missing", id="missing"), pytest.param(None, id="null"), 7],
)
def test_complete_parser_rejects_missing_null_and_non_string_tag_fields(
    field_name,
    invalid_value,
):
    raw = _post_mapping()
    raw.update(
        {
            "tag_string_artist": "",
            "tag_string_character": "",
            "tag_string_copyright": "",
            "tag_string_general": "",
            "tag_string_meta": "",
        }
    )
    if invalid_value == "missing":
        raw.pop(field_name)
    else:
        raw[field_name] = invalid_value

    with pytest.raises(DanbooruParseError, match=field_name):
        parse_complete_posts_response(json.dumps([raw]).encode("utf-8"))


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_parse_rejects_nonstandard_json_constants_even_in_unused_fields(constant):
    raw = json.dumps(_post_mapping()).encode("utf-8")
    payload = b"[{\"unused\":" + constant + b"," + raw[1:] + b"]"

    with pytest.raises(DanbooruParseError, match="not valid JSON"):
        parse_posts_response(payload)


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


def test_parse_converts_deep_json_recursion_to_a_stable_error():
    payload = b"[" * 2_000 + b"0" + b"]" * 2_000

    with pytest.raises(DanbooruParseError, match="not valid JSON"):
        parse_posts_response(payload)


def test_parse_converts_oversized_integer_to_a_stable_error():
    payload = b'[{"id":' + b"9" * 5_000 + b"}]"

    with pytest.raises(DanbooruParseError, match="not valid JSON"):
        parse_posts_response(payload)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("id", "12345"),
        ("fav_count", -1),
        ("image_width", 0),
        ("file_ext", None),
        ("preview_file_url", 7),
        ("rating", "unknown"),
    ],
)
def test_parse_rejects_invalid_fields(field_name, invalid_value):
    raw = _post_mapping()
    raw[field_name] = invalid_value

    with pytest.raises(DanbooruParseError, match=field_name):
        parse_posts_response(json.dumps([raw]).encode("utf-8"))
