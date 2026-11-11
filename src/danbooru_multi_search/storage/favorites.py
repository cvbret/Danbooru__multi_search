"""Transactional local-favorite folders, objects, and metadata."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterator

from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.danbooru.tag_syntax import TagSyntaxError, normalize_tag_name
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.post_snapshot import (
    PostSnapshotError,
    decode_post_snapshot,
    encode_post_snapshot,
)


FAVORITE_SUMMARY_VERSION = 1
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
class FavoriteFolderDraft:
    """One validated folder supplied by a trusted persistence service."""

    name: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _normalize_folder_name(self.name)
        _canonical_timestamp(self.created_at)
        _canonical_timestamp(self.updated_at)
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
        _canonical_timestamp(self.favorited_at)
        _canonical_timestamp(self.updated_at)
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


class FavoriteRepository:
    """Persist local favorites with one write transaction per operation."""

    def __init__(
        self, database: Database, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        self._database = database
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()

    def create_folder(
        self, name: str, *, created_at: datetime | None = None
    ) -> FavoriteFolder:
        display_name, normalized_name = _normalize_folder_name(name)
        timestamp = _canonical_timestamp(created_at or self._clock())
        with self._write_connection() as connection:
            try:
                cursor = connection.execute(
                    """INSERT INTO favorite_folders(
                           name, normalized_name, created_at, updated_at
                       ) VALUES (?, ?, ?, ?)""",
                    (display_name, normalized_name, timestamp, timestamp),
                )
            except sqlite3.IntegrityError as exc:
                if _folder_name_exists(connection, normalized_name):
                    raise FavoriteError("duplicate_folder", "已经存在同名收藏文件夹。") from exc
                raise
            result = _row_to_folder(
                connection.execute(
                    "SELECT * FROM favorite_folders WHERE id = ?",
                    (int(cursor.lastrowid),),
                ).fetchone()
            )
            _bump_collection_revision(connection)
            return result

    def rename_folder(
        self,
        folder_id: int,
        name: str,
        *,
        updated_at: datetime | None = None,
    ) -> FavoriteFolder:
        identifier = _positive_identifier(folder_id, "文件夹编号")
        display_name, normalized_name = _normalize_folder_name(name)
        timestamp = _canonical_timestamp(updated_at or self._clock())
        with self._write_connection() as connection:
            current = _row_to_folder(_require_folder(connection, identifier))
            if current.name == display_name:
                return current
            try:
                connection.execute(
                    """UPDATE favorite_folders
                       SET name = ?, normalized_name = ?, updated_at = ?
                       WHERE id = ?""",
                    (display_name, normalized_name, timestamp, identifier),
                )
            except sqlite3.IntegrityError as exc:
                if _folder_name_exists(connection, normalized_name, excluding=identifier):
                    raise FavoriteError("duplicate_folder", "已经存在同名收藏文件夹。") from exc
                raise
            result = _row_to_folder(
                connection.execute(
                    "SELECT * FROM favorite_folders WHERE id = ?", (identifier,)
                ).fetchone()
            )
            _bump_collection_revision(connection)
            return result

    def get_folder(self, folder_id: int) -> FavoriteFolder:
        identifier = _positive_identifier(folder_id, "文件夹编号")
        with self._read_connection() as connection:
            return _row_to_folder(_require_folder(connection, identifier))

    def list_folders(self) -> tuple[FavoriteFolder, ...]:
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM favorite_folders ORDER BY normalized_name, id"
            ).fetchall()
            return tuple(_row_to_folder(row) for row in rows)

    def delete_folder(self, folder_id: int, mode: FolderDeleteMode) -> int:
        identifier = _positive_identifier(folder_id, "文件夹编号")
        if not isinstance(mode, FolderDeleteMode):
            raise FavoriteError("invalid_delete_mode", "收藏文件夹删除方式无效。")
        with self._write_connection() as connection:
            _require_folder(connection, identifier)
            rows = connection.execute(
                "SELECT * FROM favorite_items WHERE folder_id = ? ORDER BY id",
                (identifier,),
            ).fetchall()
            for row in rows:
                _row_to_item(connection, row)
            count = len(rows)
            if mode is FolderDeleteMode.MOVE_TO_UNCATEGORIZED:
                timestamp = _canonical_timestamp(self._clock())
                connection.execute(
                    """UPDATE favorite_items
                       SET folder_id = NULL, updated_at = ?
                       WHERE folder_id = ?""",
                    (timestamp, identifier),
                )
            else:
                connection.execute(
                    "DELETE FROM favorite_items WHERE folder_id = ?", (identifier,)
                )
            connection.execute("DELETE FROM favorite_folders WHERE id = ?", (identifier,))
            _bump_collection_revision(connection)
            return count

    def add_favorite(
        self,
        candidate: FavoriteCandidate,
        *,
        folder_id: int | None = None,
        favorited_at: datetime | None = None,
    ) -> FavoriteItem:
        validated = _validate_candidate(candidate)
        folder_identifier = _optional_positive_identifier(folder_id, "文件夹编号")
        with self._write_connection() as connection:
            existing = _select_identity(connection, validated.identity)
            if existing is not None:
                return _row_to_item(connection, existing)
            if folder_identifier is not None:
                _require_folder(connection, folder_identifier)
            timestamp = _canonical_timestamp(favorited_at or self._clock())
            favorite_id = _insert_favorite(
                connection,
                validated,
                folder_identifier,
                timestamp,
                timestamp,
            )
            result = _row_to_item(
                connection,
                connection.execute(
                    "SELECT * FROM favorite_items WHERE id = ?", (favorite_id,)
                ).fetchone(),
            )
            _bump_collection_revision(connection)
            return result

    def refresh_metadata(
        self,
        candidate: FavoriteCandidate,
        *,
        updated_at: datetime | None = None,
    ) -> FavoriteItem:
        validated = _validate_candidate(candidate)
        timestamp = _canonical_timestamp(updated_at or self._clock())
        with self._write_connection() as connection:
            row = _select_identity(connection, validated.identity)
            if row is None:
                raise FavoriteError("unknown_favorite", "找不到需要更新的收藏。")
            current = _row_to_item(connection, row)
            if encode_favorite_summary(_candidate_from_item(current)) == (
                encode_favorite_summary(validated)
            ):
                return current
            encoded, score, fav_count, rating = _encode_candidate(validated)
            connection.execute(
                """UPDATE favorite_items SET
                       tag_category = ?, display_name = ?, summary_version = ?,
                       summary_json = ?, score = ?, fav_count = ?, rating = ?,
                       updated_at = ?
                   WHERE id = ?""",
                (
                    validated.tag_category.value if validated.tag_category else None,
                    validated.display_name,
                    FAVORITE_SUMMARY_VERSION,
                    encoded,
                    score,
                    fav_count,
                    rating,
                    timestamp,
                    int(row["id"]),
                ),
            )
            if validated.post is not None:
                _replace_post_tags(connection, int(row["id"]), validated.post)
            else:
                connection.execute(
                    "DELETE FROM favorite_post_tags WHERE favorite_id = ?",
                    (int(row["id"]),),
                )
            result = _row_to_item(
                connection,
                connection.execute(
                    "SELECT * FROM favorite_items WHERE id = ?", (int(row["id"]),)
                ).fetchone(),
            )
            _bump_collection_revision(connection)
            return result

    def get_favorite(self, favorite_id: int) -> FavoriteItem:
        identifier = _positive_identifier(favorite_id, "收藏编号")
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM favorite_items WHERE id = ?", (identifier,)
            ).fetchone()
            if row is None:
                raise FavoriteError("unknown_favorite", "找不到指定收藏。")
            return _row_to_item(connection, row)

    def get_by_identity(self, identity: FavoriteIdentity) -> FavoriteItem | None:
        if not isinstance(identity, FavoriteIdentity):
            raise FavoriteError("invalid_identity", "收藏身份无效。")
        with self._read_connection() as connection:
            row = _select_identity(connection, identity)
            return _row_to_item(connection, row) if row is not None else None

    def delete_favorite(self, favorite_id: int) -> bool:
        identifier = _positive_identifier(favorite_id, "收藏编号")
        with self._write_connection() as connection:
            row = connection.execute(
                "SELECT * FROM favorite_items WHERE id = ?", (identifier,)
            ).fetchone()
            if row is None:
                return False
            _row_to_item(connection, row)
            connection.execute("DELETE FROM favorite_items WHERE id = ?", (identifier,))
            _bump_collection_revision(connection)
            return True

    def move_favorites(
        self, favorite_ids: Iterable[int], folder_id: int | None
    ) -> int:
        identifiers = _batch_identifiers(favorite_ids, allow_empty=False)
        folder_identifier = _optional_positive_identifier(folder_id, "文件夹编号")
        with self._write_connection() as connection:
            if folder_identifier is not None:
                _require_folder(connection, folder_identifier)
            items = _require_favorites(connection, identifiers)
            changed_ids = tuple(
                item.id for item in items if item.folder_id != folder_identifier
            )
            if not changed_ids:
                return 0
            timestamp = _canonical_timestamp(self._clock())
            placeholders = ",".join("?" for _ in changed_ids)
            connection.execute(
                f"""UPDATE favorite_items
                    SET folder_id = ?, updated_at = ?
                    WHERE id IN ({placeholders})""",
                (folder_identifier, timestamp, *changed_ids),
            )
            _bump_collection_revision(connection)
            return len(changed_ids)

    def delete_favorites(self, favorite_ids: Iterable[int]) -> int:
        identifiers = _batch_identifiers(favorite_ids, allow_empty=True)
        if not identifiers:
            return 0
        with self._write_connection() as connection:
            _require_favorites(connection, identifiers)
            placeholders = ",".join("?" for _ in identifiers)
            connection.execute(
                f"DELETE FROM favorite_items WHERE id IN ({placeholders})", identifiers
            )
            _bump_collection_revision(connection)
            return len(identifiers)

    def list_favorites(
        self,
        filter_spec: FavoriteFilter | None = None,
        sort_spec: FavoriteSort | None = None,
        *,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[FavoriteItem, ...]:
        filters = filter_spec if filter_spec is not None else FavoriteFilter()
        sorting = sort_spec if sort_spec is not None else FavoriteSort()
        if not isinstance(filters, FavoriteFilter):
            raise FavoriteError("invalid_filter", "收藏筛选条件无效。")
        if not isinstance(sorting, FavoriteSort):
            raise FavoriteError("invalid_sort", "收藏排序条件无效。")
        page_limit = _bounded_integer(limit, "limit", minimum=1, maximum=1000)
        page_offset = _bounded_integer(
            offset,
            "offset",
            minimum=0,
            maximum=_SQLITE_INTEGER_MAX,
        )
        clauses: list[str] = []
        parameters: list[Any] = []
        if filters.folder_scope is FavoriteFolderScope.UNCATEGORIZED:
            clauses.append("fi.folder_id IS NULL")
        elif filters.folder_scope is FavoriteFolderScope.FOLDER:
            clauses.append("fi.folder_id = ?")
            parameters.append(filters.folder_id)
        if filters.object_type is not None:
            clauses.append("fi.object_type = ?")
            parameters.append(filters.object_type.value)
        if filters.tag_category is not None:
            clauses.append("fi.tag_category = ?")
            parameters.append(filters.tag_category.value)
        if filters.exact_tag is not None:
            clauses.append(
                """EXISTS (
                       SELECT 1 FROM favorite_post_tags AS ft
                       WHERE ft.favorite_id = fi.id AND ft.tag_name = ?
                   )"""
            )
            parameters.append(_normalize_tag(filters.exact_tag))
        if filters.exact_artist is not None:
            clauses.append(
                """EXISTS (
                       SELECT 1 FROM favorite_post_tags AS fa
                       WHERE fa.favorite_id = fi.id
                         AND fa.tag_category = 'artist' AND fa.tag_name = ?
                   )"""
            )
            parameters.append(_normalize_tag(filters.exact_artist))
        if filters.rating is not None:
            clauses.append("fi.object_type = 'post' AND fi.rating = ?")
            parameters.append(filters.rating)
        where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        direction = (
            "ASC"
            if sorting.direction is FavoriteSortDirection.ASCENDING
            else "DESC"
        )
        if sorting.field is FavoriteSortField.FAVORITED_AT:
            order_sql = f"fi.favorited_at {direction}, fi.id ASC"
        elif sorting.field is FavoriteSortField.SCORE:
            order_sql = f"(fi.score IS NULL), fi.score {direction}, fi.id ASC"
        else:
            order_sql = f"(fi.fav_count IS NULL), fi.fav_count {direction}, fi.id ASC"
        with self._read_connection() as connection:
            if filters.folder_scope is FavoriteFolderScope.FOLDER:
                _require_folder(connection, int(filters.folder_id))
            rows = connection.execute(
                f"""SELECT fi.* FROM favorite_items AS fi
                    {where_sql}
                    ORDER BY {order_sql}
                    LIMIT ? OFFSET ?""",
                (*parameters, page_limit, page_offset),
            ).fetchall()
            return tuple(_row_to_item(connection, row) for row in rows)

    def capture_collection(self) -> FavoriteCollectionSnapshot:
        """Read every folder and favorite from one consistent transaction."""

        with self._read_connection() as connection:
            return _collection_snapshot(connection)

    def apply_collection_import(
        self,
        *,
        expected_revision: int,
        expected_document_digest: str,
        actual_document_digest: str,
        expected_binding_digest: str,
        actual_binding_digest: str,
        folders: tuple[FavoriteFolderDraft, ...],
        favorites: tuple[FavoriteItemDraft, ...],
        replace: bool,
    ) -> FavoriteCollectionSnapshot:
        """Apply one validated transfer after rechecking the preview signature."""

        _validate_collection_import(
            expected_revision,
            expected_document_digest,
            actual_document_digest,
            expected_binding_digest,
            actual_binding_digest,
            folders,
            favorites,
            replace,
        )
        with self._write_connection() as connection:
            if (
                expected_document_digest != actual_document_digest
                or expected_binding_digest != actual_binding_digest
            ):
                raise FavoriteError("invalid_preview", "收藏导入预览已被修改。")
            current = _collection_snapshot(connection)
            if current.revision != expected_revision:
                raise FavoriteError(
                    "stale_preview",
                    "收藏数据已经变化，请重新预览导入影响。",
                )
            changed = bool(
                replace
                and (current.folders or current.favorites or folders or favorites)
            )
            if replace:
                connection.execute("DELETE FROM favorite_items")
                connection.execute("DELETE FROM favorite_folders")

            folder_ids: list[int] = []
            existing_folders = {
                _normalize_folder_name(folder.name)[1]: folder.id
                for folder in current.folders
            }
            for folder in folders:
                display_name, normalized_name = _normalize_folder_name(folder.name)
                folder_id = None if replace else existing_folders.get(normalized_name)
                if folder_id is None:
                    cursor = connection.execute(
                        """INSERT INTO favorite_folders(
                               name, normalized_name, created_at, updated_at
                           ) VALUES (?, ?, ?, ?)""",
                        (
                            display_name,
                            normalized_name,
                            _canonical_timestamp(folder.created_at),
                            _canonical_timestamp(folder.updated_at),
                        ),
                    )
                    folder_id = int(cursor.lastrowid)
                    existing_folders[normalized_name] = folder_id
                    changed = True
                folder_ids.append(folder_id)

            existing_identities = {
                item.identity for item in current.favorites
            } if not replace else set()
            for favorite in favorites:
                if favorite.candidate.identity in existing_identities:
                    continue
                folder_id = (
                    None
                    if favorite.folder_index is None
                    else folder_ids[favorite.folder_index]
                )
                _insert_favorite(
                    connection,
                    favorite.candidate,
                    folder_id,
                    _canonical_timestamp(favorite.favorited_at),
                    _canonical_timestamp(favorite.updated_at),
                )
                existing_identities.add(favorite.candidate.identity)
                changed = True
            if changed:
                _bump_collection_revision(connection)
            return _collection_snapshot(connection)

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock, closing(self._database.connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except FavoriteError:
                connection.rollback()
                raise
            except sqlite3.Error as exc:
                connection.rollback()
                raise FavoriteError(
                    "storage_error", f"收藏数据库操作失败：{exc}"
                ) from exc
            except Exception:
                connection.rollback()
                raise

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock, closing(self._database.connect()) as connection:
            try:
                connection.execute("BEGIN")
                yield connection
                connection.rollback()
            except FavoriteError:
                connection.rollback()
                raise
            except sqlite3.Error as exc:
                connection.rollback()
                raise FavoriteError(
                    "storage_error", f"收藏数据库读取失败：{exc}"
                ) from exc


def _validate_candidate(candidate: FavoriteCandidate) -> FavoriteCandidate:
    if not isinstance(candidate, FavoriteCandidate):
        raise FavoriteError("invalid_metadata", "收藏概要无效。")
    return candidate


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


def favorite_folder_key(name: str) -> str:
    """Return the Unicode-normalized, case-insensitive folder identity."""

    return _normalize_folder_name(name)[1]


def candidate_from_favorite_item(item: FavoriteItem) -> FavoriteCandidate:
    """Recreate the validated overview input represented by a stored favorite."""

    if not isinstance(item, FavoriteItem):
        raise FavoriteError("invalid_favorite", "收藏记录无效。")
    return _candidate_from_item(item)


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


def _insert_favorite(
    connection: sqlite3.Connection,
    candidate: FavoriteCandidate,
    folder_id: int | None,
    favorited_at: str,
    updated_at: str,
) -> int:
    encoded, score, fav_count, rating = _encode_candidate(candidate)
    cursor = connection.execute(
        """INSERT INTO favorite_items(
               object_type, object_key, tag_category, folder_id,
               display_name, summary_version, summary_json,
               post_id, pool_id, score, fav_count, rating,
               favorited_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            candidate.identity.object_type.value,
            candidate.identity.object_key,
            candidate.tag_category.value if candidate.tag_category else None,
            folder_id,
            candidate.display_name,
            FAVORITE_SUMMARY_VERSION,
            encoded,
            int(candidate.identity.object_key)
            if candidate.identity.object_type is FavoriteObjectType.POST
            else None,
            int(candidate.identity.object_key)
            if candidate.identity.object_type is FavoriteObjectType.POOL
            else None,
            score,
            fav_count,
            rating,
            favorited_at,
            updated_at,
        ),
    )
    favorite_id = int(cursor.lastrowid)
    if candidate.post is not None:
        _replace_post_tags(connection, favorite_id, candidate.post)
    return favorite_id


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


