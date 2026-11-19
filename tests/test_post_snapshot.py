from __future__ import annotations

import json
from dataclasses import replace

import pytest

from danbooru_multi_search.danbooru import DanbooruPost
from danbooru_multi_search.storage.post_snapshot import (
    POST_SNAPSHOT_VERSION,
    PostSnapshotError,
    decode_post_snapshot,
    encode_post_snapshot,
)


def _post() -> DanbooruPost:
    return DanbooruPost(
        id=42,
        preview_url="https://example.invalid/preview.jpg",
        large_file_url="https://example.invalid/large.jpg",
        file_url="https://example.invalid/original.png",
        score=-3,
        fav_count=0,
        rating="q",
        width=2048,
        height=1024,
        file_ext="png",
        created_at="2026-11-10T01:02:03.456789Z",
        file_size=123456,
        source="来源文本 / source",
        artist_tags=("sample_artist",),
        character_tags=("sample_character",),
        copyright_tags=("sample_series",),
        general_tags=("blue_sky",),
        meta_tags=("highres",),
    )


def test_full_post_snapshot_round_trip_is_canonical_and_complete():
    post = _post()

    encoded = encode_post_snapshot(post)
    decoded = decode_post_snapshot(encoded)

    assert decoded == post
    assert decoded is not post
    assert encoded == json.dumps(
        json.loads(encoded),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    assert json.loads(encoded)["version"] == POST_SNAPSHOT_VERSION
    assert post.artist_tags == ("sample_artist",)


def test_parentheses_post_tags_round_trip_and_save_favorites_without_changing_query_rules(tmp_path):
    from danbooru_multi_search.core.favorites import FavoriteCandidate
    from danbooru_multi_search.danbooru.query_language import QueryParseError, parse_query_expression
    from danbooru_multi_search.danbooru.tag_syntax import TagSyntaxError, normalize_tag_name
    from danbooru_multi_search.storage.database import Database
    from danbooru_multi_search.storage.favorites import FavoriteRepository

    post = replace(_post(), general_tags=(")_(", "(face)_(face)", "series_(nested_(name))"))
    assert decode_post_snapshot(encode_post_snapshot(post)) == post
    database = Database(tmp_path / "parentheses.sqlite3")
    database.initialize()
    repository = FavoriteRepository(database)
    saved = repository.add_favorite(FavoriteCandidate.for_post(post))
    assert repository.get_favorite(saved.id).post == post
    with pytest.raises(PostSnapshotError):
        encode_post_snapshot(replace(post, general_tags=("bad tag",)))
    with pytest.raises(TagSyntaxError, match="括号"):
        normalize_tag_name(")_(")
    with pytest.raises(QueryParseError):
        parse_query_expression("cat AND (dog")


def test_optional_values_and_empty_tag_groups_remain_distinct():
    post = replace(
        _post(),
        preview_url=None,
        large_file_url=None,
        score=None,
        fav_count=None,
        created_at=None,
        file_size=None,
        source=None,
        artist_tags=(),
        character_tags=(),
        copyright_tags=(),
        general_tags=(),
        meta_tags=(),
    )

    assert decode_post_snapshot(encode_post_snapshot(post)) == post


@pytest.mark.parametrize(
    "post",
    (
        replace(_post(), id=0),
        replace(_post(), id=True),
        replace(_post(), preview_url=None, large_file_url=None, file_url=None),
        replace(_post(), score=True),
        replace(_post(), fav_count=-1),
        replace(_post(), fav_count=False),
        replace(_post(), width=0),
        replace(_post(), height=True),
        replace(_post(), file_ext="PNG"),
        replace(_post(), file_ext=" png"),
        replace(_post(), rating="general"),
        replace(_post(), file_size=-1),
        replace(_post(), created_at="2026-11-10T01:02:03Z"),
        replace(_post(), source="   "),
        replace(_post(), general_tags=("Blue_Sky",)),
        replace(_post(), general_tags=("bad tag",)),
        replace(_post(), general_tags=("sample_artist",)),
        replace(_post(), artist_tags=["sample_artist"]),  # type: ignore[arg-type]
    ),
)
def test_encoding_rejects_invalid_or_noncanonical_post_fields(post):
    with pytest.raises(PostSnapshotError):
        encode_post_snapshot(post)


def test_encoding_requires_a_post_model():
    with pytest.raises(TypeError, match="DanbooruPost"):
        encode_post_snapshot({})  # type: ignore[arg-type]


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
def test_decoding_rejects_nonstandard_constants_even_in_unknown_data(constant):
    encoded = encode_post_snapshot(_post())
    invalid = encoded[:-1] + f',"ignored":{constant}' + "}"

    with pytest.raises(PostSnapshotError, match="contains"):
        decode_post_snapshot(invalid)


def test_decoding_rejects_duplicate_keys_and_unknown_or_missing_fields():
    encoded = encode_post_snapshot(_post())
    duplicate = encoded[:-1] + ',"id":42}'
    payload = json.loads(encoded)
    del payload["source"]
    missing = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["source"] = None
    payload["unknown"] = 1
    unknown = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    with pytest.raises(PostSnapshotError, match="duplicate key"):
        decode_post_snapshot(duplicate)
    with pytest.raises(PostSnapshotError, match="fields are invalid"):
        decode_post_snapshot(missing)
    with pytest.raises(PostSnapshotError, match="fields are invalid"):
        decode_post_snapshot(unknown)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("version", 2),
        ("version", True),
        ("id", True),
        ("id", 1.0),
        ("fav_count", False),
        ("width", "2048"),
        ("artist_tags", [1]),
        ("created_at", "2026-11-10T01:02:03+00:00"),
    ),
)
def test_decoding_rejects_invalid_field_types_and_values(field, value):
    payload = json.loads(encode_post_snapshot(_post()))
    payload[field] = value
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    with pytest.raises(PostSnapshotError):
        decode_post_snapshot(encoded)


def test_decoding_rejects_noncanonical_equivalent_json():
    canonical = encode_post_snapshot(_post())
    indented = json.dumps(json.loads(canonical), indent=2, sort_keys=True)

    with pytest.raises(PostSnapshotError, match="not canonical"):
        decode_post_snapshot(indented)


def test_decoding_rejects_huge_integer_without_leaking_value_error():
    encoded = encode_post_snapshot(_post())
    payload = encoded.replace('"id":42', '"id":' + "9" * 5000)

    with pytest.raises(PostSnapshotError, match="Invalid post snapshot JSON"):
        decode_post_snapshot(payload)


def test_decoding_requires_text_and_an_object_root():
    with pytest.raises(TypeError, match="must be text"):
        decode_post_snapshot(None)  # type: ignore[arg-type]
    with pytest.raises(PostSnapshotError, match="JSON object"):
        decode_post_snapshot("[]")


def test_shared_value_rules_keep_unbounded_dimensions_and_media_text():
    post = replace(
        _post(), width=10**60, height=10**60, file_size=10**60,
        preview_url="local-image:preview",
    )
    assert decode_post_snapshot(encode_post_snapshot(post)) == post


def test_encoding_maps_integer_text_limit_to_snapshot_error():
    import sys

    limit = sys.get_int_max_str_digits()
    if limit == 0:
        pytest.skip("the interpreter has disabled its integer text limit")
    with pytest.raises(PostSnapshotError):
        encode_post_snapshot(replace(_post(), width=10**limit))
