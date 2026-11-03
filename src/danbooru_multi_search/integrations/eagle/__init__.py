"""Eagle integration contracts and the M0-001 unavailable implementation."""

from danbooru_multi_search.integrations.eagle.contracts import (
    EagleBridge,
    EagleConnectionInfo,
    EagleImportResult,
    EagleLibraryInfo,
)
from danbooru_multi_search.integrations.eagle.stubs import UnavailableEagleBridge

__all__ = [
    "EagleBridge",
    "EagleConnectionInfo",
    "EagleImportResult",
    "EagleLibraryInfo",
    "UnavailableEagleBridge",
]

