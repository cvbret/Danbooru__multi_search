"""Side-effect-free platform service implementations used during M0-001."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from danbooru_multi_search.platform.contracts import (
    OperationResult,
    ServiceAvailability,
    SystemInfo,
    ValueResult,
)


UnavailableValue = TypeVar("UnavailableValue")


@dataclass(frozen=True, slots=True)
class UnavailableWallpaperService:
    availability: ServiceAvailability

    def set_wallpaper(self, image_path: Path) -> OperationResult:
        return _operation_unavailable(self.availability)

    def restore_previous(self) -> OperationResult:
        return _operation_unavailable(self.availability)


@dataclass(frozen=True, slots=True)
class UnavailableMediaCapabilityService:
    availability: ServiceAvailability

    def can_play(self, media_format: str) -> ValueResult[bool]:
        return _value_unavailable(self.availability)


@dataclass(frozen=True, slots=True)
class UnavailableExternalOpenService:
    availability: ServiceAvailability

    def open_file(self, file_path: Path) -> OperationResult:
        return _operation_unavailable(self.availability)

    def open_directory(self, directory_path: Path) -> OperationResult:
        return _operation_unavailable(self.availability)

    def open_url(self, url: str) -> OperationResult:
        return _operation_unavailable(self.availability)


@dataclass(frozen=True, slots=True)
class UnavailableCredentialStore:
    availability: ServiceAvailability

    def read(self, key: str) -> ValueResult[str]:
        return _value_unavailable(self.availability)

    def write(self, key: str, value: str) -> OperationResult:
        return _operation_unavailable(self.availability)

    def delete(self, key: str) -> OperationResult:
        return _operation_unavailable(self.availability)


@dataclass(frozen=True, slots=True)
class UnavailableSystemInfoService:
    availability: ServiceAvailability

    def get_info(self) -> ValueResult[SystemInfo]:
        return _value_unavailable(self.availability)


def _operation_unavailable(availability: ServiceAvailability) -> OperationResult:
    return OperationResult.unavailable(
        code=availability.code or "unavailable",
        message=availability.message or "Platform service is unavailable.",
    )


def _value_unavailable(
    availability: ServiceAvailability,
) -> ValueResult[UnavailableValue]:
    return ValueResult.unavailable(
        code=availability.code or "unavailable",
        message=availability.message or "Platform service is unavailable.",
    )
