"""Immutable transfer work and cancellation between file/database steps."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from danbooru_multi_search.core.favorites import FavoriteCollectionSnapshot
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.favorites import FavoriteRepository
from danbooru_multi_search.storage.favorite_transfer import (
    FavoriteImportMode, FavoriteImportPreview, FavoriteTransferError,
    FavoriteTransferService, _read_transfer_bytes, _write_transfer_bytes,
    decode_favorite_transfer_document, encode_favorite_transfer_document,
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


class _Cancelled(Exception):
    pass


def execute_transfer(
    request: TransferRequest, cancelled: Callable[[], bool]
) -> TransferResult:
    """Run without QObjects, using a repository owned solely by this work."""

    def checkpoint():
        if cancelled():
            raise _Cancelled()

    try:
        checkpoint()
        service = FavoriteTransferService(FavoriteRepository(Database(request.database_path)))
        if request.operation is TransferOperation.PREVIEW:
            data = _read_transfer_bytes(request.path)
            checkpoint()
            document = decode_favorite_transfer_document(data)
            checkpoint()
            preview = service.preview_import(document, request.mode)
            checkpoint()
            return TransferResult(request.job_id, request.operation, "success", preview=preview)
        if request.operation is TransferOperation.EXPORT:
            document = service.create_export_document()
            checkpoint()
            encoded = encode_favorite_transfer_document(document)
            checkpoint()
            # Once entered, fsync/replace/cleanup complete as one atomic step.
            _write_transfer_bytes(request.path, encoded)
            return TransferResult(request.job_id, request.operation, "success", exported_count=len(document.favorites))
        if request.operation is TransferOperation.APPLY:
            if request.preview is None:
                raise FavoriteTransferError("invalid_preview", "收藏导入预览无效。")
            snapshot = service.apply_import(request.preview, before_apply=checkpoint)
            return TransferResult(request.job_id, request.operation, "success", snapshot=snapshot)
        raise FavoriteTransferError("invalid_operation", "收藏传输操作无效。")
    except _Cancelled:
        return TransferResult(request.job_id, request.operation, "cancelled")
    except FavoriteTransferError as exc:
        return TransferResult(request.job_id, request.operation, "error", error_code=exc.code,
                              message=str(exc), residual_path=exc.residual_path)
    except Exception as exc:
        return TransferResult(request.job_id, request.operation, "error", error_code="transfer_worker_error",
                              message=f"收藏后台传输失败：{exc}")
