"""Qt-free download values, finite batches, and portable filename rules."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit


MAX_DOWNLOAD_BATCH_ITEMS = 1000
DEFAULT_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
_EXTENSION = re.compile(r"[A-Za-z0-9]{1,10}\Z")
_RESERVED = frozenset({"con", "prn", "aux", "nul"} | {f"{prefix}{index}" for prefix in ("com", "lpt") for index in "123456789¹²³"})


class DownloadError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DownloadConflictPolicy(StrEnum):
    SKIP = "skip"
    OVERWRITE = "overwrite"
    AUTO_NUMBER = "auto_number"


class DownloadTaskState(StrEnum):
    WAITING = "waiting"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    AWAITING_RESUME = "awaiting_resume"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


DOWNLOAD_TERMINAL_STATES = frozenset({DownloadTaskState.COMPLETED, DownloadTaskState.SKIPPED, DownloadTaskState.FAILED, DownloadTaskState.CANCELLED})


# The original smoke-download API continues to export these same objects.
class DownloadState:
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"


class DownloadErrorKind:
    INVALID_REQUEST = "invalid_request"
    HTTP = "http"
    TIMEOUT = "timeout"
    NETWORK = "network"
    EMPTY_RESPONSE = "empty_response"
    RESPONSE_TOO_LARGE = "response_too_large"
    FILE = "file_error"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    path: Path
    source_url: str
    bytes_written: int


@dataclass(frozen=True, slots=True)
class DanbooruDownloadOutcome:
    state: str
    message: str
    downloaded_file: DownloadedFile | None = None
    error_kind: str | None = None
    http_status: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.state == DownloadState.SUCCESS


def sanitize_filename_component(value: str) -> str:
    if not isinstance(value, str):
        raise DownloadError("invalid_filename", "下载文件名必须是文本。")
    cleaned = "".join("_" if char in '<>:"/\\|?*' or unicodedata.category(char).startswith("C") else char
                      for char in unicodedata.normalize("NFC", value)).strip().rstrip(". ")
    if not cleaned:
        cleaned = "download"
    if cleaned.split(".", 1)[0].casefold() in _RESERVED:
        cleaned = "_" + cleaned
    # Bound UTF-8 bytes as well as Windows UTF-16 units, leaving room for the
    # identifier, original extension and an auto-numbering suffix.
    return cleaned.encode("utf-8")[:120].decode("utf-8", errors="ignore").rstrip(". ")


def filename_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    post_id: int
    source_url: str
    file_ext: str
    label: str = ""

    def __post_init__(self) -> None:
        if type(self.post_id) is not int or not 1 <= self.post_id <= 2**63 - 1:
            raise DownloadError("invalid_request", "下载作品编号必须是有效正整数。")
        if not isinstance(self.source_url, str):
            raise DownloadError("invalid_request", "下载地址必须是文本。")
        try:
            url = urlsplit(self.source_url)
            if url.scheme not in {"http", "https"} or not url.hostname or any(char.isspace() or ord(char) < 32 for char in self.source_url):
                raise ValueError("invalid HTTP URL")
        except ValueError as exc:
            raise DownloadError("invalid_request", "下载地址必须是有效 HTTP 或 HTTPS 地址。") from exc
        if not isinstance(self.file_ext, str) or _EXTENSION.fullmatch(self.file_ext.strip()) is None:
            raise DownloadError("invalid_request", "下载扩展名无效。")
        if not isinstance(self.label, str):
            raise DownloadError("invalid_filename", "下载文件名必须是文本。")
        object.__setattr__(self, "file_ext", self.file_ext.strip().lower())

    @property
    def filename(self) -> str:
        stem = f"{sanitize_filename_component(self.label)}_{self.post_id}" if self.label else str(self.post_id)
        return f"{stem}.{self.file_ext}"


@dataclass(frozen=True, slots=True)
class DownloadItemSnapshot:
    item_id: str
    batch_id: str
    position: int
    request: DownloadRequest
    directory: Path
    policy: DownloadConflictPolicy
    state: DownloadTaskState
    resolved_name: str | None = None
    partial_name: str | None = None
    bytes_written: int = 0
    total_bytes: int | None = None
    attempts: int = 0
    generation: int = 0
    error_code: str = ""
    message: str = ""

    @property
    def destination(self) -> Path | None:
        return self.directory / self.resolved_name if self.resolved_name else None


@dataclass(frozen=True, slots=True)
class DownloadBatchSnapshot:
    batch_id: str
    items: tuple[DownloadItemSnapshot, ...]

    @property
    def counts(self) -> dict[str, int]:
        return {state.value: sum(item.state is state for item in self.items) for state in DownloadTaskState}

    @property
    def bytes_written(self) -> int:
        return sum(item.bytes_written for item in self.items)

    @property
    def total_bytes(self) -> int | None:
        return sum(item.total_bytes for item in self.items) if all(item.total_bytes is not None for item in self.items) else None

    @property
    def terminal(self) -> bool:
        return all(item.state in DOWNLOAD_TERMINAL_STATES for item in self.items)
