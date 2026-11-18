"""Pure cursor-aware tag replacement and suggestion merging rules."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from importlib import import_module
from typing import Any

from danbooru_multi_search.danbooru.tag_types import (
    DEFAULT_TAG_SUGGESTION_LIMIT,
    TagSuggestion,
)
from danbooru_multi_search.danbooru.tag_syntax import (
    QUERY_OPERATOR_NAMES,
    TagSyntaxError,
    normalize_tag_fragment,
    normalize_tag_name,
)


TAG_SUGGESTION_DEBOUNCE_MS = 300
_KNOWN_CATEGORIES = frozenset({0, 1, 3, 4, 5})


@dataclass(frozen=True, slots=True)
class ActiveTagFragment:
    text: str
    cursor: int
    start: int
    end: int
    fragment: str
    token_count: int

    @property
    def is_empty(self) -> bool:
        return not self.fragment


@dataclass(frozen=True, slots=True)
class QueryTagFragment:
    """One tag-like token selected from an incomplete M2 expression."""

    text: str
    cursor: int
    start: int
    end: int
    fragment: str

    @property
    def is_empty(self) -> bool:
        return not self.fragment


@dataclass(frozen=True, slots=True)
class _QueryLexeme:
    kind: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class TagReplacement:
    text: str
    cursor: int
    changed: bool
    should_search: bool

    def to_mapping(self) -> dict[str, object]:
        return {
            "text": self.text,
            "cursor": self.cursor,
            "changed": self.changed,
            "shouldSearch": self.should_search,
        }


def locate_active_tag_fragment(text: str, cursor: int) -> ActiveTagFragment:
    """Locate the complete non-whitespace token adjacent to a Qt cursor."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not isinstance(cursor, int) or isinstance(cursor, bool):
        raise TypeError("cursor must be an integer")
    if not 0 <= cursor <= len(text):
        raise ValueError("cursor must be within the text")

    seed: int | None = None
    if cursor < len(text) and not text[cursor].isspace():
        seed = cursor
    elif cursor > 0 and not text[cursor - 1].isspace():
        seed = cursor - 1

    token_count = len(text.split())
    if seed is None:
        return ActiveTagFragment(text, cursor, cursor, cursor, "", token_count)

    start = seed
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    end = seed + 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return ActiveTagFragment(
        text=text,
        cursor=cursor,
        start=start,
        end=end,
        fragment=text[start:end],
        token_count=token_count,
    )


def replace_active_tag_fragment(
    text: str,
    cursor: int,
    tag_name: str,
) -> TagReplacement:
    context = locate_active_tag_fragment(text, cursor)
    if context.is_empty or context.token_count > 2:
        return TagReplacement(text, cursor, False, False)
    if not isinstance(tag_name, str) or not tag_name.isascii():
        return TagReplacement(text, cursor, False, False)
    try:
        normalized_tag = normalize_tag_name(tag_name)
    except TagSyntaxError:
        return TagReplacement(text, cursor, False, False)

    replaced = text[: context.start] + normalized_tag + text[context.end :]
    new_cursor = context.start + len(normalized_tag)
    if len(replaced.split()) > 2:
        return TagReplacement(text, cursor, False, False)
    try:
        for token in replaced.split():
            normalize_tag_name(token)
    except TagSyntaxError:
        should_search = False
    else:
        should_search = replaced.isascii()
    return TagReplacement(replaced, new_cursor, True, should_search)


