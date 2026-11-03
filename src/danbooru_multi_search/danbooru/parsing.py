"""Strict validation and parsing for Danbooru post pages."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from danbooru_multi_search.danbooru.models import DanbooruPost


class DanbooruParseError(ValueError):
    """Raised when Danbooru returns JSON that cannot form the required model."""


def parse_posts_response(payload: bytes) -> tuple[DanbooruPost, ...]:
    """Parse and validate a Danbooru ``posts.json`` response."""

    try:
        raw = json.loads(payload)
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise DanbooruParseError("Danbooru response is not valid JSON.") from exc

    if not isinstance(raw, list):
        raise DanbooruParseError("Danbooru response root must be a JSON array.")

    posts: list[DanbooruPost] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise DanbooruParseError(f"Post at index {index} must be a JSON object.")
        posts.append(_parse_post(item, index))
    return tuple(posts)


def _parse_post(raw: Mapping[str, Any], index: int) -> DanbooruPost:
    preview_url = _optional_string(raw, "preview_file_url", index)
    large_file_url = _optional_string(raw, "large_file_url", index)
    file_url = _optional_string(raw, "file_url", index)
    if not any((preview_url, large_file_url, file_url)):
        raise DanbooruParseError(
            f"Post at index {index} has no usable preview, large, or file URL."
        )

    return DanbooruPost(
        id=_required_integer(raw, "id", index, minimum=1),
        preview_url=preview_url,
        large_file_url=large_file_url,
        file_url=file_url,
        score=_optional_integer(raw, "score", index),
        fav_count=_optional_integer(raw, "fav_count", index, minimum=0),
        rating=_rating(raw, index),
        width=_required_integer(raw, "image_width", index, minimum=1),
        height=_required_integer(raw, "image_height", index, minimum=1),
        file_ext=_required_string(raw, "file_ext", index),
    )


def _required_integer(
    raw: Mapping[str, Any],
    field_name: str,
    index: int,
    *,
    minimum: int | None = None,
) -> int:
    value = raw.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise DanbooruParseError(
            f"Post at index {index} field '{field_name}' must be an integer."
        )
    if minimum is not None and value < minimum:
        raise DanbooruParseError(
            f"Post at index {index} field '{field_name}' must be at least {minimum}."
        )
    return value


def _required_string(
    raw: Mapping[str, Any],
    field_name: str,
    index: int,
) -> str:
    value = raw.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise DanbooruParseError(
            f"Post at index {index} field '{field_name}' must be a non-empty string."
        )
    return value.strip()


def _optional_integer(
    raw: Mapping[str, Any],
    field_name: str,
    index: int,
    *,
    minimum: int | None = None,
) -> int | None:
    value = raw.get(field_name)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise DanbooruParseError(
            f"Post at index {index} field '{field_name}' must be an integer or null."
        )
    if minimum is not None and value < minimum:
        raise DanbooruParseError(
            f"Post at index {index} field '{field_name}' must be at least {minimum}."
        )
    return value


def _rating(raw: Mapping[str, Any], index: int) -> str:
    rating = _required_string(raw, "rating", index).strip().casefold()
    if rating not in {"g", "s", "q", "e"}:
        raise DanbooruParseError(
            f"Post at index {index} field 'rating' is not a supported rating."
        )
    return rating


def _optional_string(
    raw: Mapping[str, Any],
    field_name: str,
    index: int,
) -> str | None:
    value = raw.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise DanbooruParseError(
            f"Post at index {index} field '{field_name}' must be a string or null."
        )
    stripped = value.strip()
    return stripped or None
