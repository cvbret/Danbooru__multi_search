"""Platform-neutral favorite identities, values, and validation rules."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.danbooru.post_validation import (
    PostValidationError,
    validate_post_values,
)
from danbooru_multi_search.danbooru.tag_syntax import TagSyntaxError, normalize_tag_name


_RATINGS = frozenset({"g", "s", "q", "e"})
_SQLITE_INTEGER_MIN = -(2**63)
_SQLITE_INTEGER_MAX = 2**63 - 1


class FavoriteError(RuntimeError):
    """Report one stable favorite validation or persistence failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FavoriteObjectType(StrEnum):
    POST = "post"
    TAG = "tag"
    POOL = "pool"


class FavoriteTagCategory(StrEnum):
    ARTIST = "artist"
    CHARACTER = "character"
    COPYRIGHT = "copyright"
    GENERAL = "general"


class FavoritePoolCategory(StrEnum):
    SERIES = "series"


class FavoriteFolderScope(StrEnum):
    ALL = "all"
    UNCATEGORIZED = "uncategorized"
    FOLDER = "folder"


class FavoriteSortField(StrEnum):
    FAVORITED_AT = "favorited_at"
    SCORE = "score"
    FAV_COUNT = "fav_count"


class FavoriteSortDirection(StrEnum):
    ASCENDING = "ascending"
    DESCENDING = "descending"


class FolderDeleteMode(StrEnum):
    MOVE_TO_UNCATEGORIZED = "move_to_uncategorized"
    DELETE_FAVORITES = "delete_favorites"


@dataclass(frozen=True, slots=True)
class FavoriteIdentity:
    """Stable database identity for one post, tag, or series pool."""

    object_type: FavoriteObjectType
    object_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.object_type, FavoriteObjectType):
            raise FavoriteError("invalid_object_type", "收藏对象类型无效。")
        if not isinstance(self.object_key, str):
            raise FavoriteError("invalid_object_key", "收藏对象标识必须是文本。")
        if self.object_type is FavoriteObjectType.TAG:
            if _normalize_tag(self.object_key) != self.object_key:
                raise FavoriteError("invalid_object_key", "标签标识必须使用规范形式。")
        elif (
            str(_database_object_identifier(self.object_key, "收藏对象编号"))
            != self.object_key
        ):
            raise FavoriteError("invalid_object_key", "收藏对象编号必须使用规范形式。")


@dataclass(frozen=True, slots=True)
class FavoriteCandidate:
    """Validated metadata supplied when adding or refreshing a favorite."""

    identity: FavoriteIdentity
    display_name: str
    tag_category: FavoriteTagCategory | None = None
    pool_category: FavoritePoolCategory | None = None
    post: DanbooruPost | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, FavoriteIdentity):
            raise FavoriteError("invalid_identity", "收藏身份无效。")
        _validate_display_name(self.display_name)
        object_type = self.identity.object_type
        if object_type is FavoriteObjectType.POST:
            if self.tag_category is not None or self.pool_category is not None:
                raise FavoriteError("invalid_metadata", "图片作品不能带有其他分类。")
            if not isinstance(self.post, DanbooruPost):
                raise FavoriteError("invalid_metadata", "图片作品缺少作品概要。")
            if self.post.id != int(self.identity.object_key):
                raise FavoriteError("identity_mismatch", "作品编号与收藏身份不一致。")
            _validate_post(self.post)
        elif object_type is FavoriteObjectType.TAG:
            if not isinstance(self.tag_category, FavoriteTagCategory):
                raise FavoriteError("invalid_tag_category", "标签收藏缺少有效分类。")
            if self.pool_category is not None or self.post is not None:
                raise FavoriteError("invalid_metadata", "标签收藏包含不适用的概要。")
        elif object_type is FavoriteObjectType.POOL:
            if self.pool_category is not FavoritePoolCategory.SERIES:
                raise FavoriteError("invalid_pool_category", "当前只支持系列作品池收藏。")
            if self.tag_category is not None or self.post is not None:
                raise FavoriteError("invalid_metadata", "作品池收藏包含不适用的概要。")

    @classmethod
    def for_post(
        cls, post: DanbooruPost, *, display_name: str | None = None
    ) -> FavoriteCandidate:
        if not isinstance(post, DanbooruPost):
            raise FavoriteError("invalid_metadata", "图片作品缺少作品概要。")
        identifier = _positive_identifier(post.id, "作品编号")
        return cls(
            FavoriteIdentity(FavoriteObjectType.POST, str(identifier)),
            display_name if display_name is not None else str(post.id),
            post=post,
        )

    @classmethod
    def for_tag(
        cls,
        tag_name: str,
        category: FavoriteTagCategory,
        *,
        display_name: str | None = None,
    ) -> FavoriteCandidate:
        normalized = _normalize_tag(tag_name)
        return cls(
            FavoriteIdentity(FavoriteObjectType.TAG, normalized),
            display_name if display_name is not None else normalized,
            tag_category=category,
        )

    @classmethod
    def for_pool(cls, pool_id: int, display_name: str) -> FavoriteCandidate:
        identifier = _positive_identifier(pool_id, "作品池编号")
        return cls(
            FavoriteIdentity(FavoriteObjectType.POOL, str(identifier)),
            display_name,
            pool_category=FavoritePoolCategory.SERIES,
        )


