"""Pure boolean query language for bounded Danbooru searches."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias

from danbooru_multi_search.danbooru.tag_syntax import (
    QUERY_OPERATOR_NAMES,
    TagSyntaxError,
    normalize_tag_name,
)


MAX_QUERY_CHARACTERS = 2_048
MAX_QUERY_NODES = 128
MAX_QUERY_DEPTH = 16


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """Zero-based, half-open range in the original expression."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.start, int)
            or isinstance(self.start, bool)
            or not isinstance(self.end, int)
            or isinstance(self.end, bool)
            or self.start < 0
            or self.end < self.start
        ):
            raise ValueError("SourceSpan requires 0 <= start <= end integers.")


@dataclass(frozen=True, slots=True)
class QueryDiagnostic:
    """Stable parse failure suitable for a text editor error marker."""

    code: str
    message: str
    span: SourceSpan


class QueryParseError(ValueError):
    """Raised when a query expression cannot produce a valid bounded AST."""

    def __init__(self, diagnostic: QueryDiagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic

    @property
    def code(self) -> str:
        return self.diagnostic.code

    @property
    def span(self) -> SourceSpan:
        return self.diagnostic.span


@dataclass(frozen=True, slots=True)
class TagNode:
    name: str
    span: SourceSpan = field(compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", normalize_tag_name(self.name))


@dataclass(frozen=True, slots=True)
class NotNode:
    operand: QueryNode
    span: SourceSpan = field(compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.operand, (TagNode, NotNode, AndNode, OrNode)):
            raise TypeError("NotNode operand must be a query node.")


@dataclass(frozen=True, slots=True)
class AndNode:
    operands: tuple[QueryNode, ...]
    span: SourceSpan = field(compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.operands, tuple) or any(
            not isinstance(operand, (TagNode, NotNode, AndNode, OrNode))
            for operand in self.operands
        ):
            raise TypeError("AndNode operands must be a tuple of query nodes.")
        flattened = tuple(
            nested
            for operand in self.operands
            for nested in (
                operand.operands if isinstance(operand, AndNode) else (operand,)
            )
        )
        if len(flattened) < 2:
            raise ValueError("AndNode requires at least two operands.")
        object.__setattr__(self, "operands", flattened)


@dataclass(frozen=True, slots=True)
class OrNode:
    operands: tuple[QueryNode, ...]
    span: SourceSpan = field(compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.operands, tuple) or any(
            not isinstance(operand, (TagNode, NotNode, AndNode, OrNode))
            for operand in self.operands
        ):
            raise TypeError("OrNode operands must be a tuple of query nodes.")
        flattened = tuple(
            nested
            for operand in self.operands
            for nested in (
                operand.operands if isinstance(operand, OrNode) else (operand,)
            )
        )
        if len(flattened) < 2:
            raise ValueError("OrNode requires at least two operands.")
        object.__setattr__(self, "operands", flattened)


QueryNode: TypeAlias = TagNode | NotNode | AndNode | OrNode


@dataclass(frozen=True, slots=True)
class QueryAnalysis:
    """Tag occurrences classified by their effective NOT polarity."""

    positive_tags: tuple[str, ...]
    negative_tags: tuple[str, ...]

    @property
    def has_positive_tag(self) -> bool:
        return bool(self.positive_tags)


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    """A validated expression and its canonical text representation."""

    root: QueryNode
    source: str = field(compare=False)
    normalized_text: str = field(compare=False)
    span: SourceSpan = field(compare=False)


class _TokenKind(StrEnum):
    TAG = "tag"
    NOT = "not"
    AND = "and"
    OR = "or"
    LEFT_PARENTHESIS = "left_parenthesis"
    RIGHT_PARENTHESIS = "right_parenthesis"


@dataclass(frozen=True, slots=True)
class _Token:
    kind: _TokenKind
    text: str
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class _ParsedValue:
    node: QueryNode
    span: SourceSpan


_OPERATOR_KINDS = {
    "not": _TokenKind.NOT,
    "and": _TokenKind.AND,
    "or": _TokenKind.OR,
}
_OPERAND_STARTS = frozenset(
    {_TokenKind.TAG, _TokenKind.NOT, _TokenKind.LEFT_PARENTHESIS}
)


def parse_query_expression(source: str) -> ParsedQuery:
    """Parse one bounded expression or raise ``QueryParseError``."""

    if not isinstance(source, str):
        _fail(
            "expression_not_text",
            "查询表达式必须是文本。",
            0,
            0,
        )
    if len(source) > MAX_QUERY_CHARACTERS:
        _fail(
            "expression_too_long",
            f"查询表达式最多允许 {MAX_QUERY_CHARACTERS} 个字符。",
            MAX_QUERY_CHARACTERS,
            len(source),
        )

    tokens = _tokenize(source)
    if not tokens:
        _fail(
            "empty_expression",
            "查询表达式不能为空。",
            0,
            len(source),
        )
    _validate_syntax(tokens, len(source))
    _validate_nesting_depth(tokens)

    root = _Parser(tokens).parse()
    expression_span = SourceSpan(tokens[0].span.start, tokens[-1].span.end)

    analysis = analyze_query(root)
    if not analysis.has_positive_tag:
        _fail(
            "pure_negative_expression",
            "查询表达式至少需要一个不受 NOT 排除的正向标签。",
            expression_span.start,
            expression_span.end,
        )
    return ParsedQuery(
        root=root,
        source=source,
        normalized_text=format_query_expression(root),
        span=expression_span,
    )


def format_query_expression(expression: QueryNode | ParsedQuery) -> str:
    """Return canonical text while preserving operand order and structure."""

    root = _root(expression)
    return _format_node(root, parent_precedence=0)


def analyze_query(expression: QueryNode | ParsedQuery) -> QueryAnalysis:
    """Classify every tag occurrence using the effective NOT polarity."""

    positive: list[str] = []
    negative: list[str] = []

    def visit(node: QueryNode, is_positive: bool) -> None:
        if isinstance(node, TagNode):
            (positive if is_positive else negative).append(node.name)
        elif isinstance(node, NotNode):
            visit(node.operand, not is_positive)
        else:
            for operand in node.operands:
                visit(operand, is_positive)

    visit(_root(expression), True)
    return QueryAnalysis(tuple(positive), tuple(negative))


def _tokenize(source: str) -> tuple[_Token, ...]:
    for index, character in enumerate(source):
        if ord(character) > 127:
            _fail(
                "non_ascii_character",
                "查询表达式只允许 ASCII 字符。",
                index,
                index + 1,
            )

    tokens: list[_Token] = []
    index = 0
    while index < len(source):
        character = source[index]
        if character.isspace():
            index += 1
            continue
        if character == "(":
            tokens.append(
                _Token(
                    _TokenKind.LEFT_PARENTHESIS,
                    character,
                    SourceSpan(index, index + 1),
                )
            )
            index += 1
            continue
        if character == ")":
            tokens.append(
                _Token(
                    _TokenKind.RIGHT_PARENTHESIS,
                    character,
                    SourceSpan(index, index + 1),
                )
            )
            index += 1
            continue

        prefix_end = index
        while (
            prefix_end < len(source)
            and not source[prefix_end].isspace()
            and source[prefix_end] not in "()"
        ):
            prefix_end += 1
        prefix = source[index:prefix_end]
        normalized_prefix = prefix.casefold()
        if normalized_prefix in QUERY_OPERATOR_NAMES:
            tokens.append(
                _Token(
                    _OPERATOR_KINDS[normalized_prefix],
                    prefix,
                    SourceSpan(index, prefix_end),
                )
            )
            index = prefix_end
            continue

        end = _scan_tag_end(source, index)
        raw_tag = source[index:end]
        try:
            normalized_tag = normalize_tag_name(raw_tag)
        except TagSyntaxError as exc:
            code = (
                "metatag_not_allowed"
                if exc.code == "metatag_not_allowed"
                else "invalid_tag"
            )
            _fail(code, str(exc), index, end)
        tokens.append(
            _Token(
                _TokenKind.TAG,
                normalized_tag,
                SourceSpan(index, end),
            )
        )
        index = end
    return tuple(tokens)


def _scan_tag_end(source: str, start: int) -> int:
    index = start
    internal_depth = 0
    while index < len(source):
        character = source[index]
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
    return index


def _validate_syntax(tokens: tuple[_Token, ...], source_length: int) -> None:
    expect_operand = True
    open_groups: list[_Token] = []
    previous: _Token | None = None

    for token in tokens:
        if expect_operand:
            if token.kind == _TokenKind.TAG:
                expect_operand = False
            elif token.kind == _TokenKind.NOT:
                pass
            elif token.kind == _TokenKind.LEFT_PARENTHESIS:
                open_groups.append(token)
            elif token.kind == _TokenKind.RIGHT_PARENTHESIS:
                if (
                    previous is not None
                    and previous.kind == _TokenKind.LEFT_PARENTHESIS
                ):
                    _fail(
                        "empty_group",
                        "查询条件组不能为空。",
                        previous.span.start,
                        token.span.end,
                    )
                if not open_groups:
                    _fail(
                        "unexpected_closing_parenthesis",
                        "查询表达式包含没有对应左括号的右括号。",
                        token.span.start,
                        token.span.end,
                    )
                _fail(
                    "missing_operand",
                    "查询运算符或左括号后缺少条件。",
                    token.span.start,
                    token.span.end,
                )
            else:
                _fail(
                    "unexpected_operator",
                    "查询运算符前缺少条件。",
                    token.span.start,
                    token.span.end,
                )
        else:
            if token.kind in {_TokenKind.AND, _TokenKind.OR}:
                expect_operand = True
            elif token.kind == _TokenKind.RIGHT_PARENTHESIS:
                if not open_groups:
                    _fail(
                        "unexpected_closing_parenthesis",
                        "查询表达式包含没有对应左括号的右括号。",
                        token.span.start,
                        token.span.end,
                    )
                open_groups.pop()
            elif token.kind == _TokenKind.NOT:
                expect_operand = True
            elif token.kind == _TokenKind.LEFT_PARENTHESIS:
                open_groups.append(token)
                expect_operand = True
            elif token.kind == _TokenKind.TAG:
                pass
        previous = token

    if expect_operand:
        _fail(
            "missing_operand",
            "查询表达式末尾缺少条件。",
            source_length,
            source_length,
        )
    if open_groups:
        opening = open_groups[-1]
        _fail(
            "unclosed_group",
            "查询表达式包含没有闭合的左括号。",
            opening.span.start,
            opening.span.end,
        )


def _validate_nesting_depth(tokens: tuple[_Token, ...]) -> None:
    base_depth = 0
    pending_not_depth = 0
    group_bases: list[int] = []

    for token in tokens:
        if token.kind == _TokenKind.NOT:
            pending_not_depth += 1
            depth = base_depth + pending_not_depth
        elif token.kind == _TokenKind.LEFT_PARENTHESIS:
            group_bases.append(base_depth)
            base_depth = base_depth + pending_not_depth + 1
            pending_not_depth = 0
            depth = base_depth
        elif token.kind == _TokenKind.TAG:
            depth = base_depth + pending_not_depth
            pending_not_depth = 0
        elif token.kind == _TokenKind.RIGHT_PARENTHESIS:
            base_depth = group_bases.pop()
            pending_not_depth = 0
            continue
        else:
            pending_not_depth = 0
            continue
        if depth > MAX_QUERY_DEPTH:
            _fail(
                "nesting_limit_exceeded",
                f"查询表达式最多允许 {MAX_QUERY_DEPTH} 层嵌套。",
                token.span.start,
                token.span.end,
            )


class _Parser:
    def __init__(self, tokens: tuple[_Token, ...]) -> None:
        self._tokens = tokens
        self._index = 0

    def parse(self) -> QueryNode:
        return self._parse_or().node

    def _parse_or(self) -> _ParsedValue:
        operands = [self._parse_and()]
        while self._at(_TokenKind.OR):
            self._index += 1
            operands.append(self._parse_and())
        return _combine_or(operands)

    def _parse_and(self) -> _ParsedValue:
        operands = [self._parse_not()]
        while self._index < len(self._tokens):
            token = self._tokens[self._index]
            if token.kind == _TokenKind.AND:
                self._index += 1
                operands.append(self._parse_not())
            elif token.kind in _OPERAND_STARTS:
                operands.append(self._parse_not())
            else:
                break
        return _combine_and(operands)

    def _parse_not(self) -> _ParsedValue:
        operators: list[_Token] = []
        while self._at(_TokenKind.NOT):
            operators.append(self._tokens[self._index])
            self._index += 1
        parsed = self._parse_primary()
        operand = parsed.node
        span = parsed.span
        for operator in reversed(operators):
            _ensure_node_limit(
                1 + _node_count(operand),
                operator.span.start,
                span.end,
            )
            span = SourceSpan(operator.span.start, span.end)
            operand = NotNode(
                operand=operand,
                span=span,
            )
        return _ParsedValue(operand, span)

    def _parse_primary(self) -> _ParsedValue:
        token = self._tokens[self._index]
        if token.kind == _TokenKind.TAG:
            self._index += 1
            return _ParsedValue(TagNode(token.text, token.span), token.span)
        opening = token
        self._index += 1
        expression = self._parse_or()
        closing = self._tokens[self._index]
        self._index += 1
        return _ParsedValue(
            expression.node,
            SourceSpan(opening.span.start, closing.span.end),
        )

    def _at(self, kind: _TokenKind) -> bool:
        return (
            self._index < len(self._tokens)
            and self._tokens[self._index].kind == kind
        )


def _combine_and(operands: list[_ParsedValue]) -> _ParsedValue:
    if len(operands) == 1:
        return operands[0]
    flattened: list[QueryNode] = []
    for parsed in operands:
        if isinstance(parsed.node, AndNode):
            flattened.extend(parsed.node.operands)
        else:
            flattened.append(parsed.node)
    start = operands[0].span.start
    end = operands[-1].span.end
    _ensure_node_limit(
        1 + sum(_node_count(operand) for operand in flattened),
        start,
        end,
    )
    span = SourceSpan(start, end)
    return _ParsedValue(
        AndNode(tuple(flattened), span),
        span,
    )


def _combine_or(operands: list[_ParsedValue]) -> _ParsedValue:
    if len(operands) == 1:
        return operands[0]
    flattened: list[QueryNode] = []
    for parsed in operands:
        if isinstance(parsed.node, OrNode):
            flattened.extend(parsed.node.operands)
        else:
            flattened.append(parsed.node)
    start = operands[0].span.start
    end = operands[-1].span.end
    _ensure_node_limit(
        1 + sum(_node_count(operand) for operand in flattened),
        start,
        end,
    )
    span = SourceSpan(start, end)
    return _ParsedValue(
        OrNode(tuple(flattened), span),
        span,
    )


def _node_count(root: QueryNode) -> int:
    if isinstance(root, TagNode):
        return 1
    if isinstance(root, NotNode):
        return 1 + _node_count(root.operand)
    return 1 + sum(_node_count(operand) for operand in root.operands)


def _ensure_node_limit(count: int, start: int, end: int) -> None:
    if count > MAX_QUERY_NODES:
        _fail(
            "node_limit_exceeded",
            f"查询表达式最多允许 {MAX_QUERY_NODES} 个语法树节点。",
            start,
            end,
        )


def _format_node(node: QueryNode, parent_precedence: int) -> str:
    if isinstance(node, TagNode):
        text = node.name
        precedence = 4
    elif isinstance(node, NotNode):
        text = f"NOT {_format_node(node.operand, 3)}"
        precedence = 3
    elif isinstance(node, AndNode):
        text = " AND ".join(
            _format_node(operand, 2) for operand in node.operands
        )
        precedence = 2
    else:
        text = " OR ".join(
            _format_node(operand, 1) for operand in node.operands
        )
        precedence = 1
    return f"({text})" if precedence < parent_precedence else text


def _root(expression: QueryNode | ParsedQuery) -> QueryNode:
    if isinstance(expression, ParsedQuery):
        return expression.root
    if isinstance(expression, (TagNode, NotNode, AndNode, OrNode)):
        return expression
    raise TypeError("expression must be a query node or ParsedQuery")


def _fail(code: str, message: str, start: int, end: int) -> None:
    raise QueryParseError(
        QueryDiagnostic(code, message, SourceSpan(start, end))
    )
