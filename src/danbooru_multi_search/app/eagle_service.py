"""Eagle connection and folder use cases, independent of Qt and the UI."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import ContextManager, TypeAlias

from danbooru_multi_search.integrations.eagle.contracts import (
    EagleBridge,
    EagleConnectionInfo,
    EagleFolderInfo,
    EagleLibraryInfo,
)


class EagleOperation(StrEnum):
    REFRESH = "refresh"
    CREATE_FOLDER = "create_folder"


@dataclass(frozen=True, slots=True)
class EagleSnapshot:
    connection: EagleConnectionInfo
    library: EagleLibraryInfo
    folders: tuple[EagleFolderInfo, ...]


@dataclass(frozen=True, slots=True)
class EagleRequest:
    request_id: str
    operation: EagleOperation
    library_identifier: str | None = None
    name: str = ""
    parent_identifier: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id:
            raise ValueError("Eagle request identifier is required.")
        if not isinstance(self.operation, EagleOperation):
            raise ValueError("Eagle operation must be an EagleOperation.")
        if self.operation is EagleOperation.CREATE_FOLDER:
            if not isinstance(self.library_identifier, str) or not self.library_identifier:
                raise ValueError("Folder creation requires a captured library identifier.")
            if not isinstance(self.name, str) or not self.name.strip():
                raise ValueError("Folder name must not be empty.")
        if self.parent_identifier is not None:
            if not isinstance(self.parent_identifier, str) or not self.parent_identifier.strip():
                raise ValueError("Parent folder identifier is invalid.")


@dataclass(frozen=True, slots=True)
class EagleResult:
    request_id: str
    snapshot: EagleSnapshot | None = None
    error_code: str = ""
    diagnostic: str = ""
    post_started: bool = False
    uncertain: bool = False
    created_identifier: str | None = None


@dataclass(frozen=True, slots=True)
class EagleBackend:
    bridge: EagleBridge
    post_sent: Callable[[], bool]


EagleBackendFactory: TypeAlias = Callable[
    [Callable[[], bool], float], ContextManager[EagleBackend]
]


class _UseCaseError(Exception):
    def __init__(self, code: str, diagnostic: str) -> None:
        self.code = code
        self.diagnostic = diagnostic


def execute_eagle_request(
    request: EagleRequest,
    backend: EagleBackend,
    cancelled: Callable[[], bool],
) -> EagleResult:
    """Read a consistent observed library; send at most one folder creation.

    Pre/post library reads detect observed changes. The server has no atomic
    expected-library condition, so they do not establish server-side isolation.
    """
    bridge = backend.bridge
    created_identifier = None

    def check() -> None:
        if cancelled():
            raise _UseCaseError("eagle_cancelled", "Operation cancelled.")

    def take(call):
        check()
        result = call()
        check()
        if not result.succeeded or result.value is None:
            raise _UseCaseError(result.code or "eagle_error", result.message or "Eagle request failed.")
        return result.value

    def folders() -> tuple[EagleFolderInfo, ...]:
        return take(lambda: bridge.list_folders(
            maximum_pages=20,
            maximum_folders=20000,
            cancelled=cancelled,
        ))

    def same_library(expected: EagleLibraryInfo) -> EagleLibraryInfo:
        current = take(bridge.get_current_library)
        if current.identifier != expected.identifier:
            raise _UseCaseError("eagle_library_changed", "The current Eagle library changed.")
        return current

    try:
        connection = take(bridge.test_connection)
        library = take(bridge.get_current_library)
        if request.operation is EagleOperation.CREATE_FOLDER:
            if library.identifier != request.library_identifier:
                raise _UseCaseError("eagle_library_changed", "The captured Eagle library is no longer current.")
        current_folders = folders()
        same_library(library)
        if request.operation is EagleOperation.CREATE_FOLDER:
            identifiers = {folder.identifier for folder in current_folders}
            if request.parent_identifier is not None and request.parent_identifier not in identifiers:
                raise _UseCaseError("eagle_folder_not_found", "The parent folder no longer exists.")
            check()
            creation = bridge.create_folder(request.name, parent_identifier=request.parent_identifier)
            if creation.succeeded and isinstance(creation.value, EagleFolderInfo):
                created_identifier = creation.value.identifier
            check()
            if not creation.succeeded or created_identifier is None:
                raise _UseCaseError(creation.code or "eagle_invalid_folder", creation.message or "Invalid created folder.")
            current_folders = folders()
        after = same_library(library)
        if created_identifier and created_identifier not in {folder.identifier for folder in current_folders}:
            raise _UseCaseError("eagle_folder_not_found", "Created folder was not present after refreshing.")
        return EagleResult(
            request.request_id,
            snapshot=EagleSnapshot(connection, after, current_folders),
            post_started=backend.post_sent(),
            created_identifier=created_identifier,
        )
    except _UseCaseError as exc:
        started = backend.post_sent()
        return EagleResult(
            request.request_id,
            error_code=exc.code,
            diagnostic=exc.diagnostic,
            post_started=started,
            uncertain=started,
            created_identifier=created_identifier,
        )