@dataclass(frozen=True, slots=True)
class FavoriteFolder:
    id: int
    name: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class FavoriteItem:
    id: int
    identity: FavoriteIdentity
    display_name: str
    folder_id: int | None
    favorited_at: datetime
    updated_at: datetime
    tag_category: FavoriteTagCategory | None = None
    pool_category: FavoritePoolCategory | None = None
    post: DanbooruPost | None = None


@dataclass(frozen=True, slots=True)
class FavoritePageSnapshot:
    """One consistent page, count, folder list, and collection revision."""

    revision: int
    total_count: int
    items: tuple[FavoriteItem, ...]
    folders: tuple[FavoriteFolder, ...]


@dataclass(frozen=True, slots=True)
class FavoriteMutationSnapshot:
    """Validated affected records and folder references from one read."""

    revision: int
    items: tuple[FavoriteItem, ...]
    folder: FavoriteFolder | None = None
    target_folder: FavoriteFolder | None = None


@dataclass(frozen=True, slots=True)
class FavoriteFolderDraft:
    """One validated folder supplied by a trusted persistence service."""

    name: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _normalize_folder_name(self.name)
        favorite_timestamp_utc(self.created_at)
        favorite_timestamp_utc(self.updated_at)
        if self.updated_at < self.created_at:
            raise FavoriteError(
                "invalid_time_order",
                "收藏文件夹修改时间不能早于创建时间。",
            )


@dataclass(frozen=True, slots=True)
class FavoriteItemDraft:
    """One validated favorite with a document-local folder index."""

    candidate: FavoriteCandidate
    folder_index: int | None
    favorited_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _validate_candidate(self.candidate)
        if self.folder_index is not None and (
            type(self.folder_index) is not int or self.folder_index < 0
        ):
            raise FavoriteError("invalid_folder_reference", "收藏文件夹引用无效。")
        favorite_timestamp_utc(self.favorited_at)
        favorite_timestamp_utc(self.updated_at)
        if self.updated_at < self.favorited_at:
            raise FavoriteError(
                "invalid_time_order",
                "收藏修改时间不能早于首次收藏时间。",
            )


@dataclass(frozen=True, slots=True)
class FavoriteCollectionSnapshot:
    """One immutable and signature-bound view of all local favorites."""

    revision: int
    signature: str
    folders: tuple[FavoriteFolder, ...]
    favorites: tuple[FavoriteItem, ...]


