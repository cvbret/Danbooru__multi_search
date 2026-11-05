"""Deterministic, bounded query plans for ordinary Danbooru accounts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from danbooru_multi_search.danbooru.query_language import (
    AndNode,
    NotNode,
    OrNode,
    ParsedQuery,
    QueryNode,
    TagNode,
)
from danbooru_multi_search.danbooru.tag_syntax import (
    TagSyntaxError,
    normalize_tag_name,
)


MAX_QUERY_BRANCHES = 16
MAX_SEARCH_ITEMS = 2


@dataclass(frozen=True, slots=True)
class QueryPlanDiagnostic:
    code: str
    message: str


class QueryPlanError(ValueError):
    """Raised when an expression cannot form a safe executable plan."""

    def __init__(self, diagnostic: QueryPlanDiagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic

    @property
    def code(self) -> str:
        return self.diagnostic.code


@dataclass(frozen=True, slots=True)
class QueryBranch:
    """One exact DNF branch used for local evaluation after retrieval."""

    positive_tags: frozenset[str]
    excluded_tags: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.positive_tags, frozenset) or not isinstance(
            self.excluded_tags,
            frozenset,
        ):
            raise TypeError("QueryBranch tag collections must be frozensets.")
        for tag in self.positive_tags | self.excluded_tags:
            try:
                normalized = normalize_tag_name(tag)
            except TagSyntaxError as exc:
                raise ValueError(f"Invalid branch tag: {exc}") from exc
            if normalized != tag:
                raise ValueError("QueryBranch tags must already be normalized.")
        if self.positive_tags & self.excluded_tags:
            raise ValueError("QueryBranch cannot contain a contradictory tag.")


class QueryRating(StrEnum):
    ALL = "all"
    GENERAL = "general"
    SENSITIVE = "sensitive"
    QUESTIONABLE = "questionable"
    EXPLICIT = "explicit"


class RemoteOrder(StrEnum):
    RANK = "rank"


class SearchItemKind(StrEnum):
    TAG = "tag"
    RATING = "rating"
    ORDER = "order"


@dataclass(frozen=True, slots=True)
class SearchItem:
    """One ordered item used by both API tags and budget accounting."""

    kind: SearchItemKind
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SearchItemKind):
            raise TypeError("kind must be a SearchItemKind")
        if not isinstance(self.value, str) or not self.value:
            raise ValueError("Search item value must be non-empty text.")
        if self.kind == SearchItemKind.TAG:
            try:
                normalized = normalize_tag_name(self.value)
            except TagSyntaxError as exc:
                raise ValueError(f"Invalid search tag: {exc}") from exc
            if normalized != self.value:
                raise ValueError("Search tag values must already be normalized.")
        elif self.kind == SearchItemKind.RATING:
            if self.value not in {"rating:g", "rating:s", "rating:q", "rating:e"}:
                raise ValueError("Rating search items must use a fixed rating tag.")
        elif self.value != "order:rank":
            raise ValueError("Order search items must use order:rank.")

    @property
    def charged(self) -> bool:
        return self.kind != SearchItemKind.RATING


_RATING_TAGS = {
    QueryRating.GENERAL: "rating:g",
    QueryRating.SENSITIVE: "rating:s",
    QueryRating.QUESTIONABLE: "rating:q",
    QueryRating.EXPLICIT: "rating:e",
}


@dataclass(frozen=True, slots=True)
class RemoteQueryBranch:
    """One remote request template with locally retained branch semantics."""

    candidate_tags: tuple[str, ...]
    rating: QueryRating
    remote_order: RemoteOrder | None
    local_branch: QueryBranch | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_tags, tuple):
            raise TypeError("candidate_tags must be a tuple")
        for tag in self.candidate_tags:
            if normalize_tag_name(tag) != tag:
                raise ValueError("Candidate tags must already be normalized.")
        if len(set(self.candidate_tags)) != len(self.candidate_tags):
            raise ValueError("Candidate tags must be unique.")
        if not isinstance(self.rating, QueryRating):
            raise TypeError("rating must be a QueryRating")
        if self.remote_order is not None and not isinstance(
            self.remote_order,
            RemoteOrder,
        ):
            raise TypeError("remote_order must be RemoteOrder or None")
        if self.local_branch is not None:
            if not isinstance(self.local_branch, QueryBranch):
                raise TypeError("local_branch must be QueryBranch or None")
            if not set(self.candidate_tags) <= self.local_branch.positive_tags:
                raise ValueError("Candidates must come from the positive branch tags.")
        validate_search_item_budget(self.search_items)

    @property
    def search_items(self) -> tuple[SearchItem, ...]:
        items = [
            SearchItem(SearchItemKind.TAG, tag) for tag in self.candidate_tags
        ]
        rating_tag = _RATING_TAGS.get(self.rating)
        if rating_tag is not None:
            items.append(SearchItem(SearchItemKind.RATING, rating_tag))
        if self.remote_order == RemoteOrder.RANK:
            items.append(SearchItem(SearchItemKind.ORDER, "order:rank"))
        return tuple(items)

    @property
    def charged_items(self) -> tuple[SearchItem, ...]:
        return tuple(item for item in self.search_items if item.charged)

    @property
    def api_tags(self) -> str:
        return " ".join(item.value for item in self.search_items)


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """A complete local expression plus deterministic remote scan branches."""

    expression: QueryNode | None
    dnf_branches: tuple[QueryBranch, ...]
    remote_branches: tuple[RemoteQueryBranch, ...]
    rating: QueryRating
    remote_order: RemoteOrder | None

    def __post_init__(self) -> None:
        if self.expression is not None and not isinstance(
            self.expression,
            (TagNode, NotNode, AndNode, OrNode),
        ):
            raise TypeError("expression must be a query node or None")
        if not isinstance(self.dnf_branches, tuple) or not all(
            isinstance(branch, QueryBranch) for branch in self.dnf_branches
        ):
            raise TypeError("dnf_branches must be a tuple of QueryBranch values")
        if not isinstance(self.remote_branches, tuple) or not all(
            isinstance(branch, RemoteQueryBranch)
            for branch in self.remote_branches
        ):
            raise TypeError(
                "remote_branches must be a tuple of RemoteQueryBranch values"
            )
        if not isinstance(self.rating, QueryRating):
            raise TypeError("rating must be a QueryRating")
        if self.remote_order is not None and not isinstance(
            self.remote_order,
            RemoteOrder,
        ):
            raise TypeError("remote_order must be RemoteOrder or None")

    @property
    def is_rank(self) -> bool:
        return self.remote_order == RemoteOrder.RANK

    @property
    def is_latest(self) -> bool:
        if (
            self.expression is not None
            or self.dnf_branches
            or self.remote_order is not None
            or len(self.remote_branches) != 1
        ):
            return False
        branch = self.remote_branches[0]
        return (
            not branch.candidate_tags
            and branch.rating == self.rating
            and branch.remote_order is None
            and branch.local_branch is None
        )


def expand_query_to_dnf(
    expression: QueryNode | ParsedQuery,
) -> tuple[QueryBranch, ...]:
    """Expand an AST into bounded, stable, exact DNF branches."""

    root = _query_root(expression)
    branches = _expand_node(root, negated=False)
    if not branches:
        _fail(
            "all_branches_contradictory",
            "查询表达式的所有分支都互相矛盾。",
        )
    if any(not branch.positive_tags for branch in branches):
        _fail(
            "pure_negative_branch",
            "每个远程查询分支都必须包含至少一个正向标签。",
        )
    return branches


def select_candidate_tags(
    positive_tags: Iterable[str],
    post_counts: Mapping[str, int | None] | None,
    *,
    limit: int = MAX_SEARCH_ITEMS,
) -> tuple[str, ...]:
    """Select deterministic candidates using exact normalized count keys."""

    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_SEARCH_ITEMS
    ):
        raise ValueError("limit must be one or two")
    normalized: set[str] = set()
    for tag in positive_tags:
        try:
            normalized.add(normalize_tag_name(tag))
        except (TagSyntaxError, TypeError) as exc:
            _fail("invalid_post_count", f"候选标签无效：{exc}")
    counts = _validated_post_counts(frozenset(normalized), post_counts)
    return _select_from_counts(frozenset(normalized), counts, limit)


def normalize_plan_rating(value: str) -> QueryRating:
    """Normalize the aliases already accepted by the M1 rating control."""

    if not isinstance(value, str):
        _fail("invalid_rating", "内容分级必须是文本。")
    if value and not value.isascii():
        _fail("invalid_rating", "内容分级只接受固定的 ASCII 文本。")
    aliases = {
        "": QueryRating.ALL,
        "all": QueryRating.ALL,
        "g": QueryRating.GENERAL,
        "general": QueryRating.GENERAL,
        "s": QueryRating.SENSITIVE,
        "sensitive": QueryRating.SENSITIVE,
        "q": QueryRating.QUESTIONABLE,
        "questionable": QueryRating.QUESTIONABLE,
        "e": QueryRating.EXPLICIT,
        "explicit": QueryRating.EXPLICIT,
    }
    normalized = aliases.get(value.strip().lower())
    if normalized is None:
        _fail(
            "invalid_rating",
            "内容分级必须是全部、General、Sensitive、Questionable 或 Explicit。",
        )
    return normalized


def normalize_remote_order(value: str | None) -> RemoteOrder | None:
    """Accept no remote sort or the one canonical rank order."""

    if value is None or value == "":
        return None
    if value == RemoteOrder.RANK.value:
        return RemoteOrder.RANK
    _fail(
        "invalid_remote_order",
        "远程排序只允许规范值 rank。",
    )


def validate_search_item_budget(
    items: Sequence[SearchItem],
) -> tuple[SearchItem, ...]:
    """Return charged items or reject a request exceeding the fixed budget."""

    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise TypeError("items must be a sequence of SearchItem values")
    if any(not isinstance(item, SearchItem) for item in items):
        raise TypeError("items must contain only SearchItem values")
    charged = tuple(item for item in items if item.charged)
    if len(charged) > MAX_SEARCH_ITEMS:
        _fail(
            "search_item_limit",
            f"普通账号每个远程请求最多允许 {MAX_SEARCH_ITEMS} 个计费搜索项。",
        )
    return charged


def build_query_plan(
    expression: QueryNode | ParsedQuery | None,
    *,
    post_counts: Mapping[str, int | None] | None = None,
    rating: str = "all",
    remote_order: str | None = None,
) -> QueryPlan:
    """Build an ordinary local-sort plan or one single-stream rank plan."""

    root: QueryNode | None
    if expression is None:
        root = None
        dnf_branches: tuple[QueryBranch, ...] = ()
    else:
        root = _query_root(expression)
        dnf_branches = expand_query_to_dnf(root)

    relevant_tags = frozenset(
        tag
        for branch in dnf_branches
        for tag in branch.positive_tags
    )
    counts = _validated_post_counts(relevant_tags, post_counts)
    normalized_rating = normalize_plan_rating(rating)
    normalized_order = normalize_remote_order(remote_order)

    if root is None and normalized_order != RemoteOrder.RANK:
        _fail(
            "missing_expression",
            "普通查询计划必须包含查询表达式。",
        )

    if normalized_order == RemoteOrder.RANK:
        remote_branches = (
            _build_rank_branch(
                dnf_branches,
                counts,
                normalized_rating,
                has_expression=root is not None,
            ),
        )
    else:
        remote_branches = tuple(
            RemoteQueryBranch(
                candidate_tags=_select_from_counts(
                    branch.positive_tags,
                    counts,
                    MAX_SEARCH_ITEMS,
                ),
                rating=normalized_rating,
                remote_order=None,
                local_branch=branch,
            )
            for branch in dnf_branches
        )

    # Budget validation is intentionally the final mandatory construction step.
    for branch in remote_branches:
        validate_search_item_budget(branch.search_items)

    return QueryPlan(
        expression=root,
        dnf_branches=dnf_branches,
        remote_branches=remote_branches,
        rating=normalized_rating,
        remote_order=normalized_order,
    )


def build_standard_query_plan(
    expression: QueryNode | ParsedQuery,
    *,
    post_counts: Mapping[str, int | None] | None = None,
    rating: str = "all",
) -> QueryPlan:
    return build_query_plan(
        expression,
        post_counts=post_counts,
        rating=rating,
        remote_order=None,
    )


def build_rank_query_plan(
    expression: QueryNode | ParsedQuery | None,
    *,
    post_counts: Mapping[str, int | None] | None = None,
    rating: str = "all",
) -> QueryPlan:
    return build_query_plan(
        expression,
        post_counts=post_counts,
        rating=rating,
        remote_order=RemoteOrder.RANK.value,
    )


def build_latest_query_plan(*, rating: str = "all") -> QueryPlan:
    """Build the sole untagged plan using Danbooru's default newest order."""

    normalized_rating = normalize_plan_rating(rating)
    return QueryPlan(
        expression=None,
        dnf_branches=(),
        remote_branches=(
            RemoteQueryBranch(
                candidate_tags=(),
                rating=normalized_rating,
                remote_order=None,
                local_branch=None,
            ),
        ),
        rating=normalized_rating,
        remote_order=None,
    )


