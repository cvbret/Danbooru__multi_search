"""Strict versioned import and export for local favorite collections."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from danbooru_multi_search.core.favorites import (
    FavoriteCandidate,
    FavoriteError,
    FavoriteIdentity,
    FavoriteObjectType,
    favorite_folder_key,
)
from danbooru_multi_search.storage.favorite_codec import (
    FAVORITE_SUMMARY_VERSION,
    decode_favorite_summary,
    decode_favorite_timestamp,
    encode_favorite_summary,
    encode_favorite_timestamp,
)


FAVORITE_TRANSFER_DOCUMENT_TYPE = "danbooru_multi_search.favorites"
FAVORITE_TRANSFER_VERSION = 1
MAX_FAVORITE_TRANSFER_BYTES = 64 * 1024 * 1024
_FOLDER_REFERENCE_PATTERN = re.compile(r"folder-[0-9]{6}")
_ROOT_FIELDS = frozenset(
    {"document_type", "exported_at", "favorites", "folders", "format_version"}
)
_FOLDER_FIELDS = frozenset({"created_at", "name", "reference", "updated_at"})
_FAVORITE_FIELDS = frozenset(
    {
        "display_name",
        "favorited_at",
        "folder_reference",
        "object_key",
        "object_type",
        "summary_json",
        "summary_version",
        "updated_at",
    }
)


class FavoriteTransferError(RuntimeError):
    """Report one stable transfer-document, file, or apply failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        residual_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.residual_path = residual_path


class FavoriteImportMode(StrEnum):
    MERGE = "merge"
    REPLACE = "replace"


@dataclass(frozen=True, slots=True)
class FavoriteTransferFolder:
    reference: str
    name: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _validate_folder_reference(self.reference)
        try:
            favorite_folder_key(self.name)
            encode_favorite_timestamp(self.created_at)
            encode_favorite_timestamp(self.updated_at)
        except FavoriteError as exc:
            raise FavoriteTransferError(exc.code, str(exc)) from exc
        if self.updated_at < self.created_at:
            raise FavoriteTransferError(
                "invalid_time_order",
                "收藏文件夹修改时间不能早于创建时间。",
            )


@dataclass(frozen=True, slots=True)
class FavoriteTransferItem:
    candidate: FavoriteCandidate
    folder_reference: str | None
    favorited_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, FavoriteCandidate):
            raise FavoriteTransferError("invalid_favorite", "收藏概要无效。")
        if self.folder_reference is not None:
            _validate_folder_reference(self.folder_reference)
        try:
            encode_favorite_timestamp(self.favorited_at)
            encode_favorite_timestamp(self.updated_at)
            encode_favorite_summary(self.candidate)
        except FavoriteError as exc:
            raise FavoriteTransferError(exc.code, str(exc)) from exc
        if self.updated_at < self.favorited_at:
            raise FavoriteTransferError(
                "invalid_time_order",
                "收藏修改时间不能早于首次收藏时间。",
            )


@dataclass(frozen=True, slots=True)
class FavoriteTransferDocument:
    exported_at: datetime
    folders: tuple[FavoriteTransferFolder, ...]
    favorites: tuple[FavoriteTransferItem, ...]

    def __post_init__(self) -> None:
        try:
            encode_favorite_timestamp(self.exported_at)
        except FavoriteError as exc:
            raise FavoriteTransferError(exc.code, str(exc)) from exc
        if not isinstance(self.folders, tuple) or not all(
            isinstance(folder, FavoriteTransferFolder) for folder in self.folders
        ):
            raise FavoriteTransferError("invalid_folders", "收藏文件夹数组无效。")
        if not isinstance(self.favorites, tuple) or not all(
            isinstance(item, FavoriteTransferItem) for item in self.favorites
        ):
            raise FavoriteTransferError("invalid_favorites", "收藏记录数组无效。")
        _validate_document_relations(self.folders, self.favorites)


@dataclass(frozen=True, slots=True)
class FavoriteImportPreview:
    mode: FavoriteImportMode
    document: FavoriteTransferDocument
    document_digest: str
    collection_revision: int
    added_folders: int
    reused_folders: int
    added_favorites: int
    preserved_favorites: int
    deleted_folders: int
    deleted_favorites: int
    binding_digest: str


