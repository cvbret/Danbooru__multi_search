"""Persistent storage and database migrations."""

from importlib import import_module
from typing import Any


# Public names are grouped by their owning module so importing the package
# does not load network, Qt, or storage implementations before they are used.
_EXPORT_GROUPS = {
    "danbooru_multi_search.core.pool_progress": (
        "PoolProgressError", "PoolReadingProgress", "ResolvedPoolPosition",
        "normalize_api_source", "resolve_pool_progress",
    ),
    "danbooru_multi_search.storage.pool_progress": (
        "PoolProgressRepository",
    ),
    "danbooru_multi_search.storage.database": (
        "Database",
        "DatabaseError",
    ),
    "danbooru_multi_search.core.favorites": (
        "FavoriteCandidate",
        "FavoriteCollectionSnapshot",
        "FavoriteError",
        "FavoriteFilter",
        "FavoriteFolder",
        "FavoriteFolderScope",
        "FavoriteIdentity",
        "FavoriteItem",
        "FavoriteObjectType",
        "FavoritePoolCategory",
        "FavoriteSort",
        "FavoriteSortDirection",
        "FavoriteSortField",
        "FavoriteTagCategory",
        "FolderDeleteMode",
        "candidate_from_favorite_item",
        "favorite_folder_key",
    ),
    "danbooru_multi_search.storage.favorite_codec": (
        "FAVORITE_SUMMARY_VERSION",
        "decode_favorite_summary",
        "decode_favorite_timestamp",
        "encode_favorite_summary",
        "encode_favorite_timestamp",
    ),
    "danbooru_multi_search.storage.favorites": (
        "FavoriteRepository",
    ),
    "danbooru_multi_search.storage.favorite_transfer": (
        "FAVORITE_TRANSFER_DOCUMENT_TYPE",
        "FAVORITE_TRANSFER_VERSION",
        "MAX_FAVORITE_TRANSFER_BYTES",
        "FavoriteImportMode",
        "FavoriteImportPreview",
        "FavoriteTransferDocument",
        "FavoriteTransferError",
        "FavoriteTransferFolder",
        "FavoriteTransferItem",
        "FavoriteTransferService",
        "decode_favorite_transfer_document",
        "encode_favorite_transfer_document",
        "read_favorite_transfer_document",
        "write_favorite_transfer_document",
    ),
    "danbooru_multi_search.storage.history": (
        "BrowserSession",
        "GalleryPosition",
        "HistoryError",
        "HistoryRepository",
        "RecentQuery",
        "RecentTag",
        "WindowGeometry",
        "canonicalize_query_tags",
    ),
    "danbooru_multi_search.storage.query_serialization": (
        "QUERY_DEFINITION_VERSION",
        "RESULT_SUMMARY_VERSION",
        "QuerySerializationError",
        "decode_query_definition",
        "decode_result_summary",
        "encode_query_definition",
        "encode_result_summary",
        "query_definition_key",
        "query_definition_tags",
    ),
    "danbooru_multi_search.storage.post_snapshot": (
        "POST_SNAPSHOT_VERSION",
        "PostSnapshotError",
        "decode_post_snapshot",
        "encode_post_snapshot",
    ),
}
_EXPORTS = {
    name: module
    for module, names in _EXPORT_GROUPS.items()
    for name in names
}

__all__ = [
    "PoolProgressError",
    "PoolReadingProgress",
    "ResolvedPoolPosition",
    "normalize_api_source",
    "resolve_pool_progress",
    "PoolProgressRepository",
    "BrowserSession",
    "Database",
    "DatabaseError",
    "FAVORITE_SUMMARY_VERSION",
    "FAVORITE_TRANSFER_DOCUMENT_TYPE",
    "FAVORITE_TRANSFER_VERSION",
    "FavoriteCandidate",
    "FavoriteCollectionSnapshot",
    "FavoriteError",
    "FavoriteFilter",
    "FavoriteFolder",
    "FavoriteFolderScope",
    "FavoriteIdentity",
    "FavoriteItem",
    "FavoriteObjectType",
    "FavoritePoolCategory",
    "FavoriteRepository",
    "FavoriteSort",
    "FavoriteSortDirection",
    "FavoriteSortField",
    "FavoriteTagCategory",
    "FavoriteImportMode",
    "FavoriteImportPreview",
    "FavoriteTransferDocument",
    "FavoriteTransferError",
    "FavoriteTransferFolder",
    "FavoriteTransferItem",
    "FavoriteTransferService",
    "FolderDeleteMode",
    "GalleryPosition",
    "HistoryError",
    "HistoryRepository",
    "MAX_FAVORITE_TRANSFER_BYTES",
    "POST_SNAPSHOT_VERSION",
    "PostSnapshotError",
    "QUERY_DEFINITION_VERSION",
    "RESULT_SUMMARY_VERSION",
    "QuerySerializationError",
    "RecentQuery",
    "RecentTag",
    "WindowGeometry",
    "canonicalize_query_tags",
    "candidate_from_favorite_item",
    "decode_favorite_summary",
    "decode_favorite_timestamp",
    "decode_favorite_transfer_document",
    "decode_query_definition",
    "decode_result_summary",
    "decode_post_snapshot",
    "encode_query_definition",
    "encode_result_summary",
    "encode_post_snapshot",
    "encode_favorite_summary",
    "encode_favorite_timestamp",
    "encode_favorite_transfer_document",
    "favorite_folder_key",
    "query_definition_key",
    "query_definition_tags",
    "read_favorite_transfer_document",
    "write_favorite_transfer_document",
]


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