def _collection_snapshot(connection: sqlite3.Connection) -> FavoriteCollectionSnapshot:
    folders = tuple(
        _row_to_folder(row)
        for row in connection.execute(
            "SELECT * FROM favorite_folders ORDER BY id"
        ).fetchall()
    )
    favorites = tuple(
        _row_to_item(connection, row)
        for row in connection.execute(
            "SELECT * FROM favorite_items ORDER BY id"
        ).fetchall()
    )
    payload = {
        "favorites": [
            {
                "display_name": item.display_name,
                "favorited_at": _canonical_timestamp(item.favorited_at),
                "folder_id": item.folder_id,
                "id": item.id,
                "object_key": item.identity.object_key,
                "object_type": item.identity.object_type.value,
                "summary_json": encode_favorite_summary(_candidate_from_item(item)),
                "updated_at": _canonical_timestamp(item.updated_at),
            }
            for item in favorites
        ],
        "folders": [
            {
                "created_at": _canonical_timestamp(folder.created_at),
                "id": folder.id,
                "name": folder.name,
                "updated_at": _canonical_timestamp(folder.updated_at),
            }
            for folder in folders
        ],
    }
    encoded = _dump_canonical(payload).encode("utf-8")
    return FavoriteCollectionSnapshot(
        revision=_collection_revision(connection),
        signature=hashlib.sha256(encoded).hexdigest(),
        folders=folders,
        favorites=favorites,
    )


