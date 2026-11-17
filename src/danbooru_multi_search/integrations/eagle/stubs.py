"""Side-effect-free Eagle bridge for unsupported platforms."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, TypeVar

from danbooru_multi_search.integrations.eagle.contracts import (
    EagleConnectionInfo,
    EagleFolderInfo,
    EagleImportResult,
    EagleLibraryInfo,
)
from danbooru_multi_search.platform.contracts import (
    ServiceAvailability,
    ValueResult,
)


EagleResultValue = TypeVar("EagleResultValue")


@dataclass(frozen=True, slots=True)
class UnavailableEagleBridge:
    """Report an explicit state without contacting Eagle or touching files."""

    availability: ServiceAvailability
    is_supported: bool

    @property
    def unavailable_reason(self) -> str | None:
        return self.availability.message

    def test_connection(self) -> ValueResult[EagleConnectionInfo]:
        return self._unavailable_result()

    def get_current_library(self) -> ValueResult[EagleLibraryInfo]:
        return self._unavailable_result()

    def list_folders(
        self, *, maximum_pages: int = 10001, maximum_folders: int | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ValueResult[tuple[EagleFolderInfo, ...]]:
        return self._unavailable_result()

    def create_folder(self, name: str, *, parent_identifier: str | None = None) -> ValueResult[EagleFolderInfo]:
        return self._unavailable_result()

    def import_file(
        self,
        file_path: Path,
        *,
        folder_identifier: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ValueResult[EagleImportResult]:
        return self._unavailable_result()

    def _unavailable_result(self) -> ValueResult[EagleResultValue]:
        return ValueResult.unavailable(
            code=self.availability.code or "unavailable",
            message=self.availability.message or "Eagle integration is unavailable.",
        )
