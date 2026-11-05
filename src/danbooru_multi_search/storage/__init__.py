"""Persistent storage and database migrations."""

from danbooru_multi_search.storage.database import Database, DatabaseError
from danbooru_multi_search.storage.history import (
    BrowserSession,
    GalleryPosition,
    HistoryError,
    HistoryRepository,
    RecentQuery,
    RecentTag,
    WindowGeometry,
    canonicalize_query_tags,
)
from danbooru_multi_search.storage.query_serialization import (
    QUERY_DEFINITION_VERSION,
    RESULT_SUMMARY_VERSION,
    QuerySerializationError,
    decode_query_definition,
    decode_result_summary,
    encode_query_definition,
    encode_result_summary,
    query_definition_key,
    query_definition_tags,
)

__all__ = [
    "BrowserSession",
    "Database",
    "DatabaseError",
    "GalleryPosition",
    "HistoryError",
    "HistoryRepository",
    "QUERY_DEFINITION_VERSION",
    "RESULT_SUMMARY_VERSION",
    "QuerySerializationError",
    "RecentQuery",
    "RecentTag",
    "WindowGeometry",
    "canonicalize_query_tags",
    "decode_query_definition",
    "decode_result_summary",
    "encode_query_definition",
    "encode_result_summary",
    "query_definition_key",
    "query_definition_tags",
]