def _collection_revision(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT revision FROM favorite_collection_state WHERE id = 1"
    ).fetchone()
    if row is None or type(row["revision"]) is not int or row["revision"] < 0:
        raise FavoriteError("corrupt_revision", "收藏修订号记录损坏。")
    return int(row["revision"])


def _bump_collection_revision(connection: sqlite3.Connection) -> int:
    current = _collection_revision(connection)
    if current >= _SQLITE_INTEGER_MAX:
        raise FavoriteError("revision_exhausted", "收藏修订号已经达到上限。")
    updated = current + 1
    connection.execute(
        "UPDATE favorite_collection_state SET revision = ? WHERE id = 1",
        (updated,),
    )
    return updated


def _validate_collection_import(
    expected_revision: int,
    expected_document_digest: str,
    actual_document_digest: str,
    expected_binding_digest: str,
    actual_binding_digest: str,
    folders: tuple[FavoriteFolderDraft, ...],
    favorites: tuple[FavoriteItemDraft, ...],
    replace: bool,
) -> None:
    if (
        type(expected_revision) is not int
        or expected_revision < 0
        or expected_revision > _SQLITE_INTEGER_MAX
    ):
        raise FavoriteError("invalid_revision", "收藏修订号无效。")
    for value in (
        expected_document_digest,
        actual_document_digest,
        expected_binding_digest,
        actual_binding_digest,
    ):
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise FavoriteError("invalid_preview", "收藏导入预览摘要无效。")
    if type(replace) is not bool:
        raise FavoriteError("invalid_import_mode", "收藏导入方式无效。")
    if not isinstance(folders, tuple) or not all(
        isinstance(folder, FavoriteFolderDraft) for folder in folders
    ):
        raise FavoriteError("invalid_import", "收藏文件夹导入数据无效。")
    if not isinstance(favorites, tuple) or not all(
        isinstance(favorite, FavoriteItemDraft) for favorite in favorites
    ):
        raise FavoriteError("invalid_import", "收藏记录导入数据无效。")
    normalized_names = [_normalize_folder_name(folder.name)[1] for folder in folders]
    if len(set(normalized_names)) != len(normalized_names):
        raise FavoriteError("duplicate_folder", "导入数据包含重复收藏文件夹。")
    identities = [favorite.candidate.identity for favorite in favorites]
    if len(set(identities)) != len(identities):
        raise FavoriteError("duplicate_favorite", "导入数据包含重复收藏身份。")
    if any(
        favorite.folder_index is not None
        and favorite.folder_index >= len(folders)
        for favorite in favorites
    ):
        raise FavoriteError("invalid_folder_reference", "收藏文件夹引用无效。")


