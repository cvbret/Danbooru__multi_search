"""Frozen schema-v3 conversion of version-2 simple-query history.

These rules describe the format already written by the v3 migration. Keep them
independent of current query models, parsers, tag rules, and application defaults.
"""

from __future__ import annotations

import json


# Historical PostQueryBuilder metatag names recognized when schema v3 shipped.
# This set must not track future changes to the current query language.
_METATAG_NAMES = frozenset(
    """
    age ai appealer approver artcomm child comm comment commentary
    commentaryupdater commenter date disapproved downvote downvotes duration
    embedded exif fav favcount favgroup filetype filesize flagger height has id
    is limit md5 mpixels note noter noteupdater order ordfav ordfavgroup ordpool
    ordvote parent pixelhash pixiv_id pixiv pool random rating reacted ratio
    score search source status tagcount unaliased upvote upvotes user width
    comment_count deleted_comment_count active_comment_count note_count
    deleted_note_count active_note_count flag_count child_count
    deleted_child_count active_child_count pool_count deleted_pool_count
    active_pool_count series_pool_count collection_pool_count appeal_count
    approval_count replacement_count disapproval_count comments deleted_comments
    active_comments notes deleted_notes active_notes flags children
    deleted_children active_children pools deleted_pools active_pools
    series_pools collection_pools appeals approvals replacements disapprovals
    gentags arttags copytags chartags metatags
    """.split()
)
_RATINGS = frozenset({"all", "general", "sensitive", "questionable", "explicit"})


def version_three_definition_json(
    encoded_tags: str,
    rating: str,
    page_size: int,
) -> str:
    """Convert canonical v2 tags into the historical version-1 definition JSON."""

    tags = _decode_version_two_tags(encoded_tags)
    if rating not in _RATINGS:
        raise ValueError("Invalid version-2 query rating")
    if type(page_size) is not int or not 1 <= page_size <= 200:
        raise ValueError("Invalid version-2 query page size")
    expression = " AND ".join(tags)
    if len(expression) > 2_048:
        raise ValueError("Version-2 query expression exceeds 2048 characters")
    # Each tag is one node; a group of two or more tags adds one AND node.
    if len(tags) + (len(tags) > 1) > 128:
        raise ValueError("Version-2 query expression exceeds 128 nodes")
    payload = {
        "filter": {
            "blocked_tags": [],
            "file_categories": None,
            "maximum_age": None,
            "maximum_aspect_ratio": None,
            "minimum_age": None,
            "minimum_aspect_ratio": None,
            "minimum_fav_count": None,
            "minimum_height": None,
            "minimum_score": None,
            "minimum_width": None,
            "orientations": None,
            "published_from_utc": None,
            "published_until_utc": None,
        },
        "limits": {
            "active_time_limit_seconds": 60.0,
            "branch_page_limit": 10,
            "candidate_limit": 1_000,
            "page_size": page_size,
        },
        "mode": "standard",
        "normalized_expression": expression,
        "rating": rating,
        "sort": {"direction": "original", "field": "source_order"},
        "version": 1,
    }
    return _canonical_json(payload)


def _decode_version_two_tags(encoded: str) -> tuple[str, ...]:
    if not isinstance(encoded, str):
        raise TypeError("Version-2 normalized tags JSON must be text")
    try:
        tags = json.loads(encoded)
    except (ValueError, RecursionError) as exc:
        raise ValueError("Invalid version-2 normalized tags JSON") from exc
    if (
        not isinstance(tags, list)
        or not tags
        or any(not isinstance(tag, str) for tag in tags)
    ):
        raise ValueError("Version-2 normalized tags must be a nonempty string array")
    canonical = tuple(sorted({_normalize_version_two_tag(tag) for tag in tags}))
    if _canonical_json(canonical) != encoded:
        raise ValueError("Version-2 normalized tags JSON is not canonical")
    return canonical


def _normalize_version_two_tag(tag: str) -> str:
    stripped = tag.strip()
    if not stripped or not stripped.isascii() or any(
        character.isspace() or not 33 <= ord(character) <= 126
        for character in stripped
    ):
        raise ValueError("Invalid version-2 tag characters")
    if stripped.startswith(("-", "~")) or "*" in stripped:
        raise ValueError("Version-2 tags cannot contain search prefixes or wildcards")
    normalized = stripped.casefold()
    if normalized in {"and", "or", "not"}:
        raise ValueError("Version-2 tags cannot be query operators")
    if not _valid_internal_parentheses(stripped):
        raise ValueError("Invalid version-2 tag parentheses")
    prefix, separator, _value = normalized.partition(":")
    if separator and prefix in _METATAG_NAMES:
        raise ValueError("Version-2 tags cannot be search metatags")
    return normalized


def _valid_internal_parentheses(tag: str) -> bool:
    if tag.startswith("("):
        return False
    depth = 0
    content = 0
    for character in tag:
        if character == "(":
            depth += 1
            if depth > 1:
                return False
            content = 0
        elif character == ")":
            if depth != 1 or content == 0:
                return False
            depth = 0
        elif depth == 1:
            content += 1
    return depth == 0


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
