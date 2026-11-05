"""Local boolean evaluation against complete Danbooru post tag groups."""

from __future__ import annotations

from collections.abc import Set as AbstractSet

from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.danbooru.query_language import (
    AndNode,
    NotNode,
    OrNode,
    ParsedQuery,
    QueryNode,
    TagNode,
)


def post_tag_set(post: DanbooruPost) -> frozenset[str]:
    """Combine the five category-specific tag groups without changing the post."""

    if not isinstance(post, DanbooruPost):
        raise TypeError("post must be a DanbooruPost")
    return frozenset(
        tag
        for group in (
            post.artist_tags,
            post.character_tags,
            post.copyright_tags,
            post.general_tags,
            post.meta_tags,
        )
        for tag in group
    )


def evaluate_query(
    expression: QueryNode | ParsedQuery,
    tags: AbstractSet[str],
) -> bool:
    """Evaluate a validated AST against normalized complete tag names."""

    if not isinstance(tags, AbstractSet) or any(
        not isinstance(tag, str) for tag in tags
    ):
        raise TypeError("tags must be an abstract set of strings")
    root = expression.root if isinstance(expression, ParsedQuery) else expression
    return _evaluate_node(root, tags)


def evaluate_post_query(
    expression: QueryNode | ParsedQuery,
    post: DanbooruPost,
) -> bool:
    """Evaluate a query using all five complete tag groups from one post."""

    return evaluate_query(expression, post_tag_set(post))


def _evaluate_node(node: QueryNode, tags: AbstractSet[str]) -> bool:
    if isinstance(node, TagNode):
        return node.name in tags
    if isinstance(node, NotNode):
        return not _evaluate_node(node.operand, tags)
    if isinstance(node, AndNode):
        return all(_evaluate_node(operand, tags) for operand in node.operands)
    if isinstance(node, OrNode):
        return any(_evaluate_node(operand, tags) for operand in node.operands)
    raise TypeError("expression must contain only supported query nodes")
