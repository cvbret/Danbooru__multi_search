"""Validation shared by saved post values without storage or Qt dependencies."""

from __future__ import annotations

import re
from datetime import datetime

from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.danbooru.tag_syntax import normalize_post_tag


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


class PostValidationError(ValueError):
    """Report an invalid saved-post value independently of its encoding."""


def validate_post_values(post: DanbooruPost) -> None:
    """Validate saved-post values without selecting a storage representation."""

    if not isinstance(post, DanbooruPost):
        raise TypeError("post must be a DanbooruPost")
    _positive_integer(post.id, "id")
    _optional_integer(post.score, "score")
    favorite_count = _optional_integer(post.fav_count, "fav_count")
    if favorite_count is not None and favorite_count < 0:
        raise PostValidationError("fav_count cannot be negative")
    _positive_integer(post.width, "width")
    _positive_integer(post.height, "height")
    file_size = _optional_integer(post.file_size, "file_size")
    if file_size is not None and file_size < 0:
        raise PostValidationError("file_size cannot be negative")

    urls = (
        _validate_optional_stored_text(post.preview_url, "preview_url"),
        _validate_optional_stored_text(post.large_file_url, "large_file_url"),
        _validate_optional_stored_text(post.file_url, "file_url"),
    )
    if not any(urls):
        raise PostValidationError("Post snapshot requires at least one media URL")
    if post.rating not in {"g", "s", "q", "e"}:
        raise PostValidationError("rating is invalid")
    if (
        not isinstance(post.file_ext, str)
        or not post.file_ext
        or post.file_ext.strip().casefold() != post.file_ext
    ):
        raise PostValidationError("file_ext must be canonical non-empty text")

    if post.created_at is not None:
        if (
            not isinstance(post.created_at, str)
            or _CREATED_AT_PATTERN.fullmatch(post.created_at) is None
        ):
            raise PostValidationError("created_at must use fixed UTC text")
        try:
            datetime.fromisoformat(post.created_at.removesuffix("Z") + "+00:00")
        except (OverflowError, ValueError) as exc:
            raise PostValidationError("created_at is not a valid UTC datetime") from exc

    if post.source is not None:
        if not isinstance(post.source, str):
            raise PostValidationError("source must be text or null")
        if not post.source.strip():
            raise PostValidationError("source cannot be blank")
        if len(post.source) > 1200:
            raise PostValidationError("source must not exceed 1200 characters")

    seen_tags: set[str] = set()
    for field_name in _TAG_FIELDS:
        tags = getattr(post, field_name)
        if not isinstance(tags, tuple):
            raise PostValidationError(f"{field_name} must be a tuple")
        for tag in tags:
            if not isinstance(tag, str):
                raise PostValidationError(f"{field_name} must contain text")
            try:
                normalized = normalize_post_tag(tag)
            except (TypeError, ValueError) as exc:
                raise PostValidationError(
                    f"{field_name} contains an invalid tag"
                ) from exc
            if normalized != tag:
                raise PostValidationError(
                    f"{field_name} contains a noncanonical tag"
                )
            if tag in seen_tags:
                raise PostValidationError(f"Post snapshot contains duplicate tag {tag!r}")
            seen_tags.add(tag)

    # JSON previously performed this conversion after validating the values.
    # Preserve the current Python integer-text limit without importing a codec.
    for value in (
        post.id, post.score, post.fav_count, post.width, post.height, post.file_size
    ):
        if value is not None:
            try:
                str(value)
            except ValueError as exc:
                raise PostValidationError(
                    f"Post integer cannot be represented as text: {exc}"
                ) from exc


def _validate_optional_stored_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value.strip() != value:
        raise PostValidationError(f"{label} must be canonical non-empty text or null")
    return value


def _integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise PostValidationError(f"{label} must be an integer")
    return value


def _optional_integer(value: object, label: str) -> int | None:
    return None if value is None else _integer(value, label)


def _positive_integer(value: object, label: str) -> int:
    parsed = _integer(value, label)
    if parsed < 1:
        raise PostValidationError(f"{label} must be positive")
    return parsed
