"""Eagle integration contracts, local Web API client and platform stub."""

from danbooru_multi_search.integrations.eagle.contracts import (
    EagleBridge,
    EagleConnectionInfo,
    EagleFolderInfo,
    EagleImportResult,
    EagleLibraryInfo,
)
from danbooru_multi_search.integrations.eagle.client import (
    DEFAULT_EAGLE_BASE_URL,
    EagleWebApiBridge,
    library_identifier_from_windows_path,
    normalize_windows_library_path,
)
from danbooru_multi_search.integrations.eagle.stubs import UnavailableEagleBridge
from danbooru_multi_search.integrations.eagle.transport import (
    EagleConnectionError,
    EagleHttpRequest,
    EagleHttpResponse,
    EagleProtocolError,
    EagleResponseTooLargeError,
    EagleTimeoutError,
    EagleTransportError,
    UrllibEagleHttpTransport,
)

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

