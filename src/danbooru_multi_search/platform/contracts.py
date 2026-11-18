"""Platform-neutral service contracts and result types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from danbooru_multi_search.core.results import (
    OperationResult,
    ResultValue,
    ServiceAvailability,
    ValueResult,
)
from danbooru_multi_search.integrations.eagle.contracts import EagleBridge


NOT_IMPLEMENTED = "not_implemented"
UNSUPPORTED_PLATFORM = "unsupported_platform"


@dataclass(frozen=True, slots=True)
class ApplicationPaths:
    config_dir: Path
    data_dir: Path
    cache_dir: Path
    log_dir: Path
    temp_dir: Path
    download_dir: Path
    settings_file: Path
    database_file: Path

    def ensure_directories(self) -> None:
        """Create only application-owned directories."""

        for directory in (
            self.config_dir,
            self.data_dir,
            self.cache_dir,
            self.log_dir,
            self.temp_dir,
            self.download_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True, slots=True)
class SystemInfo:
    """Operating-system information returned by a future platform service."""

    os_name: str
    architecture: str


class WallpaperStyle(StrEnum):
    FILL = "fill"
    FIT = "fit"
    STRETCH = "stretch"
    TILE = "tile"
    CENTER = "center"


@dataclass(frozen=True, slots=True)
class WallpaperSnapshot:
    path: Path | None
    style: WallpaperStyle | None
    wallpaper_style_value: str | None
    tile_wallpaper_value: str | None


@runtime_checkable
class PlatformPaths(Protocol):
    """Resolve application-owned paths for the current operating system."""

    def resolve(self) -> ApplicationPaths:
        """Return configuration, data, cache, log, temporary and download paths."""

        ...


@runtime_checkable
class WallpaperService(Protocol):
    @property
    def availability(self) -> ServiceAvailability: ...

    def get_current(self) -> ValueResult[WallpaperSnapshot]: ...

    def set_wallpaper(
        self,
        image_path: Path,
        style: WallpaperStyle | None = None,
    ) -> OperationResult: ...

    def restore_previous(self) -> OperationResult: ...


@runtime_checkable
class MediaCapabilityService(Protocol):
    @property
    def availability(self) -> ServiceAvailability: ...

    def can_play(self, media_format: str) -> ValueResult[bool]: ...


@runtime_checkable
class ExternalOpenService(Protocol):
    @property
    def availability(self) -> ServiceAvailability: ...

    def open_file(self, file_path: Path) -> OperationResult: ...

    def open_directory(self, directory_path: Path) -> OperationResult: ...

    def open_url(self, url: str) -> OperationResult: ...


@runtime_checkable
class CredentialStore(Protocol):
    @property
    def availability(self) -> ServiceAvailability: ...

    def read(self, key: str) -> ValueResult[str]: ...

    def write(self, key: str, value: str) -> OperationResult: ...

    def delete(self, key: str) -> OperationResult: ...


@runtime_checkable
class SystemInfoService(Protocol):
    @property
    def availability(self) -> ServiceAvailability: ...

    def get_info(self) -> ValueResult[SystemInfo]: ...


@dataclass(frozen=True, slots=True)
class PlatformServices:
    """All operating-system services exposed to platform-neutral modules."""

    paths: PlatformPaths
    wallpaper: WallpaperService
    media: MediaCapabilityService
    eagle: EagleBridge
    external_open: ExternalOpenService
    credentials: CredentialStore
    system_info: SystemInfoService