def _row_to_item(connection: sqlite3.Connection, row: sqlite3.Row) -> FavoriteItem:
    try:
        favorite_id = _positive_identifier(row["id"], "收藏编号")
        object_type = FavoriteObjectType(row["object_type"])
        identity = FavoriteIdentity(object_type, row["object_key"])
        display_name = _validate_display_name(row["display_name"])
        folder_id = row["folder_id"]
        if folder_id is not None:
            folder_id = _positive_identifier(folder_id, "文件夹编号")
            _require_folder(connection, folder_id)
        version = _strict_integer(row["summary_version"], "概要版本")
        if version != FAVORITE_SUMMARY_VERSION:
            raise FavoriteError("invalid_summary", "收藏概要版本不受支持。")
        encoded = row["summary_json"]
        if not isinstance(encoded, str):
            raise FavoriteError("invalid_summary", "收藏概要必须是 JSON 文本。")
        favorited_at = _parse_timestamp(row["favorited_at"])
        updated_at = _parse_timestamp(row["updated_at"])
        if updated_at < favorited_at:
            raise FavoriteError(
                "invalid_time_order",
                "收藏修改时间不能早于首次收藏时间。",
            )
        post: DanbooruPost | None = None
        tag_category: FavoriteTagCategory | None = None
        pool_category: FavoritePoolCategory | None = None
        if object_type is FavoriteObjectType.POST:
            try:
                post = decode_post_snapshot(encoded)
            except (PostSnapshotError, TypeError, ValueError) as exc:
                raise FavoriteError("invalid_summary", f"作品概要损坏：{exc}") from exc
            if post.id != int(identity.object_key):
                raise FavoriteError("identity_mismatch", "作品概要编号与收藏身份不一致。")
            _expect_equal(row["post_id"], post.id, "作品编号")
            _expect_equal(row["pool_id"], None, "作品池编号")
            _expect_equal(row["tag_category"], None, "标签分类")
            _expect_equal(row["score"], post.score, "作品得分")
            _expect_equal(row["fav_count"], post.fav_count, "收藏数量")
            _expect_equal(row["rating"], post.rating, "内容分级")
            _validate_post_tag_rows(connection, favorite_id, post)
        elif object_type is FavoriteObjectType.TAG:
            _expect_non_post_columns(row)
            tag_category = FavoriteTagCategory(row["tag_category"])
            payload = _load_canonical(encoded, "标签概要")
            _expect_exact_fields(
                payload,
                {"display_name", "object_key", "object_type", "tag_category", "version"},
                "标签概要",
            )
            if payload != {
                "display_name": display_name,
                "object_key": identity.object_key,
                "object_type": object_type.value,
                "tag_category": tag_category.value,
                "version": FAVORITE_SUMMARY_VERSION,
            }:
                raise FavoriteError("summary_mismatch", "标签概要与收藏记录不一致。")
            _validate_no_post_tags(connection, favorite_id)
        else:
            _expect_equal(row["post_id"], None, "作品编号")
            _expect_equal(row["pool_id"], int(identity.object_key), "作品池编号")
            _expect_equal(row["tag_category"], None, "标签分类")
            _expect_equal(row["score"], None, "作品得分")
            _expect_equal(row["fav_count"], None, "收藏数量")
            _expect_equal(row["rating"], None, "内容分级")
            pool_category = FavoritePoolCategory.SERIES
            payload = _load_canonical(encoded, "作品池概要")
            _expect_exact_fields(
                payload,
                {"display_name", "object_key", "object_type", "pool_category", "version"},
                "作品池概要",
            )
            if payload != {
                "display_name": display_name,
                "object_key": identity.object_key,
                "object_type": object_type.value,
                "pool_category": pool_category.value,
                "version": FAVORITE_SUMMARY_VERSION,
            }:
                raise FavoriteError("summary_mismatch", "作品池概要与收藏记录不一致。")
            _validate_no_post_tags(connection, favorite_id)
        return FavoriteItem(
            id=favorite_id,
            identity=identity,
            display_name=display_name,
            folder_id=folder_id,
            favorited_at=favorited_at,
            updated_at=updated_at,
            tag_category=tag_category,
            pool_category=pool_category,
            post=post,
        )
    except FavoriteError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise FavoriteError("corrupt_favorite", f"收藏记录损坏：{exc}") from exc


