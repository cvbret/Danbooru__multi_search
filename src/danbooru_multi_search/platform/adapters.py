"""Operating-system adapters that expose one complete service container."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from danbooru_multi_search.integrations.eagle import UnavailableEagleBridge
from danbooru_multi_search.platform.contracts import (
    NOT_IMPLEMENTED,
    UNSUPPORTED_PLATFORM,
    PlatformPaths,
    PlatformServices,
    ServiceAvailability,
)
from danbooru_multi_search.platform.paths import QtPlatformPaths
from danbooru_multi_search.platform.stubs import (
    UnavailableCredentialStore,
    UnavailableExternalOpenService,
    UnavailableMediaCapabilityService,
    UnavailableSystemInfoService,
    UnavailableWallpaperService,
)


@dataclass(frozen=True, slots=True)
class PlatformCapabilities:
    """Features intended for each operating system in later tasks."""

    os_name: str
    desktop_wallpaper: bool
    local_eagle: bool
    media_playback_detection: bool
    local_eagle_unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PlatformAdapter:
    capabilities: PlatformCapabilities
    services: PlatformServices


class WindowsPlatformAdapter(PlatformAdapter):
    def __init__(self, paths: PlatformPaths | None = None) -> None:
        super().__init__(
            capabilities=PlatformCapabilities(
                os_name="windows",
                desktop_wallpaper=True,
                local_eagle=True,
                media_playback_detection=True,
            ),
            services=_build_services(
                paths=paths or QtPlatformPaths(),
                eagle_supported=True,
                eagle_availability=_not_implemented(
                    "Eagle Web API integration is scheduled for task M0-005."
                ),
            ),
        )


class LinuxPlatformAdapter(PlatformAdapter):
    EAGLE_UNAVAILABLE_REASON = (
        "Eagle does not provide an official Linux desktop client."
    )

    def __init__(self, paths: PlatformPaths | None = None) -> None:
        super().__init__(
            capabilities=PlatformCapabilities(
                os_name="linux",
                desktop_wallpaper=True,
                local_eagle=False,
                media_playback_detection=True,
                local_eagle_unavailable_reason=self.EAGLE_UNAVAILABLE_REASON,
            ),
            services=_build_services(
                paths=paths or QtPlatformPaths(),
                eagle_supported=False,
                eagle_availability=ServiceAvailability.unavailable(
                    code=UNSUPPORTED_PLATFORM,
                    message=self.EAGLE_UNAVAILABLE_REASON,
                ),
            ),
        )


class UnsupportedPlatformAdapter(PlatformAdapter):
    def __init__(
        self,
        platform_name: str,
        paths: PlatformPaths | None = None,
    ) -> None:
        reason = f"Platform '{platform_name}' is not supported."
        unsupported = ServiceAvailability.unavailable(
            code=UNSUPPORTED_PLATFORM,
            message=reason,
        )
        super().__init__(
            capabilities=PlatformCapabilities(
                os_name=platform_name,
                desktop_wallpaper=False,
                local_eagle=False,
                media_playback_detection=False,
                local_eagle_unavailable_reason=reason,
            ),
            services=_build_services(
                paths=paths or QtPlatformPaths(),
                eagle_supported=False,
                eagle_availability=unsupported,
                default_availability=unsupported,
            ),
        )


def _not_implemented(message: str) -> ServiceAvailability:
    return ServiceAvailability.unavailable(code=NOT_IMPLEMENTED, message=message)


def _build_services(
    *,
    paths: PlatformPaths,
    eagle_supported: bool,
    eagle_availability: ServiceAvailability,
    default_availability: ServiceAvailability | None = None,
) -> PlatformServices:
    default = default_availability or _not_implemented(
        "This platform service is not implemented in task M0-001."
    )
    return PlatformServices(
        paths=paths,
        wallpaper=UnavailableWallpaperService(default),
        media=UnavailableMediaCapabilityService(default),
        eagle=UnavailableEagleBridge(
            availability=eagle_availability,
            is_supported=eagle_supported,
        ),
        external_open=UnavailableExternalOpenService(default),
        credentials=UnavailableCredentialStore(default),
        system_info=UnavailableSystemInfoService(default),
    )


def create_platform_adapter(
    platform_name: str | None = None,
    *,
    path_service: PlatformPaths | None = None,
    override_root: Path | None = None,
) -> PlatformAdapter:
    """Select an adapter using Python's stable platform identifier."""

    if path_service is not None and override_root is not None:
        raise ValueError("Provide either path_service or override_root, not both.")

    selected_paths = path_service or QtPlatformPaths(override_root=override_root)
    selected = platform_name or sys.platform
    if selected == "win32":
        return WindowsPlatformAdapter(selected_paths)
    if selected.startswith("linux"):
        return LinuxPlatformAdapter(selected_paths)
    return UnsupportedPlatformAdapter(selected, selected_paths)
