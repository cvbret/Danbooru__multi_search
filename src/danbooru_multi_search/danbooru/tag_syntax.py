"""Shared validation for one concrete Danbooru tag name."""

from __future__ import annotations


_BASE_METATAG_NAMES = frozenset(
    {
        "age",
        "ai",
        "appealer",
        "approver",
        "artcomm",
        "child",
        "comm",
        "comment",
        "commentary",
        "commentaryupdater",
        "commenter",
        "date",
        "disapproved",
        "downvote",
        "downvotes",
        "duration",
        "embedded",
        "exif",
        "fav",
        "favcount",
        "favgroup",
        "filetype",
        "filesize",
        "flagger",
        "height",
        "has",
        "id",
        "is",
        "limit",
        "md5",
        "mpixels",
        "note",
        "noter",
        "noteupdater",
        "order",
        "ordfav",
        "ordfavgroup",
        "ordpool",
        "ordvote",
        "parent",
        "pixelhash",
        "pixiv_id",
        "pixiv",
        "pool",
        "random",
        "rating",
        "reacted",
        "ratio",
        "score",
        "search",
        "source",
        "status",
        "tagcount",
        "unaliased",
        "upvote",
        "upvotes",
        "user",
        "width",
    }
)
_COUNT_METATAG_NAMES = frozenset(
    {
        "comment_count",
        "deleted_comment_count",
        "active_comment_count",
        "note_count",
        "deleted_note_count",
        "active_note_count",
        "flag_count",
        "child_count",
        "deleted_child_count",
        "active_child_count",
        "pool_count",
        "deleted_pool_count",
        "active_pool_count",
        "series_pool_count",
        "collection_pool_count",
        "appeal_count",
        "approval_count",
        "replacement_count",
        "disapproval_count",
    }
)
_COUNT_METATAG_SYNONYMS = frozenset(
    {
        "comments",
        "deleted_comments",
        "active_comments",
        "notes",
        "deleted_notes",
        "active_notes",
        "flags",
        "children",
        "deleted_children",
        "active_children",
        "pools",
        "deleted_pools",
        "active_pools",
        "series_pools",
        "collection_pools",
        "appeals",
        "approvals",
        "replacements",
        "disapprovals",
    }
)
_CATEGORY_COUNT_METATAG_NAMES = frozenset(
    {"gentags", "arttags", "copytags", "chartags", "metatags"}
)

# Mirrors Danbooru PostQueryBuilder::METATAGS. Rails-derived groups are
# expanded so validation does not depend on ActiveSupport inflection rules.
METATAG_NAMES = (
    _BASE_METATAG_NAMES
    | _COUNT_METATAG_NAMES
    | _COUNT_METATAG_SYNONYMS
    | _CATEGORY_COUNT_METATAG_NAMES
)
QUERY_OPERATOR_NAMES = frozenset({"and", "or", "not"})


class TagSyntaxError(ValueError):
    """Report one stable reason why a concrete tag name is invalid."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def metatag_name(value: str) -> str | None:
    """Return the recognized search metatag prefix, if one is present."""

    if not isinstance(value, str) or not value.isascii():
        return None
    name, separator, _remainder = value.casefold().partition(":")
    return name if separator and name in METATAG_NAMES else None


def has_valid_internal_parentheses(value: str) -> bool:
    """Validate the single-level balanced parentheses allowed inside tags."""

    if value.startswith("("):
        return False
    depth = 0
    content_at_depth = 0
    for character in value:
        if character == "(":
            depth += 1
            if depth > 1:
                return False
            content_at_depth = 0
        elif character == ")":
            if depth != 1 or content_at_depth == 0:
                return False
            depth = 0
        elif depth == 1:
            content_at_depth += 1
    return depth == 0


def normalize_post_tag(value: str) -> str:
    """Normalize a returned tag value without interpreting query punctuation."""

    if not isinstance(value, str):
        raise TagSyntaxError("tag_not_text", "Tag name must be text.")
    stripped = value.strip()
    if not stripped:
        raise TagSyntaxError("empty_tag", "Tag name cannot be empty.")
    if any(character.isspace() for character in stripped):
        raise TagSyntaxError(
            "tag_contains_whitespace",
            "Tag name must contain exactly one tag.",
        )
    if not stripped.isascii():
        raise TagSyntaxError(
            "non_ascii_tag",
            "当前阶段只支持 ASCII 英文标签。",
        )
    if any(ord(character) < 33 or ord(character) > 126 for character in stripped):
        raise TagSyntaxError(
            "unsupported_tag_character",
            "标签包含不支持的控制字符。",
        )
    return stripped.casefold()


def normalize_tag_name(value: str) -> str:
    """Validate and normalize one non-metatag query tag name."""

    normalized = normalize_post_tag(value)
    if normalized.startswith(("-", "~")):
        raise TagSyntaxError(
            "search_prefix_not_allowed",
            "当前阶段不支持排除标签或 OR 前缀。",
        )
    if "*" in normalized:
        raise TagSyntaxError(
            "wildcard_not_allowed",
            "当前阶段不支持通配符查询。",
        )
    if normalized in QUERY_OPERATOR_NAMES:
        raise TagSyntaxError(
            "reserved_query_operator",
            "AND、OR 和 NOT 是保留的查询运算符。",
        )
    if not has_valid_internal_parentheses(normalized):
        raise TagSyntaxError(
            "invalid_tag_parentheses",
            "当前阶段不支持括号查询表达式。",
        )
    recognized_metatag = metatag_name(normalized)
    if recognized_metatag is not None:
        raise TagSyntaxError(
            "metatag_not_allowed",
            f"当前阶段不支持元标签 {recognized_metatag}:。",
        )
    return normalized


def normalize_tag_fragment(value: str) -> str:
    """Normalize an incomplete ASCII fragment without damaging punctuation."""

    if not isinstance(value, str):
        raise ValueError("Tag fragment must be text.")
    stripped = value.strip()
    if not stripped:
        raise ValueError("Tag fragment cannot be empty.")
    if not stripped.isascii():
        raise ValueError("Remote tag fragments must contain only ASCII characters.")
    if any(character.isspace() for character in stripped):
        raise ValueError("Tag fragment cannot contain whitespace.")
    if any(ord(character) < 33 or ord(character) > 126 for character in stripped):
        raise ValueError("Tag fragment contains unsupported control characters.")
    if stripped.startswith(("-", "~")) or "*" in stripped:
        raise ValueError("Tag fragment contains unsupported search syntax.")
    if stripped.casefold() in QUERY_OPERATOR_NAMES:
        raise ValueError("Tag fragment contains an unsupported query operator.")
    return stripped.casefold()