def _expect_non_post_columns(row: sqlite3.Row) -> None:
    _expect_equal(row["post_id"], None, "作品编号")
    _expect_equal(row["pool_id"], None, "作品池编号")
    _expect_equal(row["score"], None, "作品得分")
    _expect_equal(row["fav_count"], None, "收藏数量")
    _expect_equal(row["rating"], None, "内容分级")


def _replace_post_tags(
    connection: sqlite3.Connection, favorite_id: int, post: DanbooruPost
) -> None:
    connection.execute(
        "DELETE FROM favorite_post_tags WHERE favorite_id = ?", (favorite_id,)
    )
    connection.executemany(
        """INSERT INTO favorite_post_tags(favorite_id, tag_category, tag_name)
           VALUES (?, ?, ?)""",
        ((favorite_id, category, tag) for category, tag in _post_tag_pairs(post)),
    )


def _validate_post_tag_rows(
    connection: sqlite3.Connection, favorite_id: int, post: DanbooruPost
) -> None:
    actual = tuple(
        (str(row["tag_category"]), str(row["tag_name"]))
        for row in connection.execute(
            """SELECT tag_category, tag_name FROM favorite_post_tags
               WHERE favorite_id = ?
               ORDER BY tag_category, tag_name""",
            (favorite_id,),
        )
    )
    if actual != tuple(sorted(_post_tag_pairs(post))):
        raise FavoriteError("summary_mismatch", "作品标签索引与作品概要不一致。")


