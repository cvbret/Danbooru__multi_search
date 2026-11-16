from __future__ import annotations

import json

import pytest

from danbooru_multi_search.core.pools import PoolCategory
from danbooru_multi_search.danbooru.pool_parsing import (
    MAX_POOL_RESPONSE_BYTES, PoolParseError, parse_pool_list_response, parse_pool_response,
)


def pool_mapping(identifier=1, *, post_ids=(9, 2, 9), category="series", **extra):
    return dict(id=identifier, name="原始名称", category=category,
                description="说明", post_ids=list(post_ids), is_deleted=False, **extra)


@pytest.mark.parametrize("category", ["series", "collection"])
def test_pool_parser_retains_page_order_and_ignores_unrelated_api_fields(category):
    raw = pool_mapping(category=category, post_count=3, updated_at="API field")
    pool = parse_pool_response(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
    assert pool.category is PoolCategory(category)
    assert pool.post_ids == (9, 2, 9)
    assert pool.description == "说明" and pool.name == "原始名称"
    assert [page.post_id for page in pool.pages] == [9, 2, 9]


def test_pool_list_preserves_response_order_empty_and_deleted_metadata():
    first = pool_mapping(10, post_ids=())
    second = pool_mapping(2)
    second["is_deleted"] = True
    values = parse_pool_list_response(json.dumps([first, second]), limit=2)
    assert [pool.id for pool in values] == [10, 2]
    assert values[0].post_count == 0 and values[1].is_deleted
    assert parse_pool_list_response("[]", limit=1) == ()


@pytest.mark.parametrize("changes", [
    {"id": "1"}, {"id": True}, {"category": "unknown"}, {"post_ids": [0]},
    {"post_ids": [1.5]}, {"post_ids": None}, {"description": None},
    {"is_deleted": "false"}, {"post_count": 2}, {"post_count": True},
])
def test_pool_parser_rejects_invalid_fields(changes):
    raw = pool_mapping()
    raw.update(changes)
    with pytest.raises(PoolParseError):
        parse_pool_response(json.dumps(raw))


@pytest.mark.parametrize("payload", [
    b"\xff", b"{", b"null", b"[]", b'{"id":1,"id":2}', b'{"id":NaN}',
])
def test_pool_parser_rejects_bad_json(payload):
    with pytest.raises(PoolParseError):
        parse_pool_response(payload)


def test_parser_rejects_missing_fields_oversize_response_and_unbounded_list():
    raw = pool_mapping()
    del raw["post_ids"]
    with pytest.raises(PoolParseError):
        parse_pool_response(json.dumps(raw))
    with pytest.raises(PoolParseError):
        parse_pool_response(b" " * (MAX_POOL_RESPONSE_BYTES + 1))
    with pytest.raises(PoolParseError):
        parse_pool_list_response(json.dumps([pool_mapping(), pool_mapping(2)]), limit=1)
    with pytest.raises(PoolParseError):
        parse_pool_list_response("{}")
