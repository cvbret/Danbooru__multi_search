"""Validated reading positions and deterministic recovery without Qt or storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from urllib.parse import urlsplit, urlunsplit

from danbooru_multi_search.core.pool_reader import ReaderMode
from danbooru_multi_search.core.pools import DanbooruPool, PoolCategory, MAX_POOL_POSTS

MAX_READING_OFFSET = 10_000_000
MAX_READING_WIDTH = 100_000


class PoolProgressError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_api_source(value: str) -> str:
    """Keep the API endpoint identity, excluding user info, query and fragment."""
    try:
        if not isinstance(value, str):
            raise ValueError("API 地址必须是文本。")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("API 地址必须是 HTTP 或 HTTPS 地址。")
        host = parsed.hostname.lower().encode("idna").decode("ascii")
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            host += f":{port}"
        return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))
    except (ValueError, UnicodeError) as exc:
        raise PoolProgressError("invalid_source", f"阅读进度来源无效：{exc}") from exc


def _integer(value: int, maximum: int, label: str, *, minimum: int = 1) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise PoolProgressError("invalid_progress", f"{label}无效。")


def _number(value: float, maximum: float, label: str, *, minimum: float = 0) -> None:
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not minimum <= value <= maximum or not math.isfinite(value)):
        raise PoolProgressError("invalid_progress", f"{label}超出允许范围。")


@dataclass(frozen=True, slots=True)
class PoolReadingProgress:
    source: str
    pool_id: int
    post_id: int
    page_number: int
    updated_at: datetime
    offset: float = 0
    viewport_width: float = 0
    viewport_height: float = 0
    zoom: float = 1
    mode: ReaderMode = ReaderMode.CONTINUOUS
    scale_mode: str = "fit-width"
    revision: int = 0

    def __post_init__(self) -> None:
        if normalize_api_source(self.source) != self.source:
            raise PoolProgressError("invalid_source", "阅读进度来源必须经过规范化。")
        _integer(self.pool_id, 2**63 - 1, "作品池编号")
        _integer(self.post_id, 2**63 - 1, "作品编号")
        _integer(self.page_number, MAX_POOL_POSTS, "原始页码")
        _integer(self.revision, 2**63 - 1, "进度修订号", minimum=0)
        _number(self.offset, MAX_READING_OFFSET, "页内偏移")
        _number(self.viewport_width, MAX_READING_WIDTH, "阅读区域宽度")
        _number(self.viewport_height, MAX_READING_WIDTH, "阅读区域高度")
        _number(self.zoom, 8, "缩放比例", minimum=0.05)
        if (not isinstance(self.mode, ReaderMode) or not isinstance(self.scale_mode, str)
                or self.scale_mode not in {"fit-width", "fit-window", "manual"}):
            raise PoolProgressError("invalid_progress", "阅读布局依据无效。")
        try:
            if not isinstance(self.updated_at, datetime) or self.updated_at.utcoffset() is None:
                raise ValueError("更新时间必须包含时区。")
            object.__setattr__(self, "updated_at", self.updated_at.astimezone(timezone.utc))
        except (ValueError, OverflowError) as exc:
            raise PoolProgressError("invalid_progress", f"阅读进度更新时间无效：{exc}") from exc


@dataclass(frozen=True, slots=True)
class ResolvedPoolPosition:
    page_number: int
    offset: float


def resolve_pool_progress(
    progress: PoolReadingProgress, pool: DanbooruPool, *, viewport_width: float,
    zoom: float, mode: ReaderMode, scale_mode: str, viewport_height: float = 0,
) -> ResolvedPoolPosition | None:
    """Prefer the original occurrence, then nearest same ID, then clamped page."""
    if pool.id != progress.pool_id:
        raise PoolProgressError("invalid_progress", "阅读进度与作品池不一致。")
    if not pool.post_ids or pool.category is not PoolCategory.SERIES:
        return None
    original = pool.page_at(progress.page_number)
    if original and original.post_id == progress.post_id:
        page = progress.page_number
    else:
        matches = tuple(index for index, identifier in enumerate(pool.post_ids, 1) if identifier == progress.post_id)
        page = min(matches, key=lambda index: (abs(index - progress.page_number), index)) if matches else min(progress.page_number, pool.post_count)
    same_post = pool.post_ids[page - 1] == progress.post_id
    same_layout = (
        progress.mode is mode is ReaderMode.CONTINUOUS
        and progress.scale_mode == scale_mode
        and progress.viewport_width > 0 and viewport_width > 0
        and math.isclose(progress.viewport_width, viewport_width, abs_tol=0.5)
        and math.isclose(progress.zoom, zoom, abs_tol=1e-6)
        and (scale_mode != "fit-window" or (progress.viewport_height > 0 and viewport_height > 0
             and math.isclose(progress.viewport_height, viewport_height, abs_tol=0.5)))
    )
    return ResolvedPoolPosition(page, float(progress.offset) if same_post and same_layout else 0.0)
