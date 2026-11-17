"""Favorite collection import/export use cases and immutable worker requests."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from danbooru_multi_search.core.favorites import (
    FavoriteCollectionSnapshot,
    FavoriteError,
    FavoriteFolderDraft,
    FavoriteItemDraft,
    candidate_from_favorite_item,
    favorite_folder_key,
)
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.favorites import FavoriteRepository
from danbooru_multi_search.storage.favorite_transfer import (
    FavoriteImportMode,
    FavoriteImportPreview,
    FavoriteTransferDocument,
    FavoriteTransferError,
    FavoriteTransferFolder,
    FavoriteTransferItem,
    decode_favorite_transfer_document,
    encode_favorite_transfer_document,
    read_favorite_transfer_bytes,
    read_favorite_transfer_document,
    write_favorite_transfer_bytes,
    write_favorite_transfer_document,
)


class TransferOperation(StrEnum):
    PREVIEW = "preview"
    EXPORT = "export"
    APPLY = "apply"


@dataclass(frozen=True, slots=True)
class TransferRequest:
    job_id: str
    operation: TransferOperation
    database_path: Path
    path: Path | None = None
    mode: FavoriteImportMode = FavoriteImportMode.MERGE
    preview: FavoriteImportPreview | None = None


@dataclass(frozen=True, slots=True)
class TransferResult:
    job_id: str
    operation: TransferOperation
    status: str
    preview: FavoriteImportPreview | None = None
    snapshot: FavoriteCollectionSnapshot | None = None
    exported_count: int = 0
    error_code: str = ""
    message: str = ""
    residual_path: Path | None = None


class FavoriteTransferService:
    """Coordinate validated documents, impact previews, and atomic imports."""

    def __init__(
        self,
        repository: FavoriteRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(repository, FavoriteRepository):
            raise TypeError("repository must be a FavoriteRepository")
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def create_export_document(self) -> FavoriteTransferDocument:
        """Capture and validate the complete collection in one read transaction."""

        try:
            snapshot = self._repository.capture_collection()
            return _document_from_snapshot(snapshot, self._clock())
        except FavoriteTransferError:
            raise
        except FavoriteError as exc:
            raise FavoriteTransferError(exc.code, str(exc)) from exc

    def export_file(self, path: Path | str) -> FavoriteTransferDocument:
        """Capture the collection and complete one atomic export file write."""

        document = self.create_export_document()
        write_favorite_transfer_document(path, document)
        return document

    def preview_file(
        self,
        path: Path | str,
        mode: FavoriteImportMode,
    ) -> FavoriteImportPreview:
        """Read a document and bind its preview to the observed collection."""

        return self.preview_import(read_favorite_transfer_document(path), mode)

    def preview_import(
        self,
        document: FavoriteTransferDocument,
        mode: FavoriteImportMode,
    ) -> FavoriteImportPreview:
        """Calculate one immutable impact preview without changing the collection."""

        if not isinstance(document, FavoriteTransferDocument):
            raise FavoriteTransferError("invalid_document", "收藏传输文档无效。")
        if not isinstance(mode, FavoriteImportMode):
            raise FavoriteTransferError("invalid_import_mode", "收藏导入方式无效。")
        try:
            snapshot = self._repository.capture_collection()
        except FavoriteError as exc:
            raise FavoriteTransferError(exc.code, str(exc)) from exc
        document_digest = _document_digest(document)
        if mode is FavoriteImportMode.MERGE:
            existing_folders = {
                favorite_folder_key(folder.name) for folder in snapshot.folders
            }
            existing_favorites = {item.identity for item in snapshot.favorites}
            added_folders = sum(
                favorite_folder_key(folder.name) not in existing_folders
                for folder in document.folders
            )
            reused_folders = len(document.folders) - added_folders
            added_favorites = sum(
                item.candidate.identity not in existing_favorites
                for item in document.favorites
            )
            preserved_favorites = len(document.favorites) - added_favorites
            deleted_folders = 0
            deleted_favorites = 0
        else:
            added_folders = len(document.folders)
            reused_folders = 0
            added_favorites = len(document.favorites)
            preserved_favorites = 0
            deleted_folders = len(snapshot.folders)
            deleted_favorites = len(snapshot.favorites)
        statistics = (
            added_folders,
            reused_folders,
            added_favorites,
            preserved_favorites,
            deleted_folders,
            deleted_favorites,
        )
        binding = _preview_binding(
            mode,
            document_digest,
            snapshot.revision,
            statistics,
        )
        return FavoriteImportPreview(
            mode=mode,
            document=document,
            document_digest=document_digest,
            collection_revision=snapshot.revision,
            added_folders=added_folders,
            reused_folders=reused_folders,
            added_favorites=added_favorites,
            preserved_favorites=preserved_favorites,
            deleted_folders=deleted_folders,
            deleted_favorites=deleted_favorites,
            binding_digest=binding,
        )

    def apply_import(
        self,
        preview: FavoriteImportPreview,
        *,
        before_apply: Callable[[], None] | None = None,
    ) -> FavoriteCollectionSnapshot:
        """Check cancellation before the repository's atomic, revision-bound import."""

        if not isinstance(preview, FavoriteImportPreview):
            raise FavoriteTransferError("invalid_preview", "收藏导入预览无效。")
        document_digest = _document_digest(preview.document)
        expected_binding = _preview_binding(
            preview.mode,
            document_digest,
            preview.collection_revision,
            (
                preview.added_folders,
                preview.reused_folders,
                preview.added_favorites,
                preview.preserved_favorites,
                preview.deleted_folders,
                preview.deleted_favorites,
            ),
        )
        folders = tuple(
            FavoriteFolderDraft(folder.name, folder.created_at, folder.updated_at)
            for folder in preview.document.folders
        )
        references = {
            folder.reference: index
            for index, folder in enumerate(preview.document.folders)
        }
        favorites = tuple(
            FavoriteItemDraft(
                item.candidate,
                None
                if item.folder_reference is None
                else references[item.folder_reference],
                item.favorited_at,
                item.updated_at,
            )
            for item in preview.document.favorites
        )
        try:
            if before_apply is not None:
                before_apply()
            return self._repository.apply_collection_import(
                expected_revision=preview.collection_revision,
                expected_document_digest=preview.document_digest,
                actual_document_digest=document_digest,
                expected_binding_digest=preview.binding_digest,
                actual_binding_digest=expected_binding,
                folders=folders,
                favorites=favorites,
                replace=preview.mode is FavoriteImportMode.REPLACE,
            )
        except FavoriteError as exc:
            raise FavoriteTransferError(exc.code, str(exc)) from exc


