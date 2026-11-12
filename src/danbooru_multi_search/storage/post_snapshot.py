"""Strict canonical snapshots for persisted :class:`DanbooruPost` values."""

from __future__ import annotations

import json
import math
from typing import Any

from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.danbooru.post_validation import (
    PostValidationError,
    validate_post_values,
)


POST_SNAPSHOT_VERSION = 1

_SNAPSHOT_FIELDS = frozenset(
    {
        "artist_tags",
        "character_tags",
        "copyright_tags",
        "created_at",
        "fav_count",
        "file_ext",
        "file_size",
        "file_url",
        "general_tags",
        "height",
        "id",
        "large_file_url",
        "meta_tags",
        "preview_url",
        "rating",
        "score",
        "source",
        "version",
        "width",
    }
)
class PostSnapshotError(ValueError):
    """Report a stable persisted-post validation or JSON error."""


def encode_post_snapshot(post: DanbooruPost) -> str:
    """Return the sole canonical version-1 JSON representation of ``post``."""

    _validate_post(post)
    payload = {
        "artist_tags": list(post.artist_tags),
        "character_tags": list(post.character_tags),
        "copyright_tags": list(post.copyright_tags),
        "created_at": post.created_at,
        "fav_count": post.fav_count,
        "file_ext": post.file_ext,
        "file_size": post.file_size,
        "file_url": post.file_url,
        "general_tags": list(post.general_tags),
        "height": post.height,
        "id": post.id,
        "large_file_url": post.large_file_url,
        "meta_tags": list(post.meta_tags),
        "preview_url": post.preview_url,
        "rating": post.rating,
        "score": post.score,
        "source": post.source,
        "version": POST_SNAPSHOT_VERSION,
        "width": post.width,
    }
    return _dump_canonical(payload)


def decode_post_snapshot(encoded: str) -> DanbooruPost:
    """Decode canonical JSON and reject equivalent noncanonical text."""

    payload = _load_object(encoded)
    actual_fields = frozenset(payload)
    if actual_fields != _SNAPSHOT_FIELDS:
        missing = sorted(_SNAPSHOT_FIELDS - actual_fields)
        unknown = sorted(actual_fields - _SNAPSHOT_FIELDS)
        raise PostSnapshotError(
            "Post snapshot fields are invalid; "
            f"missing={missing}, unknown={unknown}"
        )
    if type(payload["version"]) is not int or payload["version"] != (
        POST_SNAPSHOT_VERSION
    ):
        raise PostSnapshotError("Unsupported post snapshot version")

    try:
        post = DanbooruPost(
            id=_integer(payload["id"], "id"),
            preview_url=_optional_text(payload["preview_url"], "preview_url"),
            large_file_url=_optional_text(
                payload["large_file_url"], "large_file_url"
            ),
            file_url=_optional_text(payload["file_url"], "file_url"),
            score=_optional_integer(payload["score"], "score"),
            fav_count=_optional_integer(payload["fav_count"], "fav_count"),
            rating=_text(payload["rating"], "rating"),
            width=_integer(payload["width"], "width"),
            height=_integer(payload["height"], "height"),
            file_ext=_text(payload["file_ext"], "file_ext"),
            created_at=_optional_text(payload["created_at"], "created_at"),
            file_size=_optional_integer(payload["file_size"], "file_size"),
            source=_optional_text(payload["source"], "source"),
            artist_tags=_text_tuple(payload["artist_tags"], "artist_tags"),
            character_tags=_text_tuple(
                payload["character_tags"], "character_tags"
            ),
            copyright_tags=_text_tuple(
                payload["copyright_tags"], "copyright_tags"
            ),
            general_tags=_text_tuple(payload["general_tags"], "general_tags"),
            meta_tags=_text_tuple(payload["meta_tags"], "meta_tags"),
        )
        _validate_post(post)
    except PostSnapshotError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise PostSnapshotError(f"Invalid post snapshot: {exc}") from exc

    if encode_post_snapshot(post) != encoded:
        raise PostSnapshotError("Post snapshot JSON is not canonical")
    return post


def _validate_post(post: DanbooruPost) -> None:
    try:
        validate_post_values(post)
    except PostValidationError as exc:
        raise PostSnapshotError(str(exc)) from exc


def _load_object(encoded: str) -> dict[str, Any]:
    if not isinstance(encoded, str):
        raise TypeError("Post snapshot JSON must be text")

    def reject_constant(value: str):
        raise PostSnapshotError(f"Post snapshot JSON contains {value}")

    def reject_duplicate_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PostSnapshotError(
                    f"Post snapshot JSON contains duplicate key {key!r}"
                )
            result[key] = value
        return result

    def parse_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise PostSnapshotError(
                "Post snapshot JSON contains a non-finite number"
            )
        return parsed

    try:
        value = json.loads(
            encoded,
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_constant,
            parse_float=parse_float,
        )
    except PostSnapshotError:
        raise
    except (
        json.JSONDecodeError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as exc:
        raise PostSnapshotError(f"Invalid post snapshot JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PostSnapshotError("Post snapshot must be a JSON object")
    return value


def _dump_canonical(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise PostSnapshotError(f"Unable to encode post snapshot JSON: {exc}") from exc


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise PostSnapshotError(f"{label} must be text")
    return value


def _optional_text(value: object, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _text_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PostSnapshotError(f"{label} must be a JSON string array")
    return tuple(value)


def _integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise PostSnapshotError(f"{label} must be an integer")
    return value


def _optional_integer(value: object, label: str) -> int | None:
    return None if value is None else _integer(value, label)
