"""Pure data models returned by the Danbooru API boundary."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DanbooruPost:
    id: int
    preview_url: str | None
    large_file_url: str | None
    file_url: str | None
    score: int | None
    fav_count: int | None
    rating: str
    width: int
    height: int
    file_ext: str
    created_at: str | None = None
    file_size: int | None = None
    source: str | None = None
    artist_tags: tuple[str, ...] = ()
    character_tags: tuple[str, ...] = ()
    copyright_tags: tuple[str, ...] = ()
    general_tags: tuple[str, ...] = ()
    meta_tags: tuple[str, ...] = ()

    @property
    def display_url(self) -> str:
        """Choose a useful image URL without changing the parsed API fields."""

        return self.preview_url or self.large_file_url or self.file_url or ""

    @property
    def download_url(self) -> str:
        """Return only the original media URL matching ``file_ext``."""

        return self.file_url or ""

    @property
    def media_kind(self) -> str:
        """Classify media for compact gallery badges."""

        extension = self.file_ext.strip().casefold()
        if extension in {"jpg", "jpeg", "png", "webp", "avif"}:
            return "image"
        if extension == "gif":
            return "gif"
        if extension in {"mp4", "webm"}:
            return "video"
        if extension == "zip":
            return "ugoira"
        if extension == "swf":
            return "flash"
        return "unknown"

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)
