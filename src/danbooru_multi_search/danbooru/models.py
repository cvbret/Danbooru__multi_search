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
    score: int
    fav_count: int
    rating: str
    width: int
    height: int
    file_ext: str

    @property
    def display_url(self) -> str:
        """Choose a useful image URL without changing the parsed API fields."""

        return self.preview_url or self.large_file_url or self.file_url or ""

    @property
    def download_url(self) -> str:
        """Return only the original media URL matching ``file_ext``."""

        return self.file_url or ""

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)
