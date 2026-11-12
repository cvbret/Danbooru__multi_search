"""Canonical overview JSON and UTC text at the favorite storage boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from danbooru_multi_search.core.favorites import (
    FavoriteCandidate,
    FavoriteError,
    FavoriteIdentity,
    FavoriteObjectType,
    FavoritePoolCategory,
    FavoriteTagCategory,
    _validate_candidate,
    _validate_display_name,
    favorite_timestamp_utc,
)
from danbooru_multi_search.storage.post_snapshot import (
    PostSnapshotError,
    decode_post_snapshot,
    encode_post_snapshot,
)


FAVORITE_SUMMARY_VERSION = 1


def encode_favorite_summary(candidate: FavoriteCandidate) -> str:
    """Return the canonical persisted overview for one favorite candidate."""

    return _encode_candidate(_validate_candidate(candidate))[0]


def decode_favorite_summary(
    identity: FavoriteIdentity,
    display_name: str,
    encoded: str,
) -> FavoriteCandidate:
    """Decode an overview while checking its redundant identity and display data."""

    if not isinstance(identity, FavoriteIdentity):
        raise FavoriteError("invalid_identity", "收藏身份无效。")
    _validate_display_name(display_name)
    if identity.object_type is FavoriteObjectType.POST:
        try:
            post = decode_post_snapshot(encoded)
        except (PostSnapshotError, TypeError, ValueError) as exc:
            raise FavoriteError("invalid_summary", f"作品概要损坏：{exc}") from exc
        candidate = FavoriteCandidate(identity, display_name, post=post)
    elif identity.object_type is FavoriteObjectType.TAG:
        payload = _load_canonical(encoded, "标签概要")
        _expect_exact_fields(
            payload,
            {"display_name", "object_key", "object_type", "tag_category", "version"},
            "标签概要",
        )
        try:
            category = FavoriteTagCategory(payload["tag_category"])
        except (TypeError, ValueError) as exc:
            raise FavoriteError("invalid_summary", "标签概要分类无效。") from exc
        candidate = FavoriteCandidate(identity, display_name, tag_category=category)
    else:
        payload = _load_canonical(encoded, "作品池概要")
        _expect_exact_fields(
            payload,
            {"display_name", "object_key", "object_type", "pool_category", "version"},
            "作品池概要",
        )
        if payload["pool_category"] != FavoritePoolCategory.SERIES.value:
            raise FavoriteError("invalid_summary", "作品池概要分类无效。")
        candidate = FavoriteCandidate(
            identity,
            display_name,
            pool_category=FavoritePoolCategory.SERIES,
        )
    if encode_favorite_summary(candidate) != encoded:
        raise FavoriteError("summary_mismatch", "收藏概要与身份或显示名称不一致。")
    return candidate


def encode_favorite_timestamp(value: datetime) -> str:
    """Encode a timezone-aware time using the favorite storage format."""

    return _canonical_timestamp(value)


def decode_favorite_timestamp(value: str) -> datetime:
    """Decode the sole canonical favorite storage timestamp."""

    return _parse_timestamp(value)


def _encode_candidate(
    candidate: FavoriteCandidate,
) -> tuple[str, int | None, int | None, str | None]:
    if candidate.post is not None:
        try:
            encoded = encode_post_snapshot(candidate.post)
        except (PostSnapshotError, TypeError, ValueError) as exc:
            raise FavoriteError("invalid_summary", f"作品概要无效：{exc}") from exc
        return encoded, candidate.post.score, candidate.post.fav_count, candidate.post.rating
    if candidate.identity.object_type is FavoriteObjectType.TAG:
        payload: Mapping[str, Any] = {
            "display_name": candidate.display_name,
            "object_key": candidate.identity.object_key,
            "object_type": FavoriteObjectType.TAG.value,
            "tag_category": candidate.tag_category.value,
            "version": FAVORITE_SUMMARY_VERSION,
        }
    else:
        payload = {
            "display_name": candidate.display_name,
            "object_key": candidate.identity.object_key,
            "object_type": FavoriteObjectType.POOL.value,
            "pool_category": FavoritePoolCategory.SERIES.value,
            "version": FAVORITE_SUMMARY_VERSION,
        }
    return _dump_canonical(payload), None, None, None


def _canonical_timestamp(value: datetime) -> str:
    return favorite_timestamp_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise FavoriteError("corrupt_timestamp", "收藏时间不是规范文本。")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise FavoriteError("corrupt_timestamp", "收藏时间格式损坏。") from exc
    if _canonical_timestamp(parsed) != value:
        raise FavoriteError("corrupt_timestamp", "收藏时间不是规范文本。")
    return parsed


def _dump_canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _load_canonical(encoded: str, label: str) -> dict[str, Any]:
    if not isinstance(encoded, str):
        raise FavoriteError("invalid_summary", f"{label}必须是 JSON 文本。")

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise FavoriteError("invalid_summary", f"{label}包含重复字段。")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise FavoriteError("invalid_summary", f"概要包含非法数值 {value}。")

    try:
        payload = json.loads(
            encoded,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except FavoriteError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise FavoriteError("invalid_summary", f"{label}不是有效 JSON。") from exc
    if not isinstance(payload, dict):
        raise FavoriteError("invalid_summary", f"{label}必须是 JSON 对象。")
    if _dump_canonical(payload) != encoded:
        raise FavoriteError("invalid_summary", f"{label}不是规范 JSON。")
    return payload


def _expect_exact_fields(
    payload: Mapping[str, Any], fields: set[str], label: str
) -> None:
    if set(payload) != fields:
        raise FavoriteError("invalid_summary", f"{label}字段集合无效。")
    if type(payload.get("version")) is not int or (
        payload["version"] != FAVORITE_SUMMARY_VERSION
    ):
        raise FavoriteError("invalid_summary", f"{label}版本无效。")