def _document_from_snapshot(
    snapshot: FavoriteCollectionSnapshot,
    exported_at: datetime,
) -> FavoriteTransferDocument:
    sorted_folders = sorted(
        snapshot.folders,
        key=lambda folder: (favorite_folder_key(folder.name), folder.id),
    )
    references = {
        folder.id: f"folder-{index:06d}"
        for index, folder in enumerate(sorted_folders, start=1)
    }
    folders = tuple(
        FavoriteTransferFolder(
            references[folder.id],
            folder.name,
            folder.created_at,
            folder.updated_at,
        )
        for folder in sorted_folders
    )
    sorted_favorites = sorted(
        snapshot.favorites,
        key=lambda item: (
            item.identity.object_type.value,
            item.identity.object_key,
        ),
    )
    favorites = tuple(
        FavoriteTransferItem(
            candidate_from_favorite_item(item),
            None if item.folder_id is None else references[item.folder_id],
            item.favorited_at,
            item.updated_at,
        )
        for item in sorted_favorites
    )
    try:
        return FavoriteTransferDocument(exported_at, folders, favorites)
    except (FavoriteError, KeyError) as exc:
        raise FavoriteTransferError(
            "invalid_collection",
            "本地收藏数据无法建立传输文档。",
        ) from exc


class _Cancelled(Exception):
    pass


def execute_transfer(
    request: TransferRequest,
    cancelled: Callable[[], bool],
) -> TransferResult:
    """Run file/database steps in order with an independent worker repository.

    Check cancellation between steps and immediately before the import transaction.
    Once an atomic file write or import transaction starts, finish that step.
    """

    def checkpoint() -> None:
        if cancelled():
            raise _Cancelled()

    try:
        checkpoint()
        service = FavoriteTransferService(
            FavoriteRepository(Database(request.database_path))
        )
        if request.operation is TransferOperation.PREVIEW:
            data = read_favorite_transfer_bytes(request.path)
            checkpoint()
            document = decode_favorite_transfer_document(data)
            checkpoint()
            preview = service.preview_import(document, request.mode)
            checkpoint()
            return TransferResult(
                request.job_id,
                request.operation,
                "success",
                preview=preview,
            )
        if request.operation is TransferOperation.EXPORT:
            document = service.create_export_document()
            checkpoint()
            encoded = encode_favorite_transfer_document(document)
            checkpoint()
            write_favorite_transfer_bytes(request.path, encoded)
            return TransferResult(
                request.job_id,
                request.operation,
                "success",
                exported_count=len(document.favorites),
            )
        if request.operation is TransferOperation.APPLY:
            if request.preview is None:
                raise FavoriteTransferError("invalid_preview", "收藏导入预览无效。")
            snapshot = service.apply_import(
                request.preview,
                before_apply=checkpoint,
            )
            return TransferResult(
                request.job_id,
                request.operation,
                "success",
                snapshot=snapshot,
            )
        raise FavoriteTransferError("invalid_operation", "收藏传输操作无效。")
    except _Cancelled:
        return TransferResult(request.job_id, request.operation, "cancelled")
    except FavoriteTransferError as exc:
        return TransferResult(
            request.job_id,
            request.operation,
            "error",
            error_code=exc.code,
            message=str(exc),
            residual_path=exc.residual_path,
        )
    except Exception as exc:
        return TransferResult(
            request.job_id,
            request.operation,
            "error",
            error_code="transfer_worker_error",
            message=f"收藏后台传输失败：{exc}",
        )


def _document_digest(document: FavoriteTransferDocument) -> str:
    return hashlib.sha256(encode_favorite_transfer_document(document)).hexdigest()


def _preview_binding(
    mode: FavoriteImportMode,
    document_digest: str,
    revision: int,
    statistics: tuple[int, int, int, int, int, int],
) -> str:
    if not isinstance(mode, FavoriteImportMode):
        raise FavoriteTransferError("invalid_import_mode", "收藏导入方式无效。")
    if (
        not isinstance(statistics, tuple)
        or len(statistics) != 6
        or any(type(value) is not int or value < 0 for value in statistics)
    ):
        raise FavoriteTransferError("invalid_preview", "收藏导入预览统计无效。")
    counts = ",".join(str(value) for value in statistics)
    payload = f"{mode.value}\0{document_digest}\0{revision}\0{counts}".encode("ascii")
    return hashlib.sha256(payload).hexdigest()