def _validate_no_post_tags(connection: sqlite3.Connection, favorite_id: int) -> None:
    if connection.execute(
        "SELECT 1 FROM favorite_post_tags WHERE favorite_id = ? LIMIT 1",
        (favorite_id,),
    ).fetchone() is not None:
        raise FavoriteError("summary_mismatch", "非作品收藏包含作品标签索引。")


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
        encode_post_snapshot(post)
    except PostSnapshotError as exc:
        raise FavoriteError("invalid_summary", f"作品概要无效：{exc}") from exc
    _post_tag_pairs(post)


def _select_identity(
    connection: sqlite3.Connection, identity: FavoriteIdentity
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT * FROM favorite_items
           WHERE object_type = ? AND object_key = ?""",
        (identity.object_type.value, identity.object_key),
    ).fetchone()


def _require_favorites(
    connection: sqlite3.Connection, identifiers: tuple[int, ...]
) -> tuple[FavoriteItem, ...]:
    placeholders = ",".join("?" for _ in identifiers)
    rows = connection.execute(
        f"SELECT * FROM favorite_items WHERE id IN ({placeholders}) ORDER BY id",
        identifiers,
    ).fetchall()
    if len(rows) != len(identifiers):
        raise FavoriteError("unknown_favorite", "批量操作包含未知收藏。")
    return tuple(_row_to_item(connection, row) for row in rows)


def _require_folder(connection: sqlite3.Connection, folder_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM favorite_folders WHERE id = ?", (folder_id,)
    ).fetchone()
    if row is None:
        raise FavoriteError("unknown_folder", "找不到指定收藏文件夹。")
    _row_to_folder(row)
    return row


def _row_to_folder(row: sqlite3.Row | None) -> FavoriteFolder:
    if row is None:
        raise FavoriteError("unknown_folder", "找不到指定收藏文件夹。")
    try:
        identifier = _positive_identifier(row["id"], "文件夹编号")
        name, normalized = _normalize_folder_name(row["name"])
        if normalized != row["normalized_name"]:
            raise FavoriteError("corrupt_folder", "收藏文件夹规范名称不一致。")
        created_at = _parse_timestamp(row["created_at"])
        updated_at = _parse_timestamp(row["updated_at"])
        if updated_at < created_at:
            raise FavoriteError(
                "invalid_time_order",
                "收藏文件夹修改时间不能早于创建时间。",
            )
        return FavoriteFolder(
            id=identifier,
            name=name,
            created_at=created_at,
            updated_at=updated_at,
        )
    except FavoriteError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise FavoriteError("corrupt_folder", f"收藏文件夹记录损坏：{exc}") from exc


def _normalize_folder_name(name: str) -> tuple[str, str]:
    if not isinstance(name, str):
        raise FavoriteError("invalid_folder_name", "收藏文件夹名称必须是文本。")
    display_name = unicodedata.normalize("NFC", name.strip())
    if not display_name:
        raise FavoriteError("invalid_folder_name", "收藏文件夹名称不能为空。")
    if any(unicodedata.category(character).startswith("C") for character in display_name):
        raise FavoriteError("invalid_folder_name", "收藏文件夹名称包含控制字符。")
    return display_name, display_name.casefold()


def _folder_name_exists(
    connection: sqlite3.Connection,
    normalized_name: str,
    *,
    excluding: int | None = None,
) -> bool:
    if excluding is None:
        row = connection.execute(
            "SELECT 1 FROM favorite_folders WHERE normalized_name = ?",
            (normalized_name,),
        ).fetchone()
    else:
        row = connection.execute(
            """SELECT 1 FROM favorite_folders
               WHERE normalized_name = ? AND id != ?""",
            (normalized_name, excluding),
        ).fetchone()
    return row is not None


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


def _strict_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FavoriteError("corrupt_favorite", f"{label}不是整数。")
    return value


def _canonical_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise FavoriteError("invalid_timestamp", "收藏时间必须包含时区。")
    try:
        utc = value.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise FavoriteError("invalid_timestamp", "收藏时间超出允许范围。") from exc
    return utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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


def _expect_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected or isinstance(actual, bool):
        raise FavoriteError("summary_mismatch", f"{label}与收藏概要不一致。")
