"""Strict validation and parsing for Danbooru post pages."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.danbooru.tag_syntax import normalize_tag_name


class DanbooruParseError(ValueError):
    """Raised when Danbooru returns JSON that cannot form the required model."""


_CREATED_AT_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?"
    r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
_TAG_FIELDS = (
    ("tag_string_artist", "artist_tags"),
    ("tag_string_character", "character_tags"),
    ("tag_string_copyright", "copyright_tags"),
    ("tag_string_general", "general_tags"),
    ("tag_string_meta", "meta_tags"),
)


def parse_posts_response(payload: bytes) -> tuple[DanbooruPost, ...]:
    """Parse a Danbooru page while retaining the M1 optional-tag behavior."""

    return _parse_posts_response(payload, require_complete_tags=False)


def parse_complete_posts_response(payload: bytes) -> tuple[DanbooruPost, ...]:
    """Parse an M2 page whose five complete tag fields are all present."""

    return _parse_posts_response(payload, require_complete_tags=True)


def _parse_posts_response(
    payload: bytes,
    *,
    require_complete_tags: bool,
) -> tuple[DanbooruPost, ...]:

    try:
        raw = json.loads(payload, parse_constant=_reject_json_constant)
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
        posts.append(
            _parse_post(
                item,
                index,
                require_complete_tags=require_complete_tags,
            )
        )
    return tuple(posts)


def _parse_post(
    raw: Mapping[str, Any],
    index: int,
    *,
    require_complete_tags: bool,
) -> DanbooruPost:
    preview_url = _optional_string(raw, "preview_file_url", index)
    large_file_url = _optional_string(raw, "large_file_url", index)
    file_url = _optional_string(raw, "file_url", index)
    if not any((preview_url, large_file_url, file_url)):
        raise DanbooruParseError(
            f"Post at index {index} has no usable preview, large, or file URL."
        )

    tag_groups = _tag_groups(
        raw,
        index,
        require_complete=require_complete_tags,
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
        file_ext=_required_string(raw, "file_ext", index).casefold(),
        created_at=_created_at(raw, index),
        file_size=_optional_integer(raw, "file_size", index, minimum=0),
        source=_source(raw, index),
        **tag_groups,
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant is not allowed: {value}")


def _created_at(raw: Mapping[str, Any], index: int) -> str | None:
    value = raw.get("created_at")
    if value is None:
        return None
    if not isinstance(value, str) or _CREATED_AT_PATTERN.fullmatch(value) is None:
        raise DanbooruParseError(
            f"Post at index {index} field 'created_at' must be a timezone-aware "
            "ISO 8601 timestamp."
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        offset = parsed.utcoffset()
        if offset is None:
            raise ValueError("timestamp has no UTC offset")
        normalized = parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise DanbooruParseError(
            f"Post at index {index} field 'created_at' is not a valid timestamp."
        ) from exc
    return normalized.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _source(raw: Mapping[str, Any], index: int) -> str | None:
    value = raw.get("source")
    if value is None:
        return None
    if not isinstance(value, str):
        raise DanbooruParseError(
            f"Post at index {index} field 'source' must be a string or null."
        )
    if len(value) > 1200:
        raise DanbooruParseError(
            f"Post at index {index} field 'source' must not exceed 1200 characters."
        )
    return value if value.strip() else None


def _tag_groups(
    raw: Mapping[str, Any],
    index: int,
    *,
    require_complete: bool,
) -> dict[str, tuple[str, ...]]:
    parsed_groups: dict[str, tuple[str, ...]] = {}
    seen: set[str] = set()
    for field_name, model_name in _TAG_FIELDS:
        if field_name not in raw:
            if require_complete:
                raise DanbooruParseError(
                    f"Post at index {index} field '{field_name}' must be present "
                    "and contain a string."
                )
            parsed_groups[model_name] = ()
            continue
        value = raw[field_name]
        if value is None and not require_complete:
            parsed_groups[model_name] = ()
            continue
        if not isinstance(value, str):
            expected = "a string" if require_complete else "a string or null"
            raise DanbooruParseError(
                f"Post at index {index} field '{field_name}' must be {expected}."
            )
        if any(ord(character) < 32 or ord(character) > 126 for character in value):
            raise DanbooruParseError(
                f"Post at index {index} field '{field_name}' contains a non-ASCII "
                "or control character."
            )
        if not value.strip(" "):
            parsed_groups[model_name] = ()
            continue

        normalized_tags: list[str] = []
        for raw_tag in (part for part in value.split(" ") if part):
            try:
                tag = normalize_tag_name(raw_tag)
            except ValueError as exc:
                raise DanbooruParseError(
                    f"Post at index {index} field '{field_name}' contains an "
                    f"invalid tag: {exc}"
                ) from exc
            if tag in seen:
                raise DanbooruParseError(
                    f"Post at index {index} contains duplicate tag '{tag}'."
                )
            seen.add(tag)
            normalized_tags.append(tag)
        parsed_groups[model_name] = tuple(normalized_tags)
    return parsed_groups


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
