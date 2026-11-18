"""Pure query event values shared by controllers and application services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from danbooru_multi_search.danbooru.query_definition import GalleryQueryDefinition
from danbooru_multi_search.danbooru.query_types import GalleryResultSummary


class GalleryQueryOrigin(StrEnum):
    STANDARD = "standard"
    RANK = "rank"
    LATEST = "latest"
    HISTORY = "history"
    REFRESH = "refresh"
    CHECKPOINT = "checkpoint"


@dataclass(frozen=True, slots=True)
class GalleryQueryAccepted:
    operation_id: int
    execution_id: int
    definition: GalleryQueryDefinition
    display_text: str
    origin: GalleryQueryOrigin
    query_tags: tuple[str, ...]
    accepted_at: datetime
    count_tag_usage: bool


@dataclass(frozen=True, slots=True)
class GalleryViewDefinitionChanged:
    operation_id: int
    execution_id: int
    view_generation: int
    definition: GalleryQueryDefinition
    applied_to_gallery: bool


@dataclass(frozen=True, slots=True)
class GalleryResultEvent:
    operation_id: int
    execution_id: int
    view_generation: int
    definition: GalleryQueryDefinition
    summary: GalleryResultSummary
    gallery_operation_id: int | None