@dataclass(frozen=True, slots=True)
class FavoriteFilter:
    folder_scope: FavoriteFolderScope = FavoriteFolderScope.ALL
    folder_id: int | None = None
    object_type: FavoriteObjectType | None = None
    tag_category: FavoriteTagCategory | None = None
    exact_tag: str | None = None
    exact_artist: str | None = None
    rating: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.folder_scope, FavoriteFolderScope):
            raise FavoriteError("invalid_folder_scope", "收藏文件夹范围无效。")
        if self.folder_scope is FavoriteFolderScope.FOLDER:
            _positive_identifier(self.folder_id, "文件夹编号")
        elif self.folder_id is not None:
            raise FavoriteError("invalid_folder_scope", "当前范围不能带文件夹编号。")
        if self.object_type is not None and not isinstance(
            self.object_type, FavoriteObjectType
        ):
            raise FavoriteError("invalid_object_type", "收藏对象类型无效。")
        if self.tag_category is not None:
            if not isinstance(self.tag_category, FavoriteTagCategory):
                raise FavoriteError("invalid_tag_category", "标签分类无效。")
            if self.object_type not in (None, FavoriteObjectType.TAG):
                raise FavoriteError("invalid_tag_category", "标签分类只能筛选标签收藏。")
        if self.exact_tag is not None:
            _normalize_tag(self.exact_tag)
        if self.exact_artist is not None:
            _normalize_tag(self.exact_artist)
        if self.rating is not None and self.rating not in _RATINGS:
            raise FavoriteError("invalid_rating", "内容分级无效。")


@dataclass(frozen=True, slots=True)
class FavoriteSort:
    field: FavoriteSortField = FavoriteSortField.FAVORITED_AT
    direction: FavoriteSortDirection = FavoriteSortDirection.DESCENDING

    def __post_init__(self) -> None:
        if not isinstance(self.field, FavoriteSortField) or not isinstance(
            self.direction, FavoriteSortDirection
        ):
            raise FavoriteError("invalid_sort", "收藏排序条件无效。")


def _validate_candidate(candidate: FavoriteCandidate) -> FavoriteCandidate:
    if not isinstance(candidate, FavoriteCandidate):
        raise FavoriteError("invalid_metadata", "收藏概要无效。")
    return candidate


def favorite_folder_key(name: str) -> str:
    """Return the Unicode-normalized, case-insensitive folder identity."""

    return _normalize_folder_name(name)[1]


def candidate_from_favorite_item(item: FavoriteItem) -> FavoriteCandidate:
    """Recreate the validated overview input represented by a stored favorite."""

    if not isinstance(item, FavoriteItem):
        raise FavoriteError("invalid_favorite", "收藏记录无效。")
    return _candidate_from_item(item)


def _candidate_from_item(item: FavoriteItem) -> FavoriteCandidate:
    if item.identity.object_type is FavoriteObjectType.POST:
        return FavoriteCandidate(item.identity, item.display_name, post=item.post)
    if item.identity.object_type is FavoriteObjectType.TAG:
        return FavoriteCandidate(
            item.identity,
            item.display_name,
            tag_category=item.tag_category,
        )
    return FavoriteCandidate(
        item.identity,
        item.display_name,
        pool_category=item.pool_category,
    )


def _post_tag_pairs(post: DanbooruPost) -> tuple[tuple[str, str], ...]:
    groups = (
        ("artist", post.artist_tags),
        ("character", post.character_tags),
        ("copyright", post.copyright_tags),
        ("general", post.general_tags),
        ("meta", post.meta_tags),
    )
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for category, tags in groups:
        if not isinstance(tags, tuple):
            raise FavoriteError("invalid_summary", "作品标签组必须是不可变序列。")
        for tag in tags:
            normalized = _normalize_tag(tag)
            if normalized != tag or normalized in seen:
                raise FavoriteError("invalid_summary", "作品标签未规范化或存在重复。")
            seen.add(normalized)
            pairs.append((category, normalized))
    return tuple(pairs)


