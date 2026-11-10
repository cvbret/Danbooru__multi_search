"""Strict canonical snapshots for persisted :class:`DanbooruPost` values."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any

from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.danbooru.tag_syntax import normalize_tag_name


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
_TAG_FIELDS = (
    "artist_tags",
    "character_tags",
    "copyright_tags",
    "general_tags",
    "meta_tags",
)
_CREATED_AT_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z"
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
    if not isinstance(post, DanbooruPost):
        raise TypeError("post must be a DanbooruPost")
    _positive_integer(post.id, "id")
    _optional_integer(post.score, "score")
    favorite_count = _optional_integer(post.fav_count, "fav_count")
    if favorite_count is not None and favorite_count < 0:
        raise PostSnapshotError("fav_count cannot be negative")
    _positive_integer(post.width, "width")
    _positive_integer(post.height, "height")
    file_size = _optional_integer(post.file_size, "file_size")
    if file_size is not None and file_size < 0:
        raise PostSnapshotError("file_size cannot be negative")

    urls = (
        _validate_optional_stored_text(post.preview_url, "preview_url"),
        _validate_optional_stored_text(post.large_file_url, "large_file_url"),
        _validate_optional_stored_text(post.file_url, "file_url"),
    )
    if not any(urls):
        raise PostSnapshotError("Post snapshot requires at least one media URL")
    if post.rating not in {"g", "s", "q", "e"}:
        raise PostSnapshotError("rating is invalid")
    if (
        not isinstance(post.file_ext, str)
        or not post.file_ext
        or post.file_ext.strip().casefold() != post.file_ext
    ):
        raise PostSnapshotError("file_ext must be canonical non-empty text")

    if post.created_at is not None:
        if (
            not isinstance(post.created_at, str)
            or _CREATED_AT_PATTERN.fullmatch(post.created_at) is None
        ):
            raise PostSnapshotError("created_at must use fixed UTC text")
        try:
            datetime.fromisoformat(post.created_at.removesuffix("Z") + "+00:00")
        except (OverflowError, ValueError) as exc:
            raise PostSnapshotError("created_at is not a valid UTC datetime") from exc

    if post.source is not None:
        if not isinstance(post.source, str):
            raise PostSnapshotError("source must be text or null")
        if not post.source.strip():
            raise PostSnapshotError("source cannot be blank")
        if len(post.source) > 1200:
            raise PostSnapshotError("source must not exceed 1200 characters")

    seen_tags: set[str] = set()
    for field_name in _TAG_FIELDS:
        tags = getattr(post, field_name)
        if not isinstance(tags, tuple):
            raise PostSnapshotError(f"{field_name} must be a tuple")
        for tag in tags:
            if not isinstance(tag, str):
                raise PostSnapshotError(f"{field_name} must contain text")
            try:
                normalized = normalize_tag_name(tag)
            except (TypeError, ValueError) as exc:
                raise PostSnapshotError(
                    f"{field_name} contains an invalid tag"
                ) from exc
            if normalized != tag:
                raise PostSnapshotError(
                    f"{field_name} contains a noncanonical tag"
                )
            if tag in seen_tags:
                raise PostSnapshotError(f"Post snapshot contains duplicate tag {tag!r}")
            seen_tags.add(tag)


def _validate_optional_stored_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value.strip() != value:
        raise PostSnapshotError(f"{label} must be canonical non-empty text or null")
    return value


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


def _positive_integer(value: object, label: str) -> int:
    parsed = _integer(value, label)
    if parsed < 1:
        raise PostSnapshotError(f"{label} must be positive")
    return parsed
