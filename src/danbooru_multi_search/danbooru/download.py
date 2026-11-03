"""Cancellable, size-limited Danbooru media downloads."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

from PySide6.QtCore import QCoreApplication, QObject, QUrl, Signal, Slot
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from danbooru_multi_search.danbooru.client import DEFAULT_TIMEOUT_MS, USER_AGENT
from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.tasks import (
    BoundedTaskQueue,
    QtScheduler,
    SystemClock,
    TaskAttemptResult,
    TaskFinalResult,
    TaskKind,
    TaskQueueConfig,
    TaskQueueFullError,
    TaskQueueShutdownError,
    TaskState,
)


DEFAULT_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
_FILE_EXTENSION = re.compile(r"[A-Za-z0-9]{1,10}")


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


def build_media_request(
    source_url: str,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> QNetworkRequest:
    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be greater than zero")

    url = QUrl(source_url)
    if not url.isValid() or url.scheme() not in {"http", "https"} or not url.host():
        raise ValueError("Danbooru media URL must be a valid HTTP or HTTPS URL.")

    request = QNetworkRequest(url)
    request.setRawHeader(b"User-Agent", USER_AGENT.encode("ascii"))
    request.setRawHeader(
        b"Accept",
        b"image/*, video/*, application/octet-stream;q=0.8",
    )
    request.setAttribute(QNetworkRequest.Attribute.Http2AllowedAttribute, False)
    request.setTransferTimeout(timeout_ms)
    return request


def download_destination(post: DanbooruPost, download_directory: Path) -> Path:
    extension = post.file_ext.strip().lower()
    if _FILE_EXTENSION.fullmatch(extension) is None:
        raise ValueError("Danbooru file extension is not safe for a local filename.")
    return download_directory / f"{post.id}.{extension}"


class _QtDownloadAttempt(QObject):
    def __init__(
        self,
        manager: QNetworkAccessManager,
        request: QNetworkRequest,
        destination: Path,
        source_url: str,
        maximum_bytes: int,
        completion: Callable[[TaskAttemptResult], None],
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        self._completion: Callable[[TaskAttemptResult], None] | None = completion
        self._destination = destination
        self._source_url = source_url
        self._maximum_bytes = maximum_bytes
        self._bytes_written = 0
        self._finishing = False
        self._reply: QNetworkReply | None = None
        self._stream: BinaryIO | None = None
        self._temporary_path: Path | None = None

        temporary = tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=f".{destination.name}.",
            suffix=".part",
            dir=destination.parent,
            delete=False,
        )
        self._stream = temporary
        self._temporary_path = Path(temporary.name)
        try:
            self._reply = manager.get(request)
        except Exception:
            self._discard_temporary_file()
            raise

        self._reply.metaDataChanged.connect(self._metadata_changed)
        self._reply.readyRead.connect(self._ready_read)
        self._reply.finished.connect(self._finished)
        self._metadata_changed()

    def abort(self) -> None:
        self._completion = None
        reply = self._reply
        self._discard_temporary_file()
        if reply is not None:
            reply.abort()

    @Slot()
    def _metadata_changed(self) -> None:
        reply = self._reply
        if reply is None or self._completion is None:
            return
        raw_length = bytes(reply.rawHeader("Content-Length")).strip()
        if not raw_length:
            return
        try:
            content_length = int(raw_length)
        except ValueError:
            return
        if content_length > self._maximum_bytes:
            self._force_failure(
                TaskAttemptResult.failure(
                    "Danbooru media response exceeds the configured size limit.",
                    error_code=DownloadErrorKind.RESPONSE_TOO_LARGE,
                )
            )

    @Slot()
    def _ready_read(self) -> None:
        reply = self._reply
        stream = self._stream
        if reply is None or stream is None or self._completion is None:
            return

        payload = bytes(reply.readAll())
        if not payload:
            return
        if self._bytes_written + len(payload) > self._maximum_bytes:
            self._force_failure(
                TaskAttemptResult.failure(
                    "Danbooru media response exceeds the configured size limit.",
                    error_code=DownloadErrorKind.RESPONSE_TOO_LARGE,
                )
            )
            return

        try:
            written = stream.write(payload)
            if written != len(payload):
                raise OSError("The temporary file accepted only part of the payload.")
        except OSError as exc:
            self._force_failure(
                TaskAttemptResult.failure(
                    f"Unable to write the temporary download file: {exc}",
                    error_code=DownloadErrorKind.FILE,
                )
            )
            return
        self._bytes_written += written

    def _force_failure(self, result: TaskAttemptResult) -> None:
        if self._completion is None:
            return
        reply = self._reply
        if self._finishing:
            self._complete(result)
            return

        self._finishing = True
        self._reply = None
        self._complete(result)
        if reply is not None:
            reply.abort()
            reply.deleteLater()
        self.deleteLater()

    @Slot()
    def _finished(self) -> None:
        if self._finishing:
            return
        reply = self._reply
        if reply is None:
            return
        self._finishing = True

        if self._completion is not None:
            self._ready_read()

        self._reply = None
        status_value = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute
        )
        http_status = int(status_value) if status_value is not None else None
        network_error = reply.error()
        network_error_text = reply.errorString()
        retry_after_bytes = bytes(reply.rawHeader("Retry-After"))
        retry_after = (
            retry_after_bytes.decode("ascii", errors="replace").strip() or None
        )
        reply.deleteLater()

        if self._completion is None:
            self._discard_temporary_file()
            self.deleteLater()
            return

        if http_status is not None and not 200 <= http_status < 300:
            result = TaskAttemptResult.failure(
                f"Danbooru media request returned HTTP {http_status}.",
                error_code=DownloadErrorKind.HTTP,
                http_status=http_status,
                retry_after=retry_after,
            )
        elif network_error != QNetworkReply.NetworkError.NoError:
            if network_error == QNetworkReply.NetworkError.TimeoutError:
                error_code = DownloadErrorKind.TIMEOUT
                message = "Danbooru media request timed out."
            else:
                error_code = DownloadErrorKind.NETWORK
                message = f"Danbooru media network error: {network_error_text}"
            result = TaskAttemptResult.failure(
                message,
                error_code=error_code,
                http_status=http_status,
            )
        elif self._bytes_written == 0:
            result = TaskAttemptResult.failure(
                "Danbooru media response was empty.",
                error_code=DownloadErrorKind.EMPTY_RESPONSE,
                http_status=http_status,
            )
        else:
            result = self._commit_download(http_status)

        self._complete(result)
        self.deleteLater()

    def _commit_download(self, http_status: int | None) -> TaskAttemptResult:
        stream = self._stream
        temporary_path = self._temporary_path
        if stream is None or temporary_path is None:
            return TaskAttemptResult.failure(
                "The temporary download file is unavailable.",
                error_code=DownloadErrorKind.FILE,
                http_status=http_status,
            )

        try:
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            self._stream = None
            os.replace(temporary_path, self._destination)
            self._temporary_path = None
        except OSError as exc:
            return TaskAttemptResult.failure(
                f"Unable to complete the downloaded file: {exc}",
                error_code=DownloadErrorKind.FILE,
                http_status=http_status,
            )

        return TaskAttemptResult.success(
            DownloadedFile(
                path=self._destination,
                source_url=self._source_url,
                bytes_written=self._bytes_written,
            ),
            http_status=http_status,
        )

    def _complete(self, result: TaskAttemptResult) -> None:
        completion = self._completion
        self._completion = None
        if not result.succeeded:
            self._discard_temporary_file()
        if completion is not None:
            completion(result)

    def _discard_temporary_file(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

        temporary_path = self._temporary_path
        self._temporary_path = None
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


class DanbooruDownloadController(QObject):
    downloadCompleted = Signal(object)

    def __init__(
        self,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        maximum_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        network_manager: QNetworkAccessManager | None = None,
        task_queue: BoundedTaskQueue | None = None,
        queue_config: TaskQueueConfig | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if maximum_bytes <= 0 or isinstance(maximum_bytes, bool):
            raise ValueError("maximum_bytes must be greater than zero")
        self._timeout_ms = timeout_ms
        self._maximum_bytes = maximum_bytes
        self._network_manager = network_manager or QNetworkAccessManager(self)
        self._owns_queue = task_queue is None
        if task_queue is None:
            scheduler = QtScheduler(self)
            task_queue = BoundedTaskQueue(
                queue_config or TaskQueueConfig(),
                clock=SystemClock(),
                scheduler=scheduler,
            )
        self._task_queue = task_queue
        self._active_task_id: str | None = None
        self._generation = 0
        self._shutdown = False
        application = QCoreApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self.shutdown)

    def download(self, post: DanbooruPost, download_directory: Path) -> None:
        if self._shutdown:
            self._emit_error(
                "Application is shutting down; no download can start.",
                DownloadErrorKind.INVALID_REQUEST,
            )
            return
        if self._active_task_id is not None:
            self._emit_error(
                "A Danbooru media download is already active.",
                DownloadErrorKind.INVALID_REQUEST,
            )
            return

        source_url = post.download_url
        if not source_url:
            self._emit_error(
                "The Danbooru post has no original file URL.",
                DownloadErrorKind.INVALID_REQUEST,
            )
            return

        try:
            directory = Path(download_directory).expanduser().resolve()
            directory.mkdir(parents=True, exist_ok=True)
            if not directory.is_dir():
                raise OSError("Download destination is not a directory.")
            destination = download_destination(post, directory)
            request = build_media_request(source_url, timeout_ms=self._timeout_ms)
        except ValueError as exc:
            self._emit_error(str(exc), DownloadErrorKind.INVALID_REQUEST)
            return
        except OSError as exc:
            self._emit_error(
                f"Unable to prepare the download directory: {exc}",
                DownloadErrorKind.FILE,
            )
            return

        self._generation += 1
        generation = self._generation

        def starter(
            attempt_number: int,
            completion: Callable[[TaskAttemptResult], None],
        ) -> _QtDownloadAttempt | None:
            try:
                return _QtDownloadAttempt(
                    self._network_manager,
                    request,
                    destination,
                    source_url,
                    self._maximum_bytes,
                    completion,
                    self,
                )
            except OSError as exc:
                completion(
                    TaskAttemptResult.failure(
                        f"Unable to create the temporary download file: {exc}",
                        error_code=DownloadErrorKind.FILE,
                    )
                )
                return None

        def completed(result: TaskFinalResult) -> None:
            self._task_completed(generation, result)

        try:
            record = self._task_queue.submit(
                TaskKind.MEDIA,
                starter,
                completed,
            )
        except (TaskQueueFullError, TaskQueueShutdownError) as exc:
            self._emit_error(str(exc), DownloadErrorKind.INVALID_REQUEST)
            return

        if not record.terminal and generation == self._generation:
            self._active_task_id = record.identifier

    @Slot()
    def cancelCurrentDownload(self) -> None:
        task_id = self._active_task_id
        if task_id is not None:
            self._task_queue.cancel(task_id, "User cancelled the download.")

    @Slot()
    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        if self._owns_queue:
            self._task_queue.shutdown()
        elif self._active_task_id is not None:
            self._task_queue.cancel(
                self._active_task_id,
                "Application is shutting down.",
            )

    def _task_completed(self, generation: int, result: TaskFinalResult) -> None:
        if generation != self._generation:
            return
        if self._active_task_id is not None and result.task_id != self._active_task_id:
            return
        self._active_task_id = None

        if result.state == TaskState.CANCELLED:
            self.downloadCompleted.emit(
                DanbooruDownloadOutcome(
                    state=DownloadState.CANCELLED,
                    message="Download was cancelled.",
                    error_kind=DownloadErrorKind.CANCELLED,
                )
            )
            return

        if result.state == TaskState.FAILED:
            self.downloadCompleted.emit(
                DanbooruDownloadOutcome(
                    state=DownloadState.ERROR,
                    message=result.error or "Danbooru media download failed.",
                    error_kind=result.error_code,
                    http_status=result.http_status,
                )
            )
            return

        downloaded_file = result.value
        if not isinstance(downloaded_file, DownloadedFile):
            self._emit_error(
                "Danbooru media download returned an unknown result.",
                DownloadErrorKind.FILE,
            )
            return

        self.downloadCompleted.emit(
            DanbooruDownloadOutcome(
                state=DownloadState.SUCCESS,
                message=f"Downloaded Danbooru post to {downloaded_file.path.name}.",
                downloaded_file=downloaded_file,
                http_status=result.http_status,
            )
        )

    def _emit_error(self, message: str, error_kind: str) -> None:
        self.downloadCompleted.emit(
            DanbooruDownloadOutcome(
                state=DownloadState.ERROR,
                message=message,
                error_kind=error_kind,
            )
        )