def locate_query_tag_fragment(text: str, cursor: int) -> QueryTagFragment:
    """Locate a tag token without treating query operators as tag fragments.

    The scanner intentionally accepts incomplete text. Parentheses at the start
    of a token delimit query groups, while balanced parentheses after a tag
    character remain part of a Danbooru tag such as ``touhou_(pc-98)``.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not isinstance(cursor, int) or isinstance(cursor, bool):
        raise TypeError("cursor must be an integer")
    if not 0 <= cursor <= len(text):
        raise ValueError("cursor must be within the text")

    lexemes = _query_lexemes(text)
    for lexeme in lexemes:
        if lexeme.kind == "tag" and lexeme.start <= cursor < lexeme.end:
            return _query_fragment(text, cursor, lexeme)

    # Match the existing search field rule at the immediate right edge of a
    # token. This keeps suggestions active after a tag and before whitespace or
    # a closing query parenthesis, without turning operators into suggestions.
    if cursor > 0 and not text[cursor - 1].isspace():
        for lexeme in lexemes:
            if lexeme.kind == "tag" and lexeme.end == cursor:
                return _query_fragment(text, cursor, lexeme)

    return QueryTagFragment(text, cursor, cursor, cursor, "")


def replace_query_tag_fragment(
    text: str,
    cursor: int,
    tag_name: str,
) -> TagReplacement:
    """Replace only the active M2 tag token and never execute the query."""

    context = locate_query_tag_fragment(text, cursor)
    if context.is_empty:
        return TagReplacement(text, cursor, False, False)
    try:
        normalized_tag = normalize_tag_name(tag_name)
    except (TagSyntaxError, TypeError):
        return TagReplacement(text, cursor, False, False)

    replaced = text[: context.start] + normalized_tag + text[context.end :]
    new_cursor = context.start + len(normalized_tag)
    return TagReplacement(replaced, new_cursor, True, False)


def _query_fragment(
    text: str,
    cursor: int,
    lexeme: _QueryLexeme,
) -> QueryTagFragment:
    return QueryTagFragment(
        text=text,
        cursor=cursor,
        start=lexeme.start,
        end=lexeme.end,
        fragment=text[lexeme.start : lexeme.end],
    )


def _query_lexemes(text: str) -> tuple[_QueryLexeme, ...]:
    lexemes: list[_QueryLexeme] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if character in "()":
            lexemes.append(_QueryLexeme("group", index, index + 1))
            index += 1
            continue

        start = index
        prefix_end = start
        while (
            prefix_end < len(text)
            and not text[prefix_end].isspace()
            and text[prefix_end] not in "()"
        ):
            prefix_end += 1
        if text[start:prefix_end].casefold() in QUERY_OPERATOR_NAMES:
            lexemes.append(_QueryLexeme("operator", start, prefix_end))
            index = prefix_end
            continue

        internal_depth = 0
        while index < len(text):
            character = text[index]
            if character.isspace():
                break
            if character == "(":
                internal_depth += 1
                index += 1
                continue
            if character == ")":
                if internal_depth == 0:
                    break
                internal_depth -= 1
                index += 1
                continue
            index += 1
        lexemes.append(_QueryLexeme("tag", start, index))
    return tuple(lexemes)


def merge_tag_suggestions(
    local: Sequence[TagSuggestion],
    remote: Sequence[TagSuggestion],
    fragment: str,
    *,
    limit: int = DEFAULT_TAG_SUGGESTION_LIMIT,
) -> tuple[TagSuggestion, ...]:
    """Merge field-by-field, then apply deterministic query-specific sorting."""

    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    local_by_name: dict[str, TagSuggestion] = {}
    for suggestion in local:
        local_by_name[suggestion.name] = _merge_same_source(
            local_by_name.get(suggestion.name), suggestion
        )
    remote_by_name: dict[str, TagSuggestion] = {}
    for suggestion in remote:
        remote_by_name[suggestion.name] = _merge_same_source(
            remote_by_name.get(suggestion.name), suggestion
        )

    merged = dict(local_by_name)
    for name, suggestion in remote_by_name.items():
        current = merged.get(name)
        if current is None:
            merged[name] = suggestion
            continue
        merged[name] = TagSuggestion(
            name=name,
            chinese_names=_ordered_union(
                current.chinese_names,
                suggestion.chinese_names,
            ),
            post_count=(
                suggestion.post_count
                if suggestion.post_count is not None
                else current.post_count
            ),
            category=(
                suggestion.category
                if suggestion.category in _KNOWN_CATEGORIES
                else current.category
            ),
        )

    chinese = not fragment.isascii()
    normalized_fragment = fragment.strip()
    if not chinese:
        try:
            normalized_fragment = normalize_tag_fragment(fragment)
        except ValueError:
            return ()

    candidates: list[TagSuggestion] = []
    for suggestion in merged.values():
        if chinese:
            matched_names = [
                name
                for name in suggestion.chinese_names
                if normalized_fragment in name
            ]
            if not matched_names:
                continue
            exact_names = [name for name in matched_names if name == normalized_fragment]
            preferred = exact_names[0] if exact_names else matched_names[0]
            candidates.append(suggestion.prefer_chinese_name(preferred))
        elif suggestion.name.startswith(normalized_fragment):
            candidates.append(suggestion)

    candidates.sort(
        key=lambda item: _merged_sort_key(item, normalized_fragment, chinese)
    )
    return tuple(candidates[:limit])


def _merge_same_source(
    current: TagSuggestion | None,
    incoming: TagSuggestion,
) -> TagSuggestion:
    if current is None:
        return incoming
    categories = {
        value
        for value in (current.category, incoming.category)
        if value in _KNOWN_CATEGORIES
    }
    return TagSuggestion(
        name=incoming.name,
        chinese_names=_ordered_union(
            current.chinese_names,
            incoming.chinese_names,
        ),
        post_count=max(
            (
                value
                for value in (current.post_count, incoming.post_count)
                if value is not None
            ),
            default=None,
        ),
        category=next(iter(categories)) if len(categories) == 1 else None,
    )


def _ordered_union(first: Sequence[str], second: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*first, *second)))


def _merged_sort_key(
    suggestion: TagSuggestion,
    fragment: str,
    chinese: bool,
) -> tuple[int, bool, int, str]:
    exact = (
        fragment in suggestion.chinese_names
        if chinese
        else suggestion.name == fragment
    )
    return (
        0 if exact else 1,
        suggestion.post_count is None,
        -(suggestion.post_count or 0),
        suggestion.name,
    )


_LEGACY_EXPORTS = {
    "DanbooruTagSuggestionController": "danbooru_multi_search.ui.autocomplete",
    "TagSuggestionListModel": "danbooru_multi_search.ui.autocomplete",
    "TagSuggestionRole": "danbooru_multi_search.ui.autocomplete",
    "TagImportOutcome": "danbooru_multi_search.app.tag_suggestions",
}


def __getattr__(name: str) -> Any:
    """Resolve only explicitly requested historical UI and application names."""
    module_name = _LEGACY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LEGACY_EXPORTS))
