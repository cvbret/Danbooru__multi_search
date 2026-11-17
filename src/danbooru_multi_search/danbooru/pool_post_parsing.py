"""Classify requested pool posts without weakening the existing post parser."""

from __future__ import annotations

import json

from danbooru_multi_search.core.pool_pages import POOL_WINDOW_SIZE, PoolPageState, PoolPostMetadata
from danbooru_multi_search.core.pools import pool_identifier
from danbooru_multi_search.danbooru.parsing import DanbooruParseError, parse_complete_posts_response
from danbooru_multi_search.danbooru.pool_parsing import PoolParseError, _decode


def parse_pool_posts_response(
    payload: bytes | str, requested_ids: tuple[int, ...],
) -> tuple[PoolPostMetadata, ...]:
    """Map a bounded ID response to requested order, retaining explicit gaps.

    A row with a known ID can fail independently. Ambiguous IDs invalidate the
    response, since assigning them to a page would invent a missing result.
    """
    if not isinstance(requested_ids, tuple) or not 1 <= len(requested_ids) <= POOL_WINDOW_SIZE:
        raise PoolParseError("作品编号批次必须包含 1—40 项。")
    for identifier in requested_ids:
        pool_identifier(identifier, "作品")
    if len(set(requested_ids)) != len(requested_ids):
        raise PoolParseError("请求作品编号必须先按首次出现顺序去重。")
    raw = _decode(payload)
    if not isinstance(raw, list) or len(raw) > len(requested_ids):
        raise PoolParseError("页面响应不是请求范围内的作品列表。")
    values: dict[int, PoolPostMetadata] = {}
    expected = set(requested_ids)
    for row in raw:
        if not isinstance(row, dict) or type(row.get("id")) is not int:
            raise PoolParseError("页面响应存在无法识别的作品编号。")
        identifier = row["id"]
        if identifier not in expected or identifier in values:
            raise PoolParseError("页面响应存在请求以外或重复的作品编号。")
        try:
            if type(row.get("is_deleted")) is not bool:
                raise DanbooruParseError("作品删除状态无效。")
            if row["is_deleted"]:
                value = PoolPostMetadata(identifier, PoolPageState.DELETED, message="作品已删除。")
            else:
                urls = tuple(row.get(name) for name in ("preview_file_url", "large_file_url", "file_url"))
                if any(url is not None and not isinstance(url, str) for url in urls):
                    raise DanbooruParseError("作品媒体地址无效。")
                if not any(url and url.strip() for url in urls):
                    value = PoolPostMetadata(identifier, PoolPageState.UNAVAILABLE, message="作品没有可访问的媒体地址。")
                else:
                    post = parse_complete_posts_response(json.dumps([row], allow_nan=False).encode("utf-8"))[0]
                    value = PoolPostMetadata(identifier, PoolPageState.AVAILABLE, post)
        except (DanbooruParseError, TypeError, ValueError) as exc:
            value = PoolPostMetadata(identifier, PoolPageState.FAILED, message=f"作品数据无效：{exc}")
        values[identifier] = value
    return tuple(values.get(identifier) or PoolPostMetadata(
        identifier, PoolPageState.NOT_RETURNED, message="接口未返回该作品，可能无法访问。",
    ) for identifier in requested_ids)
