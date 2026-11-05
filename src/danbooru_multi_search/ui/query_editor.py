"""Pure state and tree operations for the M2 visual query editor."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from danbooru_multi_search.danbooru.query_language import (
    AndNode,
    NotNode,
    OrNode,
    ParsedQuery,
    QueryNode,
    QueryParseError,
    SourceSpan,
    TagNode,
    analyze_query,
    format_query_expression,
    parse_query_expression,
)
from danbooru_multi_search.danbooru.query_planning import (
    QueryPlanError,
    build_standard_query_plan,
)
from danbooru_multi_search.danbooru.tag_syntax import (
    TagSyntaxError,
    normalize_tag_name,
)


NodePath: TypeAlias = tuple[int, ...]
_EMPTY_SPAN = SourceSpan(0, 0)


class QueryGroupOperator(StrEnum):
    AND = "and"
    OR = "or"


class ArtistCombinationMode(StrEnum):
    ALL = "all"
    ANY = "any"
    EXCLUDE = "exclude"


@dataclass(frozen=True, slots=True)
class QueryEditorDiagnostic:
    """Stable editor failure with a zero-based half-open source range."""

    code: str
    message: str
    span: SourceSpan

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("diagnostic code must be non-empty text")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("diagnostic message must be non-empty text")
        if not isinstance(self.span, SourceSpan):
            raise TypeError("diagnostic span must be SourceSpan")


class QueryEditorError(ValueError):
    """Reject one visual edit without changing the last valid tree."""

    def __init__(self, diagnostic: QueryEditorDiagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic

    @property
    def code(self) -> str:
        return self.diagnostic.code


@dataclass(frozen=True, slots=True)
class ConfirmedArtistTag:
    """A normalized tag whose source confirms Danbooru category 1."""

    name: str
    category: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.category, int)
            or isinstance(self.category, bool)
            or self.category != 1
        ):
            raise QueryEditorError(
                _diagnostic(
                    "artist_category_required",
                    "多画师条件只接受已确认分类为画师的标签。",
                )
            )
        try:
            normalized = normalize_tag_name(self.name)
        except (TagSyntaxError, TypeError) as exc:
            raise QueryEditorError(
                _diagnostic("invalid_artist_tag", f"画师标签无效：{exc}")
            ) from exc
        object.__setattr__(self, "name", normalized)


@dataclass(frozen=True, slots=True)
class QueryEditorSnapshot:
    """Complete state that a non-QObject bridge can read after one operation."""

    root: QueryNode | None
    canonical_text: str
    draft_text: str
    text_diagnostic: QueryEditorDiagnostic | None
    revision: int

    @property
    def has_text_error(self) -> bool:
        return self.text_diagnostic is not None


class QueryTreeEditor:
    """Keep one valid AST while preserving an invalid advanced-text draft."""

    def __init__(
        self,
        expression: str | ParsedQuery | QueryNode | None = None,
    ) -> None:
        self._root: QueryNode | None = None
        self._canonical_text = ""
        self._draft_text = ""
        self._text_diagnostic: QueryEditorDiagnostic | None = None
        self._revision = 0
        if expression is None:
            return
        try:
            parsed = (
                _parse_and_validate(expression)
                if isinstance(expression, str)
                else _validate_visual_root(_root_of(expression))
            )
        except (QueryParseError, QueryPlanError) as exc:
            source = expression if isinstance(expression, str) else ""
            raise QueryEditorError(_from_query_error(exc, source)) from exc
        self._commit(parsed)

    @property
    def root(self) -> QueryNode | None:
        return self._root

    @property
    def canonical_text(self) -> str:
        return self._canonical_text

    @property
    def draft_text(self) -> str:
        return self._draft_text

    @property
    def text_diagnostic(self) -> QueryEditorDiagnostic | None:
        return self._text_diagnostic

    @property
    def has_text_error(self) -> bool:
        return self._text_diagnostic is not None

    @property
    def visual_editing_enabled(self) -> bool:
        return self._text_diagnostic is None

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def snapshot(self) -> QueryEditorSnapshot:
        return QueryEditorSnapshot(
            self._root,
            self._canonical_text,
            self._draft_text,
            self._text_diagnostic,
            self._revision,
        )

    def apply_advanced_text(self, text: str) -> bool:
        """Apply valid text, or retain raw text and the previous valid tree."""

        if not isinstance(text, str):
            raise TypeError("advanced query text must be text")
        self._draft_text = text
        self._revision += 1
        try:
            parsed = _parse_and_validate(text)
        except (QueryParseError, QueryPlanError) as exc:
            self._text_diagnostic = _from_query_error(exc, text)
            return False
        self._commit(parsed, increment=False)
        return True

    def discard_invalid_text(self) -> bool:
        """Explicitly return to the last valid visual tree text."""

        if self._text_diagnostic is None:
            return False
        self._draft_text = self._canonical_text
        self._text_diagnostic = None
        self._revision += 1
        return True

    def replace_root(self, root: QueryNode | ParsedQuery | None) -> None:
        """Atomically replace the tree; ``None`` is an unfilled editor."""

        self._require_visual_editing()
        if root is None:
            self._root = None
            self._canonical_text = ""
            self._draft_text = ""
            self._revision += 1
            return
        self._commit(_validate_visual_root(_root_of(root)))

    def insert_subtree(
        self,
        parent_path: NodePath | None,
        subtree: QueryNode | ParsedQuery,
        *,
        index: int | None = None,
    ) -> None:
        """Insert below a selected group, or below the root AND group."""

        self._require_visual_editing()
        inserted = _root_of(subtree)
        if parent_path is None:
            if self._root is None:
                candidate = inserted
            elif isinstance(self._root, AndNode):
                candidate = _insert_child(self._root, inserted, index)
            else:
                if index not in {None, 0, 1}:
                    raise QueryEditorError(
                        _diagnostic(
                            "invalid_insert_index",
                            "根条件组的新条件只能放在现有条件之前或之后。",
                        )
                    )
                operands = (
                    (inserted, self._root)
                    if index == 0
                    else (self._root, inserted)
                )
                candidate = AndNode(operands, _EMPTY_SPAN)
        else:
            path = _path(parent_path)
            group = _node_at(self._root, path)
            if not isinstance(group, (AndNode, OrNode)):
                raise QueryEditorError(
                    _diagnostic(
                        "target_not_group",
                        "新条件只能插入 AND 或 OR 条件组。",
                    )
                )
            candidate = _replace_at(
                self._root,
                path,
                _insert_child(group, inserted, index),
            )
        self._commit(_validate_visual_root(candidate))

    def add_tag(
        self,
        parent_path: NodePath | None,
        name: str,
        *,
        index: int | None = None,
    ) -> None:
        try:
            node = TagNode(name, _EMPTY_SPAN)
        except (TagSyntaxError, TypeError) as exc:
            raise QueryEditorError(
                _diagnostic("invalid_tag", f"标签无效：{exc}")
            ) from exc
        self.insert_subtree(parent_path, node, index=index)

    def remove_node(self, path: NodePath) -> None:
        """Remove a node, collapsing unary groups and allowing an empty root."""

        self._require_visual_editing()
        validated = _path(path)
        if self._root is None:
            raise QueryEditorError(_diagnostic("path_not_found", "当前查询树为空。"))
        candidate = _remove_at(self._root, validated)
        if candidate is None:
            self.replace_root(None)
        else:
            self._commit(_validate_visual_root(candidate))

    def move_node(self, path: NodePath, direction: int) -> None:
        """Move one child exactly one position inside the same AND/OR group."""

        self._require_visual_editing()
        validated = _path(path)
        if direction not in {-1, 1} or isinstance(direction, bool):
            raise QueryEditorError(
                _diagnostic(
                    "invalid_move_direction",
                    "条件移动方向只能是向上或向下。",
                )
            )
        if not validated:
            raise QueryEditorError(
                _diagnostic("root_cannot_move", "根条件不能上下移动。")
            )
        parent_path = validated[:-1]
        parent = _node_at(self._root, parent_path)
        if not isinstance(parent, (AndNode, OrNode)):
            raise QueryEditorError(
                _diagnostic(
                    "parent_not_group",
                    "只有同一 AND 或 OR 条件组内的条件可以移动。",
                )
            )
        index = validated[-1]
        target = index + direction
        if not 0 <= index < len(parent.operands):
            raise QueryEditorError(
                _diagnostic("path_not_found", "查询树路径不存在。")
            )
        if not 0 <= target < len(parent.operands):
            raise QueryEditorError(
                _diagnostic(
                    "move_out_of_range",
                    "条件已经位于当前方向的边界。",
                )
            )
        operands = list(parent.operands)
        operands[index], operands[target] = operands[target], operands[index]
        candidate = _replace_at(
            self._root,
            parent_path,
            _rebuild_group(parent, tuple(operands)),
        )
        self._commit(_validate_visual_root(candidate))

    def wrap_not(self, path: NodePath) -> None:
        self._require_visual_editing()
        validated = _path(path)
        selected = _node_at(self._root, validated)
        candidate = _replace_at(
            self._root,
            validated,
            NotNode(selected, _EMPTY_SPAN),
        )
        self._commit(_validate_visual_root(candidate))

    def unwrap_not(self, path: NodePath) -> None:
        self._require_visual_editing()
        validated = _path(path)
        selected = _node_at(self._root, validated)
        if not isinstance(selected, NotNode):
            raise QueryEditorError(
                _diagnostic("target_not_negated", "选中的条件没有 NOT。")
            )
        candidate = _replace_at(self._root, validated, selected.operand)
        self._commit(_validate_visual_root(candidate))

    def wrap_with_group(
        self,
        path: NodePath,
        operator: QueryGroupOperator,
        sibling: QueryNode | ParsedQuery,
        *,
        sibling_after: bool = True,
    ) -> None:
        """Create a two-child AND/OR group around one selected node."""

        self._require_visual_editing()
        if not isinstance(operator, QueryGroupOperator):
            raise QueryEditorError(
                _diagnostic("invalid_group_operator", "条件组类型无效。")
            )
        if not isinstance(sibling_after, bool):
            raise TypeError("sibling_after must be bool")
        validated = _path(path)
        selected = _node_at(self._root, validated)
        other = _root_of(sibling)
        operands = (selected, other) if sibling_after else (other, selected)
        grouped: QueryNode = (
            AndNode(operands, _EMPTY_SPAN)
            if operator == QueryGroupOperator.AND
            else OrNode(operands, _EMPTY_SPAN)
        )
        candidate = _replace_at(self._root, validated, grouped)
        self._commit(_validate_visual_root(candidate))

    def insert_artist_group(
        self,
        artists: Sequence[ConfirmedArtistTag],
        mode: ArtistCombinationMode,
        *,
        parent_path: NodePath | None = None,
        index: int | None = None,
    ) -> None:
        self.insert_subtree(
            parent_path,
            build_artist_subtree(artists, mode),
            index=index,
        )

    def _commit(self, parsed: ParsedQuery, *, increment: bool = True) -> None:
        self._root = parsed.root
        self._canonical_text = parsed.normalized_text
        self._draft_text = parsed.normalized_text
        self._text_diagnostic = None
        if increment:
            self._revision += 1

    def _require_visual_editing(self) -> None:
        if self._text_diagnostic is None:
            return
        raise QueryEditorError(
            QueryEditorDiagnostic(
                "invalid_text_pending",
                "请先放弃有错误的高级文本，再修改可视查询树。",
                self._text_diagnostic.span,
            )
        )


def build_artist_subtree(
    artists: Sequence[ConfirmedArtistTag],
    mode: ArtistCombinationMode,
) -> QueryNode:
    """Build an ordinary AST from one confirmed multi-artist shortcut."""

    if not isinstance(mode, ArtistCombinationMode):
        raise QueryEditorError(
            _diagnostic("invalid_artist_mode", "多画师条件模式无效。")
        )
    if not isinstance(artists, Sequence) or isinstance(artists, (str, bytes)):
        raise TypeError("artists must be a sequence of ConfirmedArtistTag values")
    names: list[str] = []
    seen: set[str] = set()
    for artist in artists:
        if not isinstance(artist, ConfirmedArtistTag):
            raise QueryEditorError(
                _diagnostic(
                    "artist_category_required",
                    "多画师条件只接受已确认分类为画师的标签。",
                )
            )
        if artist.name not in seen:
            names.append(artist.name)
            seen.add(artist.name)
    if not names:
        raise QueryEditorError(
            _diagnostic(
                "empty_artist_group",
                "多画师条件至少需要一个已确认的画师标签。",
            )
        )
    tags = tuple(TagNode(name, _EMPTY_SPAN) for name in names)
    if mode == ArtistCombinationMode.EXCLUDE:
        excluded: tuple[QueryNode, ...] = tuple(
            NotNode(tag, _EMPTY_SPAN) for tag in tags
        )
        return (
            excluded[0]
            if len(excluded) == 1
            else AndNode(excluded, _EMPTY_SPAN)
        )
    if len(tags) == 1:
        return tags[0]
    if mode == ArtistCombinationMode.ALL:
        return AndNode(tags, _EMPTY_SPAN)
    return OrNode(tags, _EMPTY_SPAN)


def confirmed_artist_tags(
    values: Iterable[tuple[str, int]],
) -> tuple[ConfirmedArtistTag, ...]:
    """Strictly convert suggestion/index ``(name, category)`` records."""

    if isinstance(values, (str, bytes)):
        raise TypeError("artist values must be an iterable of pairs")
    return tuple(ConfirmedArtistTag(name, category) for name, category in values)


def _parse_and_validate(source: str) -> ParsedQuery:
    parsed = parse_query_expression(source)
    analyze_query(parsed)
    build_standard_query_plan(parsed, post_counts={})
    return parsed


def _validate_visual_root(root: QueryNode) -> ParsedQuery:
    source = format_query_expression(root)
    try:
        return _parse_and_validate(source)
    except (QueryParseError, QueryPlanError) as exc:
        raise QueryEditorError(_from_query_error(exc, source)) from exc


def _from_query_error(
    error: QueryParseError | QueryPlanError,
    source: str,
) -> QueryEditorDiagnostic:
    span = error.span if isinstance(error, QueryParseError) else SourceSpan(0, len(source))
    return QueryEditorDiagnostic(error.code, str(error), span)


def _diagnostic(code: str, message: str) -> QueryEditorDiagnostic:
    return QueryEditorDiagnostic(code, message, _EMPTY_SPAN)


def _root_of(value: QueryNode | ParsedQuery) -> QueryNode:
    if isinstance(value, ParsedQuery):
        return value.root
    if isinstance(value, (TagNode, NotNode, AndNode, OrNode)):
        return value
    raise TypeError("query tree value must be a QueryNode or ParsedQuery")


def _path(value: NodePath) -> NodePath:
    if not isinstance(value, tuple) or any(
        not isinstance(index, int) or isinstance(index, bool) or index < 0
        for index in value
    ):
        raise QueryEditorError(
            _diagnostic(
                "invalid_tree_path",
                "查询树路径必须由非负整数构成。",
            )
        )
    return value


def _node_at(root: QueryNode | None, path: NodePath) -> QueryNode:
    if root is None:
        raise QueryEditorError(_diagnostic("path_not_found", "当前查询树为空。"))
    node = root
    for index in path:
        if isinstance(node, (AndNode, OrNode)) and index < len(node.operands):
            node = node.operands[index]
        elif isinstance(node, NotNode) and index == 0:
            node = node.operand
        else:
            raise QueryEditorError(
                _diagnostic("path_not_found", "查询树路径不存在。")
            )
    return node


def _replace_at(root: QueryNode | None, path: NodePath, value: QueryNode) -> QueryNode:
    if root is None:
        raise QueryEditorError(_diagnostic("path_not_found", "当前查询树为空。"))
    if not path:
        return value
    index = path[0]
    if isinstance(root, (AndNode, OrNode)) and index < len(root.operands):
        operands = list(root.operands)
        operands[index] = _replace_at(operands[index], path[1:], value)
        return _rebuild_group(root, tuple(operands))
    if isinstance(root, NotNode) and index == 0:
        return NotNode(_replace_at(root.operand, path[1:], value), _EMPTY_SPAN)
    raise QueryEditorError(_diagnostic("path_not_found", "查询树路径不存在。"))


def _remove_at(root: QueryNode, path: NodePath) -> QueryNode | None:
    if not path:
        return None
    index = path[0]
    if isinstance(root, (AndNode, OrNode)) and index < len(root.operands):
        operands = list(root.operands)
        if len(path) == 1:
            del operands[index]
        else:
            nested = _remove_at(operands[index], path[1:])
            if nested is None:
                del operands[index]
            else:
                operands[index] = nested
        if not operands:
            return None
        return _rebuild_group(root, tuple(operands))
    if isinstance(root, NotNode) and index == 0:
        if len(path) == 1:
            return None
        nested = _remove_at(root.operand, path[1:])
        return None if nested is None else NotNode(nested, _EMPTY_SPAN)
    raise QueryEditorError(_diagnostic("path_not_found", "查询树路径不存在。"))


def _insert_child(
    group: AndNode | OrNode,
    child: QueryNode,
    index: int | None,
) -> QueryNode:
    resolved = len(group.operands) if index is None else index
    if (
        not isinstance(resolved, int)
        or isinstance(resolved, bool)
        or not 0 <= resolved <= len(group.operands)
    ):
        raise QueryEditorError(
            _diagnostic(
                "invalid_insert_index",
                "条件插入位置超出当前条件组范围。",
            )
        )
    operands = list(group.operands)
    operands.insert(resolved, child)
    return _rebuild_group(group, tuple(operands))


def _rebuild_group(
    group: AndNode | OrNode,
    operands: tuple[QueryNode, ...],
) -> QueryNode:
    if len(operands) == 1:
        return operands[0]
    if isinstance(group, AndNode):
        return AndNode(operands, _EMPTY_SPAN)
    return OrNode(operands, _EMPTY_SPAN)
