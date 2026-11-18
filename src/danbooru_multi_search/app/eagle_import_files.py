"""Worker-side readability, signature and content checks before Eagle writes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import zipfile

from danbooru_multi_search.app.eagle_import_types import EaglePostImportError
from danbooru_multi_search.core.downloads import DEFAULT_MAX_DOWNLOAD_BYTES


@dataclass(frozen=True, slots=True)
class CheckedImportFile:
    path: Path
    sha256: str
    size: int
    identity: tuple[int, int, int, int]


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def assert_file_unchanged(checked: CheckedImportFile) -> None:
    """Refuse to submit metadata for bytes replaced after verification."""
    try:
        if _identity(checked.path.stat()) != checked.identity:
            raise EaglePostImportError("eagle_file_changed", "The checked import file changed.")
    except OSError as exc:
        raise EaglePostImportError("eagle_file_changed", str(exc)) from exc


def _valid_signature(extension: str, prefix: bytes, tail: bytes, size: int) -> bool:
    if extension in {"jpg", "jpeg"}:
        return prefix.startswith(b"\xff\xd8\xff") and tail.endswith(b"\xff\xd9")
    if extension == "png":
        return (
            len(prefix) >= 33
            and prefix.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
            and int.from_bytes(prefix[16:20], "big") > 0
            and int.from_bytes(prefix[20:24], "big") > 0
            and tail.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")
        )
    if extension == "gif":
        return (
            prefix[:6] in {b"GIF87a", b"GIF89a"}
            and len(prefix) >= 13
            and int.from_bytes(prefix[6:8], "little") > 0
            and int.from_bytes(prefix[8:10], "little") > 0
            and tail.endswith(b";")
        )
    if extension == "webp":
        return (
            len(prefix) >= 16 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP"
            and int.from_bytes(prefix[4:8], "little") + 8 == size
            and prefix[12:16] in {b"VP8 ", b"VP8L", b"VP8X"}
        )
    if extension in {"avif", "mp4"}:
        box_size = int.from_bytes(prefix[:4], "big")
        if len(prefix) < 16 or prefix[4:8] != b"ftyp" or not 16 <= box_size <= size:
            return False
        return extension == "mp4" or any(brand in prefix[8:min(box_size, len(prefix))] for brand in (b"avif", b"avis"))
    if extension == "webm":
        return len(prefix) >= 32 and prefix.startswith(b"\x1aE\xdf\xa3") and b"webm" in prefix[:4096]
    if extension == "zip":
        return prefix.startswith((b"PK\x03\x04", b"PK\x05\x06")) and b"PK\x05\x06" in tail
    if extension == "swf":
        return len(prefix) >= 8 and prefix[:3] in {b"FWS", b"CWS", b"ZWS"}
    return False


def _check_mp4_boxes(path: Path, size: int, cancelled: Callable[[], bool]) -> None:
    position = 0
    boxes: set[bytes] = set()
    with path.open("rb") as stream:
        for _ in range(10000):
            if cancelled():
                raise EaglePostImportError("eagle_cancelled", "File verification was cancelled.")
            if position == size:
                if not {b"ftyp", b"moov", b"mdat"} <= boxes:
                    raise EaglePostImportError("eagle_file_invalid", "The MP4 file lacks complete media boxes.")
                return
            stream.seek(position)
            header = stream.read(8)
            if len(header) != 8:
                break
            box_size = int.from_bytes(header[:4], "big")
            minimum = 8
            if box_size == 1:
                extended = stream.read(8)
                if len(extended) != 8:
                    break
                box_size = int.from_bytes(extended, "big")
                minimum = 16
            elif box_size == 0:
                box_size = size - position
            if box_size < minimum or position + box_size > size:
                break
            boxes.add(header[4:8])
            position += box_size
    raise EaglePostImportError("eagle_file_invalid", "The MP4 structure is truncated or exceeds verification limits.")


def rendition_extension(path: Path) -> str:
    """Identify downloaded sample bytes before giving Eagle a file with the matching suffix."""
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        prefix = stream.read(65536)
        stream.seek(max(0, size - 65557))
        tail = stream.read(65557)
    for extension in ("jpg", "png", "gif", "webp", "avif", "mp4", "webm", "zip", "swf"):
        if _valid_signature(extension, prefix, tail, size):
            return extension
    raise EaglePostImportError("eagle_file_invalid", "The selected rendition has no recognized media signature.")


def check_import_file(
    path: Path,
    *,
    extension: str,
    expected_size: int | None,
    cancelled: Callable[[], bool],
    validate_media: Callable[[Path, str], None] | None,
) -> CheckedImportFile:
    """Read and hash once, then validate actual media on the worker thread."""
    if cancelled():
        raise EaglePostImportError("eagle_cancelled", "File verification was cancelled.")
    try:
        if not path.is_absolute() or not path.is_file():
            raise EaglePostImportError("eagle_file_invalid", "The import source is not an absolute regular file.")
        resolved = path.resolve(strict=True)
        digest = hashlib.sha256()
        prefix = b""
        tail = b""
        read_size = 0
        with resolved.open("rb") as stream:
            identity = _identity(os.fstat(stream.fileno()))
            size = identity[2]
            if not 0 < size <= DEFAULT_MAX_DOWNLOAD_BYTES:
                raise EaglePostImportError("eagle_file_invalid", "The import source is empty or exceeds 100 MiB.")
            if expected_size is not None and expected_size != size:
                raise EaglePostImportError("eagle_file_invalid", "The original file length does not match Danbooru metadata.")
            while True:
                if cancelled():
                    raise EaglePostImportError("eagle_cancelled", "File verification was cancelled.")
                block = stream.read(1024 * 1024)
                if not block:
                    break
                read_size += len(block)
                if read_size > DEFAULT_MAX_DOWNLOAD_BYTES:
                    raise EaglePostImportError("eagle_file_changed", "The import file grew during verification.")
                digest.update(block)
                if not prefix:
                    prefix = block[:65536]
                tail = (tail + block)[-65557:]
            if read_size != size or _identity(os.fstat(stream.fileno())) != identity:
                raise EaglePostImportError("eagle_file_changed", "The import file changed during verification.")
        extension = extension.casefold()
        if not _valid_signature(extension, prefix, tail, size):
            raise EaglePostImportError("eagle_file_invalid", "The file signature or ending does not match its media format.")
        if extension == "mp4":
            _check_mp4_boxes(resolved, size, cancelled)
        elif extension == "zip":
            with zipfile.ZipFile(resolved) as archive:
                archive.infolist()
        if validate_media is None:
            raise EaglePostImportError("eagle_file_invalid", "Worker media validation was not configured.")
        validate_media(resolved, extension)
        if cancelled():
            raise EaglePostImportError("eagle_cancelled", "File verification was cancelled.")
        checked = CheckedImportFile(resolved, digest.hexdigest(), size, identity)
        assert_file_unchanged(checked)
        return checked
    except EaglePostImportError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise EaglePostImportError("eagle_file_hash_failed", str(exc)) from exc
