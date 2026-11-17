"""Eagle public contracts and implementations, loaded only when requested."""

from importlib import import_module
from typing import Any


_EXPORT_GROUPS = {
    "danbooru_multi_search.integrations.eagle.contracts": (
        "EagleBridge", "EagleConnectionInfo", "EagleFolderInfo",
        "EagleImportResult", "EagleLibraryInfo",
    ),
    "danbooru_multi_search.integrations.eagle.client": (
        "DEFAULT_EAGLE_BASE_URL", "EagleWebApiBridge",
        "library_identifier_from_windows_path", "normalize_windows_library_path",
    ),
    "danbooru_multi_search.integrations.eagle.stubs": ("UnavailableEagleBridge",),
    "danbooru_multi_search.integrations.eagle.transport": (
        "EagleConnectionError", "EagleHttpRequest", "EagleHttpResponse",
        "EagleProtocolError", "EagleResponseTooLargeError", "EagleTimeoutError",
        "EagleTransportError", "UrllibEagleHttpTransport",
    ),
}
_EXPORTS = {
    name: module for module, names in _EXPORT_GROUPS.items() for name in names
}

__all__ = [
    "DEFAULT_EAGLE_BASE_URL",
    "EagleBridge",
    "EagleConnectionInfo",
    "EagleConnectionError",
    "EagleFolderInfo",
    "EagleHttpRequest",
    "EagleHttpResponse",
    "EagleImportResult",
    "EagleLibraryInfo",
    "EagleProtocolError",
    "EagleResponseTooLargeError",
    "EagleTimeoutError",
    "EagleTransportError",
    "EagleWebApiBridge",
    "UnavailableEagleBridge",
    "UrllibEagleHttpTransport",
    "library_identifier_from_windows_path",
    "normalize_windows_library_path",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