def _expand_node(node: QueryNode, *, negated: bool) -> tuple[QueryBranch, ...]:
    if isinstance(node, TagNode):
        return (
            QueryBranch(
                positive_tags=(frozenset() if negated else frozenset({node.name})),
                excluded_tags=(frozenset({node.name}) if negated else frozenset()),
            ),
        )
    if isinstance(node, NotNode):
        return _expand_node(node.operand, negated=not negated)

    effective_or = isinstance(node, OrNode) != negated
    if effective_or:
        combined: list[QueryBranch] = []
        seen: set[QueryBranch] = set()
        for operand in node.operands:
            for branch in _expand_node(operand, negated=negated):
                _append_unique_branch(combined, seen, branch)
        return tuple(combined)

    accumulated = [QueryBranch(frozenset(), frozenset())]
    for operand in node.operands:
        if not accumulated:
            break
        incoming = _expand_node(operand, negated=negated)
        merged: list[QueryBranch] = []
        seen: set[QueryBranch] = set()
        for outer in accumulated:
            for inner in incoming:
                branch = _merge_branches(outer, inner)
                if branch is not None:
                    _append_unique_branch(merged, seen, branch)
        accumulated = merged
    return tuple(accumulated)


def _merge_branches(
    left: QueryBranch,
    right: QueryBranch,
) -> QueryBranch | None:
    positive = left.positive_tags | right.positive_tags
    excluded = left.excluded_tags | right.excluded_tags
    if positive & excluded:
        return None
    return QueryBranch(positive, excluded)


