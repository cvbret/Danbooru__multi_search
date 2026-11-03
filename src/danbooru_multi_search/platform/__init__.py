"""Operating-system service contracts and adapters."""

from danbooru_multi_search.platform.adapters import (
    LinuxPlatformAdapter,
    PlatformAdapter,
    PlatformCapabilities,
    UnsupportedPlatformAdapter,
    WindowsPlatformAdapter,
    create_platform_adapter,
)
from danbooru_multi_search.platform.contracts import (
    NOT_IMPLEMENTED,
    UNSUPPORTED_PLATFORM,
    CredentialStore,
    ExternalOpenService,
    MediaCapabilityService,
    OperationResult,
    PlatformPaths,
    PlatformServices,
    ServiceAvailability,
    SystemInfo,
    SystemInfoService,
    ValueResult,
    WallpaperService,
)
from danbooru_multi_search.platform.paths import (
    ApplicationPaths,
    QtPlatformPaths,
    resolve_application_paths,
)

__all__ = [
    "ApplicationPaths",
    "CredentialStore",
    "ExternalOpenService",
    "LinuxPlatformAdapter",
    "MediaCapabilityService",
    "NOT_IMPLEMENTED",
    "OperationResult",
    "PlatformAdapter",
    "PlatformCapabilities",
    "PlatformPaths",
    "PlatformServices",
    "QtPlatformPaths",
    "ServiceAvailability",
    "SystemInfo",
    "SystemInfoService",
    "UNSUPPORTED_PLATFORM",
    "UnsupportedPlatformAdapter",
    "ValueResult",
    "WallpaperService",
    "WindowsPlatformAdapter",
    "create_platform_adapter",
    "resolve_application_paths",
]