def encode_favorite_transfer_document(document: FavoriteTransferDocument) -> bytes:
    """Return stable UTF-8 version-1 JSON without a byte-order mark."""

    if not isinstance(document, FavoriteTransferDocument):
        raise FavoriteTransferError("invalid_document", "收藏传输文档无效。")
    try:
        payload = {
            "document_type": FAVORITE_TRANSFER_DOCUMENT_TYPE,
            "exported_at": _encode_time(document.exported_at),
            "favorites": [
                {
                    "display_name": item.candidate.display_name,
                    "favorited_at": _encode_time(item.favorited_at),
                    "folder_reference": item.folder_reference,
                    "object_key": item.candidate.identity.object_key,
                    "object_type": item.candidate.identity.object_type.value,
                    "summary_json": _encode_summary(item.candidate),
                    "summary_version": FAVORITE_SUMMARY_VERSION,
                    "updated_at": _encode_time(item.updated_at),
                }
                for item in document.favorites
            ],
            "folders": [
                {
                    "created_at": _encode_time(folder.created_at),
                    "name": folder.name,
                    "reference": folder.reference,
                    "updated_at": _encode_time(folder.updated_at),
                }
                for folder in document.folders
            ],
            "format_version": FAVORITE_TRANSFER_VERSION,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (MemoryError, OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise FavoriteTransferError(
            "document_encode_error",
            "无法编码收藏传输文档。",
        ) from exc
    if len(encoded) > MAX_FAVORITE_TRANSFER_BYTES:
        raise FavoriteTransferError(
            "document_too_large",
            "收藏传输文档超过 64 MiB 上限。",
        )
    return encoded


def decode_favorite_transfer_document(data: bytes) -> FavoriteTransferDocument:
    """Strictly decode one bounded UTF-8 version-1 document."""

    if not isinstance(data, bytes):
        raise FavoriteTransferError("invalid_document_bytes", "收藏文档必须是字节数据。")
    if len(data) > MAX_FAVORITE_TRANSFER_BYTES:
        raise FavoriteTransferError("document_too_large", "收藏传输文档超过 64 MiB 上限。")
    if data.startswith(b"\xef\xbb\xbf"):
        raise FavoriteTransferError("utf8_bom_not_allowed", "收藏文档不能包含 UTF-8 BOM。")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise FavoriteTransferError("invalid_utf8", "收藏文档不是有效 UTF-8。") from exc
    except MemoryError as exc:
        raise FavoriteTransferError("document_too_complex", "收藏文档过于复杂。") from exc
    payload = _load_json_object(text)
    _expect_fields(payload, _ROOT_FIELDS, "收藏文档")
    if payload["document_type"] != FAVORITE_TRANSFER_DOCUMENT_TYPE:
        raise FavoriteTransferError("unknown_document_type", "收藏文档类型不受支持。")
    if type(payload["format_version"]) is not int or (
        payload["format_version"] != FAVORITE_TRANSFER_VERSION
    ):
        raise FavoriteTransferError("unknown_format_version", "收藏文档版本不受支持。")
    exported_at = _decode_time(payload["exported_at"])
    folders = _decode_folders(payload["folders"])
    favorites = _decode_favorites(payload["favorites"])
    try:
        return FavoriteTransferDocument(exported_at, folders, favorites)
    except MemoryError as exc:
        raise FavoriteTransferError("document_too_complex", "收藏文档过于复杂。") from exc


def read_favorite_transfer_document(path: Path | str) -> FavoriteTransferDocument:
    """Read at most 64 MiB and decode one local transfer document."""

    return decode_favorite_transfer_document(read_favorite_transfer_bytes(path))


def read_favorite_transfer_bytes(path: Path | str) -> bytes:
    """Read at most 64 MiB without decoding, allowing a later cancel checkpoint."""

    file_path = _coerce_path(path)
    try:
        if not file_path.is_file():
            raise FavoriteTransferError("invalid_file", "收藏导入路径不是普通文件。")
        if file_path.stat().st_size > MAX_FAVORITE_TRANSFER_BYTES:
            raise FavoriteTransferError(
                "document_too_large",
                "收藏传输文档超过 64 MiB 上限。",
            )
        with file_path.open("rb") as stream:
            data = stream.read(MAX_FAVORITE_TRANSFER_BYTES + 1)
    except FavoriteTransferError:
        raise
    except (MemoryError, OSError) as exc:
        raise FavoriteTransferError("file_read_error", "无法读取收藏传输文档。") from exc
    if len(data) > MAX_FAVORITE_TRANSFER_BYTES:
        raise FavoriteTransferError("document_too_large", "收藏传输文档超过 64 MiB 上限。")
    return data


def write_favorite_transfer_document(
    path: Path | str,
    document: FavoriteTransferDocument,
) -> None:
    """Write through a same-directory temporary file, fsync, and replace."""

    target = _coerce_path(path)
    data = encode_favorite_transfer_document(document)
    write_favorite_transfer_bytes(target, data)


def write_favorite_transfer_bytes(path: Path | str, data: bytes) -> None:
    """Atomically publish bytes produced by encode_favorite_transfer_document.

    The caller owns encoding and cancellation before entry; fsync, replacement,
    and temporary-file cleanup finish together once this step starts.
    """

    target = _coerce_path(path)
    parent = target.parent
    if not parent.is_dir():
        raise FavoriteTransferError("invalid_directory", "收藏导出目录不存在。")
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=parent,
        )
        temporary = Path(raw_path)
    except OSError as exc:
        raise FavoriteTransferError("file_write_error", "无法创建收藏导出临时文件。") from exc
    try:
        remaining = memoryview(data)
        try:
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("temporary file write made no progress")
                remaining = remaining[written:]
        except (MemoryError, OSError) as exc:
            raise FavoriteTransferError("file_write_error", "无法写入收藏导出文件。") from exc
        try:
            os.fsync(descriptor)
        except OSError as exc:
            raise FavoriteTransferError("file_sync_error", "无法刷新收藏导出文件。") from exc
        try:
            os.close(descriptor)
        except OSError as exc:
            raise FavoriteTransferError("file_close_error", "无法关闭收藏导出文件。") from exc
        descriptor = None
        try:
            os.replace(temporary, target)
        except OSError as exc:
            raise FavoriteTransferError("file_replace_error", "无法替换收藏导出文件。") from exc
        temporary = None
    finally:
        cleanup_error: OSError | None = None
        if descriptor is not None:
            for _attempt in range(2):
                try:
                    os.close(descriptor)
                except OSError as exc:
                    cleanup_error = exc
                else:
                    descriptor = None
                    cleanup_error = None
                    break
        if temporary is not None:
            if descriptor is None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as exc:
                    cleanup_error = exc
            if cleanup_error is not None:
                raise FavoriteTransferError(
                    "file_cleanup_error",
                    f"无法清理收藏导出临时文件：{temporary}",
                    residual_path=temporary,
                ) from cleanup_error


def _decode_folders(value: Any) -> tuple[FavoriteTransferFolder, ...]:
    if not isinstance(value, list):
        raise FavoriteTransferError("invalid_folders", "folders 必须是数组。")
    result: list[FavoriteTransferFolder] = []
    try:
        for payload in value:
            if not isinstance(payload, dict):
                raise FavoriteTransferError("invalid_folder", "收藏文件夹必须是对象。")
            _expect_fields(payload, _FOLDER_FIELDS, "收藏文件夹")
            result.append(
                FavoriteTransferFolder(
                    reference=_expect_text(payload["reference"], "reference"),
                    name=_expect_text(payload["name"], "name"),
                    created_at=_decode_time(payload["created_at"]),
                    updated_at=_decode_time(payload["updated_at"]),
                )
            )
        return _freeze_tuple(result)
    except MemoryError as exc:
        raise FavoriteTransferError("document_too_complex", "收藏文档过于复杂。") from exc


def _decode_favorites(value: Any) -> tuple[FavoriteTransferItem, ...]:
    if not isinstance(value, list):
        raise FavoriteTransferError("invalid_favorites", "favorites 必须是数组。")
    result: list[FavoriteTransferItem] = []
    try:
        for payload in value:
            if not isinstance(payload, dict):
                raise FavoriteTransferError("invalid_favorite", "收藏记录必须是对象。")
            _expect_fields(payload, _FAVORITE_FIELDS, "收藏记录")
            try:
                object_type = FavoriteObjectType(
                    _expect_text(payload["object_type"], "object_type")
                )
                identity = FavoriteIdentity(
                    object_type,
                    _expect_text(payload["object_key"], "object_key"),
                )
            except FavoriteError as exc:
                raise FavoriteTransferError(exc.code, str(exc)) from exc
            except ValueError as exc:
                raise FavoriteTransferError(
                    "invalid_object_type",
                    "收藏对象类型无效。",
                ) from exc
            if type(payload["summary_version"]) is not int or (
                payload["summary_version"] != FAVORITE_SUMMARY_VERSION
            ):
                raise FavoriteTransferError("invalid_summary", "收藏概要版本无效。")
            display_name = _expect_text(payload["display_name"], "display_name")
            summary = _expect_text(payload["summary_json"], "summary_json")
            try:
                candidate = decode_favorite_summary(identity, display_name, summary)
            except FavoriteError as exc:
                raise FavoriteTransferError(exc.code, str(exc)) from exc
            folder_reference = payload["folder_reference"]
            if folder_reference is not None:
                folder_reference = _expect_text(
                    folder_reference,
                    "folder_reference",
                )
            result.append(
                FavoriteTransferItem(
                    candidate=candidate,
                    folder_reference=folder_reference,
                    favorited_at=_decode_time(payload["favorited_at"]),
                    updated_at=_decode_time(payload["updated_at"]),
                )
            )
        return _freeze_tuple(result)
    except MemoryError as exc:
        raise FavoriteTransferError("document_too_complex", "收藏文档过于复杂。") from exc


def _validate_document_relations(
    folders: tuple[FavoriteTransferFolder, ...],
    favorites: tuple[FavoriteTransferItem, ...],
) -> None:
    references = [folder.reference for folder in folders]
    if len(set(references)) != len(references):
        raise FavoriteTransferError("duplicate_folder_reference", "文件夹引用重复。")
    normalized_names = [favorite_folder_key(folder.name) for folder in folders]
    if len(set(normalized_names)) != len(normalized_names):
        raise FavoriteTransferError("duplicate_folder", "收藏文件夹名称重复。")
    identities = [item.candidate.identity for item in favorites]
    if len(set(identities)) != len(identities):
        raise FavoriteTransferError("duplicate_favorite", "收藏身份重复。")
    valid_references = set(references)
    if any(
        item.folder_reference is not None
        and item.folder_reference not in valid_references
        for item in favorites
    ):
        raise FavoriteTransferError("invalid_folder_reference", "收藏文件夹引用不存在。")


def _load_json_object(text: str) -> dict[str, Any]:
    def reject_constant(value: str) -> Any:
        raise FavoriteTransferError("invalid_json", f"收藏文档包含非法数值 {value}。")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise FavoriteTransferError("duplicate_field", f"收藏文档字段 {key} 重复。")
            result[key] = value
        return result

    try:
        payload = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except FavoriteTransferError:
        raise
    except (
        json.JSONDecodeError,
        MemoryError,
        OverflowError,
        RecursionError,
        ValueError,
    ) as exc:
        raise FavoriteTransferError("invalid_json", "收藏文档不是有效 JSON。") from exc
    if not isinstance(payload, dict):
        raise FavoriteTransferError("invalid_document", "收藏文档根节点必须是对象。")
    return payload


def _expect_fields(payload: dict[str, Any], fields: frozenset[str], label: str) -> None:
    if frozenset(payload) != fields:
        raise FavoriteTransferError("invalid_fields", f"{label}字段集合无效。")


def _expect_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise FavoriteTransferError("invalid_field_type", f"{label} 必须是文本。")
    return value


def _freeze_tuple(values: list[Any]) -> tuple[Any, ...]:
    """Freeze one decoded collection inside the guarded allocation boundary."""

    return tuple(values)


def _encode_time(value: datetime) -> str:
    try:
        return encode_favorite_timestamp(value)
    except FavoriteError as exc:
        raise FavoriteTransferError(exc.code, str(exc)) from exc


def _decode_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise FavoriteTransferError("invalid_timestamp", "收藏时间必须是文本。")
    try:
        return decode_favorite_timestamp(value)
    except FavoriteError as exc:
        raise FavoriteTransferError(exc.code, str(exc)) from exc


def _encode_summary(candidate: FavoriteCandidate) -> str:
    try:
        return encode_favorite_summary(candidate)
    except FavoriteError as exc:
        raise FavoriteTransferError(exc.code, str(exc)) from exc


def _validate_folder_reference(value: Any) -> str:
    if not isinstance(value, str) or _FOLDER_REFERENCE_PATTERN.fullmatch(value) is None:
        raise FavoriteTransferError("invalid_folder_reference", "收藏文件夹引用无效。")
    return value


def _coerce_path(value: Path | str) -> Path:
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value:
        return Path(value)
    raise FavoriteTransferError("invalid_path", "收藏传输路径无效。")


def __getattr__(name: str) -> Any:
    """Keep the historical public service name without loading use cases eagerly."""

    if name == "FavoriteTransferService":
        from danbooru_multi_search.app.favorite_transfer import FavoriteTransferService

        globals()[name] = FavoriteTransferService
        return FavoriteTransferService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | {"FavoriteTransferService"})
