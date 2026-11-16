"""Strict pool JSON parsing without requests, sorting or page compaction."""

from __future__ import annotations

import json

from danbooru_multi_search.core.pools import (
    MAX_POOL_LIST_LIMIT,
    DanbooruPool,
    PoolCategory,
    PoolError,
)

MAX_POOL_RESPONSE_BYTES = 8 * 1024 * 1024


class PoolParseError(PoolError):
    def __init__(self, message: str) -> None:
        super().__init__("invalid_response", message)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate field: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _decode(payload: bytes | str) -> object:
    try:
        if isinstance(payload, bytes):
            if len(payload) > MAX_POOL_RESPONSE_BYTES:
                raise ValueError("response is too large")
            text = payload.decode("utf-8")
        elif isinstance(payload, str):
            if len(payload.encode("utf-8")) > MAX_POOL_RESPONSE_BYTES:
                raise ValueError("response is too large")
            text = payload
        else:
            raise TypeError("pool response must be UTF-8 bytes or text")
        return json.loads(
            text, object_pairs_hook=_pairs, parse_constant=_invalid_constant
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise PoolParseError(f"作品池 JSON 无效：{exc}") from exc


def _pool(raw: object) -> DanbooruPool:
    try:
        if not isinstance(raw, dict):
            raise ValueError("pool must be an object")
        for field in ("id", "name", "category", "post_ids", "is_deleted"):
            if field not in raw:
                raise ValueError(f"missing field: {field}")
        if not isinstance(raw["post_ids"], list):
            raise ValueError("post_ids must be an array")
        value = DanbooruPool(
            raw["id"], raw["name"], PoolCategory(raw["category"]),
            tuple(raw["post_ids"]), raw.get("description", ""), raw["is_deleted"],
        )
        if "post_count" in raw and (
            type(raw["post_count"]) is not int
            or raw["post_count"] != value.post_count
        ):
            raise ValueError("post_count does not match post_ids")
        return value
    except (TypeError, ValueError, KeyError) as exc:
        raise PoolParseError(f"作品池数据无效：{exc}") from exc


def parse_pool_response(payload: bytes | str) -> DanbooruPool:
    """Parse one pool, retaining every original post ID position."""
    return _pool(_decode(payload))


def parse_pool_list_response(
    payload: bytes | str, *, limit: int = MAX_POOL_LIST_LIMIT,
) -> tuple[DanbooruPool, ...]:
    """Parse one bounded pool list; membership filtering belongs to its request."""
    if type(limit) is not int or not 1 <= limit <= MAX_POOL_LIST_LIMIT:
        raise PoolParseError("作品池列表解析上限无效。")
    raw = _decode(payload)
    if not isinstance(raw, list) or len(raw) > limit:
        raise PoolParseError("作品池响应必须是未超过请求上限的列表。")
    return tuple(_pool(item) for item in raw)
