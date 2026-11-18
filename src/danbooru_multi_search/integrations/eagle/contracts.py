"""Single platform-neutral contract for Eagle integration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, runtime_checkable

from danbooru_multi_search.core.results import ServiceAvailability, ValueResult


@dataclass(frozen=True, slots=True)
class EagleLibraryInfo:
    identifier: str
    name: str
    path: Path


@dataclass(frozen=True, slots=True)
class EagleConnectionInfo:
    version: str
    prerelease_version: str | None = None
    build_version: str | None = None
    platform: str | None = None
    build_number: int | None = None


@dataclass(frozen=True, slots=True)
class EagleFolderInfo:
    identifier: str
    name: str
    parent_identifier: str | None = None


@dataclass(frozen=True, slots=True)
class EagleImportResult:
    item_identifier: str


@dataclass(frozen=True, slots=True)
class EagleItemInfo:
    identifier: str
    name: str
    url: str
    tags: tuple[str, ...]
    folders: tuple[str, ...]
    annotation: str
    is_deleted: bool


@dataclass(frozen=True, slots=True)
class EagleItemPage:
    items: tuple[EagleItemInfo, ...]
    total: int
    offset: int
    limit: int


@runtime_checkable
class EagleBridge(Protocol):
    """Detect Eagle, read its current library and import a local file."""

    @property
    def availability(self) -> ServiceAvailability:
        """Return whether this implementation can currently perform requests."""

        ...

    @property
    def is_supported(self) -> bool:
        """Return whether Eagle integration is supported on this platform."""

        ...

    @property
    def unavailable_reason(self) -> str | None:
        """Return a user-facing reason when integration is unavailable."""

        ...

    def test_connection(self) -> ValueResult[EagleConnectionInfo]:
        """Detect a running Eagle Web API and read its version."""

        ...

    def get_current_library(self) -> ValueResult[EagleLibraryInfo]:
        """Read information about the library currently open in Eagle."""

        ...

    def list_folders(
        self, *, maximum_pages: int = 10001, maximum_folders: int | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ValueResult[tuple[EagleFolderInfo, ...]]:
        """Return all current-library folders, including nested children."""

        ...

    def create_folder(self, name: str, *, parent_identifier: str | None = None) -> ValueResult[EagleFolderInfo]:
        """Create one explicitly requested folder in the currently open library."""

        ...

    def get_items(
        self,
        *,
        item_identifier: str | None = None,
        url: str | None = None,
        annotation: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> ValueResult[EagleItemPage]:
        """Read one bounded page for an explicit identity, URL or annotation filter."""
        ...

    def add_item_from_file(
        self,
        file_path: Path,
        *,
        folder_identifier: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ValueResult[EagleImportResult]:
        """Send one validated local path after application target checks."""
        ...

    def update_item_metadata(
        self,
        item_identifier: str,
        *,
        tags: tuple[str, ...],
        annotation: str,
    ) -> ValueResult[EagleItemInfo]:
        """Update only tags and annotation, leaving the file and other metadata alone."""
        ...

    def import_file(
        self,
        file_path: Path,
        *,
        folder_identifier: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ValueResult[EagleImportResult]:
        """Import one checked local file through the Eagle Web API."""

        ...