def _validate_post(post: DanbooruPost) -> None:
    if isinstance(post.id, bool) or not isinstance(post.id, int) or post.id < 1:
        raise FavoriteError("invalid_summary", "作品编号必须是正整数。")
    if post.rating not in _RATINGS:
        raise FavoriteError("invalid_summary", "作品内容分级无效。")
    if post.fav_count is not None and (
        isinstance(post.fav_count, bool)
        or not isinstance(post.fav_count, int)
        or post.fav_count < 0
        or post.fav_count > _SQLITE_INTEGER_MAX
    ):
        raise FavoriteError("invalid_summary", "作品收藏数量无效。")
    if post.score is not None and (
        isinstance(post.score, bool)
        or not isinstance(post.score, int)
        or not _SQLITE_INTEGER_MIN <= post.score <= _SQLITE_INTEGER_MAX
    ):
        raise FavoriteError("invalid_summary", "作品得分无效。")
    try:
        validate_post_values(post)
    except PostValidationError as exc:
        raise FavoriteError("invalid_summary", f"作品概要无效：{exc}") from exc
    _post_tag_pairs(post)


def _normalize_folder_name(name: str) -> tuple[str, str]:
    if not isinstance(name, str):
        raise FavoriteError("invalid_folder_name", "收藏文件夹名称必须是文本。")
    display_name = unicodedata.normalize("NFC", name.strip())
    if not display_name:
        raise FavoriteError("invalid_folder_name", "收藏文件夹名称不能为空。")
    if any(unicodedata.category(character).startswith("C") for character in display_name):
        raise FavoriteError("invalid_folder_name", "收藏文件夹名称包含控制字符。")
    return display_name, display_name.casefold()


def _validate_display_name(value: Any) -> str:
    if not isinstance(value, str):
        raise FavoriteError("invalid_display_name", "收藏显示名称必须是文本。")
    if not value.strip():
        raise FavoriteError("invalid_display_name", "收藏显示名称不能为空。")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise FavoriteError("invalid_display_name", "收藏显示名称包含控制字符。")
    return value


def _normalize_tag(value: str) -> str:
    try:
        return normalize_tag_name(value)
    except (TagSyntaxError, TypeError, ValueError) as exc:
        raise FavoriteError("invalid_tag", f"收藏标签无效：{exc}") from exc


def _positive_identifier(value: Any, label: str) -> int:
    if type(value) is not int:
        raise FavoriteError("invalid_identifier", f"{label}必须是正整数。")
    if value < 1 or value > _SQLITE_INTEGER_MAX:
        raise FavoriteError("invalid_identifier", f"{label}必须是正整数。")
    return value


def _database_object_identifier(value: Any, label: str) -> int:
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or not value.isdecimal()
    ):
        raise FavoriteError("invalid_object_key", f"{label}文本无效。")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise FavoriteError("invalid_object_key", f"{label}文本无效。") from exc
    if parsed < 1 or parsed > _SQLITE_INTEGER_MAX:
        raise FavoriteError("invalid_object_key", f"{label}文本无效。")
    return parsed


def _optional_positive_identifier(value: Any, label: str) -> int | None:
    return None if value is None else _positive_identifier(value, label)


def _batch_identifiers(
    values: Iterable[int], *, allow_empty: bool
) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)):
        raise FavoriteError("invalid_batch", "批量收藏编号必须是整数序列。")
    try:
        identifiers = tuple(_positive_identifier(value, "收藏编号") for value in values)
    except TypeError as exc:
        raise FavoriteError("invalid_batch", "批量收藏编号必须是整数序列。") from exc
    if len(set(identifiers)) != len(identifiers):
        raise FavoriteError("duplicate_batch_item", "批量操作包含重复收藏编号。")
    if not identifiers and not allow_empty:
        raise FavoriteError("empty_batch", "批量移动至少需要一项收藏。")
    return identifiers


def _bounded_integer(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FavoriteError("invalid_pagination", f"{label} 必须是整数。")
    if value < minimum or (maximum is not None and value > maximum):
        raise FavoriteError("invalid_pagination", f"{label} 超出允许范围。")
    return value


def favorite_timestamp_utc(value: datetime) -> datetime:
    """Validate a favorite time and return its equivalent UTC value."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise FavoriteError("invalid_timestamp", "收藏时间必须包含时区。")
    try:
        utc = value.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise FavoriteError("invalid_timestamp", "收藏时间超出允许范围。") from exc
    return utc