def _append_unique_branch(
    branches: list[QueryBranch],
    seen: set[QueryBranch],
    branch: QueryBranch,
) -> None:
    if branch in seen:
        return
    seen.add(branch)
    branches.append(branch)
    if len(branches) > MAX_QUERY_BRANCHES:
        _fail(
            "branch_limit",
            f"查询最多允许 {MAX_QUERY_BRANCHES} 个有效远程分支。",
        )


def _validated_post_counts(
    tags: frozenset[str],
    post_counts: Mapping[str, int | None] | None,
) -> dict[str, int | None]:
    if post_counts is None:
        source: Mapping[str, int | None] = {}
    elif isinstance(post_counts, Mapping):
        source = post_counts
    else:
        _fail("invalid_post_count", "post_count 数据必须是只读映射。")

    validated: dict[str, int | None] = {}
    for tag in sorted(tags):
        value = source[tag] if tag in source else None
        if value is not None and (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            _fail(
                "invalid_post_count",
                f"标签 {tag} 的 post_count 必须是非负整数、None 或缺失。",
            )
        validated[tag] = value
    return validated


def _select_from_counts(
    tags: frozenset[str],
    counts: Mapping[str, int | None],
    limit: int,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            tags,
            key=lambda tag: (
                counts.get(tag) is None,
                counts.get(tag) if counts.get(tag) is not None else 0,
                tag,
            ),
        )[:limit]
    )


def _build_rank_branch(
    dnf_branches: tuple[QueryBranch, ...],
    counts: Mapping[str, int | None],
    rating: QueryRating,
    *,
    has_expression: bool,
) -> RemoteQueryBranch:
    if not has_expression:
        candidates: tuple[str, ...] = ()
    else:
        common = set(dnf_branches[0].positive_tags)
        for branch in dnf_branches[1:]:
            common.intersection_update(branch.positive_tags)
        if not common:
            _fail(
                "rank_requires_common_tag",
                "复杂近期热门查询必须有所有分支共同包含的正向标签。",
            )
        candidates = _select_from_counts(frozenset(common), counts, 1)
    return RemoteQueryBranch(
        candidate_tags=candidates,
        rating=rating,
        remote_order=RemoteOrder.RANK,
        local_branch=None,
    )


def _query_root(expression: QueryNode | ParsedQuery) -> QueryNode:
    if isinstance(expression, ParsedQuery):
        return expression.root
    if isinstance(expression, (TagNode, NotNode, AndNode, OrNode)):
        return expression
    raise TypeError("expression must be a query node or ParsedQuery")


def _fail(code: str, message: str) -> None:
    raise QueryPlanError(QueryPlanDiagnostic(code, message))
