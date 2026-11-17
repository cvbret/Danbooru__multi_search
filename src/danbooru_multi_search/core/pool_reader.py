"""Reader modes and bounded page ranges, independent of Qt and persistence."""

from enum import StrEnum

from danbooru_multi_search.core.pools import PoolError

READER_METADATA_RADIUS = 2
READER_MEDIA_RADIUS = 1
READER_CACHE_PAGES = 120
READER_DIRECTORY_SIZE = 5


class ReaderMode(StrEnum):
    CONTINUOUS = "continuous"
    SINGLE = "single"


def reader_range(page: int, count: int, radius: int = READER_METADATA_RADIUS) -> tuple[int, ...]:
    """Return only neighbouring original positions, never reordered post IDs."""
    if type(page) is not int or type(count) is not int or not 1 <= page <= count:
        raise PoolError("invalid_page", "阅读页码超出作品池范围。")
    return tuple(range(max(1, page - radius), min(count, page + radius) + 1))
