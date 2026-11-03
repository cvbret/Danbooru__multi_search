"""Platform-neutral service contracts and result types."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from danbooru_multi_search.integrations.eagle.contracts import EagleBridge
    from danbooru_multi_search.platform.paths import ApplicationPaths


NOT_IMPLEMENTED = "not_implemented"
UNSUPPORTED_PLATFORM = "unsupported_platform"


@dataclass(frozen=True, slots=True)
class ServiceAvailability:
    """Describe whether a platform service can currently perform operations."""

    available: bool
    code: str | None = None
    message: str | None = None

    @classmethod
    def unavailable(cls, code: str, message: str) -> ServiceAvailability:
        return cls(available=False, code=code, message=message)


@dataclass(frozen=True, slots=True)
class OperationResult:
    """Result for a platform operation that does not return a value."""

    succeeded: bool
    code: str | None = None
    message: str | None = None

    @classmethod
    def unavailable(cls, code: str, message: str) -> OperationResult:
        return cls(succeeded=False, code=code, message=message)


ResultValue = TypeVar("ResultValue")


@dataclass(frozen=True, slots=True)
class ValueResult(Generic[ResultValue]):
    """Result for a platform operation that may return a typed value."""

    succeeded: bool
    value: ResultValue | None = None
    code: str | None = None
    message: str | None = None

    @classmethod
    def unavailable(cls, code: str, message: str) -> ValueResult[ResultValue]:
        return cls(succeeded=False, code=code, message=message)


@dataclass(frozen=True, slots=True)
class SystemInfo:
    """Operating-system information returned by a future platform service."""

    os_name: str
    architecture: str


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

    def set_wallpaper(self, image_path: Path) -> OperationResult: ...

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
