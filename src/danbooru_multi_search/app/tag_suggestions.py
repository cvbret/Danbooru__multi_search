"""Coordinate local suggestions, bounded remote requests and CSV imports."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QCoreApplication, QObject, QThread, QTimer, Signal, Slot
from PySide6.QtNetwork import QNetworkAccessManager

from danbooru_multi_search.danbooru.autocomplete import (
    TAG_SUGGESTION_DEBOUNCE_MS,
    merge_tag_suggestions,
)
from danbooru_multi_search.danbooru.client import (
    DEFAULT_TIMEOUT_MS,
    select_preferred_tls_backend,
)
from danbooru_multi_search.danbooru.tag_index import (
    TagCsvImportCancelled,
    TagCsvImportError,
    TagCsvImportResult,
    TagIndexSnapshot,
    load_tag_csv,
)
from danbooru_multi_search.danbooru.tags import QtTagsRequestAttempt, build_tags_request
from danbooru_multi_search.danbooru.tag_types import (
    DEFAULT_TAG_SUGGESTION_LIMIT,
    MAX_TAG_RESPONSE_BYTES,
    TagSuggestion,
)
from danbooru_multi_search.danbooru.tag_syntax import normalize_tag_fragment
from danbooru_multi_search.danbooru.tag_translation import TagTranslationStore
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
    TaskRecord,
    TaskState,
)


@dataclass(frozen=True, slots=True)
class TagImportOutcome:
    succeeded: bool
    message: str
    error_code: str | None = None
    result: TagCsvImportResult | None = None
    path: Path | None = None
    generation: int = 0
    path_saved: bool | None = None


class _CsvImportThread(QThread):
    completed = Signal(int, object, object)

    def __init__(
        self,
        generation: int,
        path: Path,
        loader: Callable[..., TagCsvImportResult],
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        self._generation = generation
        self._path = path
        self._loader = loader
        self._cancel_event = threading.Event()

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        result: TagCsvImportResult | None = None
        error: Exception | None = None
        try:
            result = self._loader(self._path, cancel_event=self._cancel_event)
        except (TagCsvImportCancelled, TagCsvImportError) as exc:
            error = exc
        except Exception as exc:
            error = TagCsvImportError(
                "internal_error",
                f"标签 CSV 导入失败：{exc}",
            )
        self.completed.emit(self._generation, result, error)


class TagSuggestionService(QObject):
    """Own suggestion work without input widgets or view models.

    View signals are synchronous on this QObject's thread. Receivers may
    invalidate requests, so each original publication boundary is rechecked.
    CSV completion enters the QObject slot on this thread before publication.
    """

    suggestionsReplaced = Signal(int, object)
    suggestionsCleared = Signal(int)
    viewChanged = Signal()
    remoteStateChanged = Signal()
    noticeTextChanged = Signal()
    importStateChanged = Signal()
    importCompleted = Signal(object)
    refreshRequested = Signal(int, int)

    def __init__(
        self,
        base_url: str,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        network_manager: QNetworkAccessManager | None = None,
        task_queue: BoundedTaskQueue | None = None,
        queue_config: TaskQueueConfig | None = None,
        index_snapshot: TagIndexSnapshot | None = None,
        translation_store: TagTranslationStore | None = None,
        csv_loader: Callable[..., TagCsvImportResult] = load_tag_csv,
        csv_path_saver: Callable[[Path], None] | None = None,
        connect_application_quit: bool = True,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        select_preferred_tls_backend()
        self._base_url = base_url
        self._timeout_ms = timeout_ms
        self._network_manager = network_manager or QNetworkAccessManager(self)
        self._owns_queue = task_queue is None
        if task_queue is None:
            task_queue = BoundedTaskQueue(
                queue_config or TaskQueueConfig(),
                clock=SystemClock(),
                scheduler=QtScheduler(self),
            )
        self._task_queue = task_queue
        if translation_store is not None and index_snapshot is not None:
            raise ValueError(
                "index_snapshot and translation_store cannot both be provided"
            )
        self._translation_store = translation_store or TagTranslationStore(
            index_snapshot,
            self,
        )
        self._csv_loader = csv_loader
        self._csv_path_saver = csv_path_saver
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(TAG_SUGGESTION_DEBOUNCE_MS)
        self._timer.timeout.connect(self._start_remote_request)
        self._request_generation = 0
        self._active_task_id: str | None = None
        self._local_suggestions: tuple[TagSuggestion, ...] = ()
        self._fragment: str | None = None
        self._category: int | None = None
        self._remote_busy = False
        self._notice_text = ""
        self._import_generation = 0
        self._import_thread: _CsvImportThread | None = None
        self._import_path: Path | None = None
        self._persist_import_path = False
        self._import_busy = False
        self._import_status_text = "尚未导入本地标签库。"
        self._closing = False
        self._shutdown = False

        application = QCoreApplication.instance()
        if connect_application_quit and application is not None:
            application.aboutToQuit.connect(self.shutdown)

    @property
    def closed(self) -> bool:
        return self._closing or self._shutdown

    @property
    def remote_busy(self) -> bool:
        return self._remote_busy

    @property
    def debounce_pending(self) -> bool:
        return self._timer.isActive()

    @property
    def notice_text(self) -> str:
        return self._notice_text

    @property
    def import_busy(self) -> bool:
        return self._import_busy

    @property
    def import_status_text(self) -> str:
        return self._import_status_text

    @property
    def local_tag_count(self) -> int:
        return len(self._translation_store.snapshot.records)

    @property
    def translation_store(self) -> TagTranslationStore:
        return self._translation_store

    def is_current_request(self, generation: int) -> bool:
        """Compare identity independently from shutdown and remote permission."""
        return generation == self._request_generation

    def is_current_import(self, generation: int) -> bool:
        """Compare import identity without changing its lifecycle."""
        return generation == self._import_generation

    def suggest(self, fragment: str | None, *, generation: int, category: int | None = None) -> None:
        """Publish local candidates before considering any remote request."""
        if self.closed or not self.is_current_request(generation):
            return
        self._fragment = fragment
        self._category = category
        self._notice_text = ""
        if not fragment:
            self._local_suggestions = ()
            self.suggestionsCleared.emit(generation)
            self.viewChanged.emit()
            return

        if fragment.isascii():
            local = self._translation_store.snapshot.search_english(
                fragment,
                limit=DEFAULT_TAG_SUGGESTION_LIMIT * 2,
            )
        else:
            local = self._translation_store.snapshot.search_chinese(fragment)
        if category is not None:
            local = tuple(item for item in local if item.category == category)
        self._local_suggestions = local
        self.suggestionsReplaced.emit(generation, local[:DEFAULT_TAG_SUGGESTION_LIMIT])
        self.viewChanged.emit()
        if generation != self._request_generation or self._requests_disabled:
            return
        if fragment.isascii():
            try:
                normalize_tag_fragment(fragment)
            except ValueError:
                return
            self._timer.start()
            self.remoteStateChanged.emit()

    def begin_import(self) -> int:
        """Replace and wait for the previous import before URL validation."""
        if self.closed:
            return self._import_generation
        self._import_generation += 1
        generation = self._import_generation
        self._cancel_import_thread(wait=True)
        return generation

    def start_import(
        self,
        generation: int,
        path: Path,
        *,
        persist_path: bool,
    ) -> None:
        """Start the captured import only while its identity is current."""
        if self.closed or not self.is_current_import(generation):
            return
        thread = _CsvImportThread(
            generation,
            path,
            self._csv_loader,
            self,
        )
        thread.completed.connect(self._import_finished)
        self._import_thread = thread
        self._import_path = path
        self._persist_import_path = persist_path
        self._import_busy = True
        self._import_status_text = (
            "正在恢复本地标签 CSV……"
            if not persist_path
            else "正在读取本地标签 CSV……"
        )
        self.importStateChanged.emit()
        if (
            self._closing
            or self._shutdown
            or generation != self._import_generation
            or self._import_thread is not thread
        ):
            return
        thread.start()

    @Slot()
    def begin_shutdown(self) -> None:
        """Reject new work before coordinated shutdown reaches the worker."""

        if self._closing or self._shutdown:
            return
        self._closing = True
        self.invalidate_request()
        self._import_generation += 1

    @Slot()
    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self.invalidate_request()
        self._import_generation += 1
        self._cancel_import_thread(wait=True)
        self._import_busy = False
        self._local_suggestions = ()
        self._notice_text = ""
        self.suggestionsCleared.emit(self._request_generation)
        if self._owns_queue:
            self._task_queue.shutdown()
        self.viewChanged.emit()
        self.importStateChanged.emit()

    @Slot()
    def _start_remote_request(self) -> None:
        generation = self._request_generation
        fragment = self._fragment
        self.remoteStateChanged.emit()
        if (
            generation != self._request_generation
            or self._requests_disabled
            or not fragment
            or not fragment.isascii()
        ):
            return
        try:
            request = build_tags_request(
                self._base_url,
                fragment,
                timeout_ms=self._timeout_ms,
                category=self._category,
            )
        except ValueError as exc:
            self._notice_text = str(exc)
            self.noticeTextChanged.emit()
            return

        self._remote_busy = True
        self._notice_text = ""
        self.viewChanged.emit()
        if generation != self._request_generation or self._requests_disabled:
            return

        def starter(
            attempt_number: int,
            completion: Callable[[TaskAttemptResult], None],
        ) -> QtTagsRequestAttempt:
            return QtTagsRequestAttempt(
                self._network_manager,
                request,
                completion,
                self,
                maximum_bytes=MAX_TAG_RESPONSE_BYTES,
            )

        def state_changed(record: TaskRecord) -> None:
            self._task_state_changed(generation, record)

        def completed(result: TaskFinalResult) -> None:
            self._task_completed(generation, fragment, result)

        try:
            record = self._task_queue.submit(
                TaskKind.METADATA,
                starter,
                completed,
                state_listener=state_changed,
            )
        except (TaskQueueFullError, TaskQueueShutdownError) as exc:
            if (
                generation == self._request_generation
                and not self._requests_disabled
            ):
                self._remote_busy = False
                self._notice_text = f"标签建议暂时不可用：{exc}"
                self.viewChanged.emit()
            return
        if generation != self._request_generation or self._requests_disabled:
            self._task_queue.cancel(
                record.identifier,
                "A newer tag fragment replaced this suggestion task.",
            )
            return
        if not record.terminal:
            self._active_task_id = record.identifier

    def _task_state_changed(self, generation: int, record: TaskRecord) -> None:
        if record.terminal:
            return
        if generation != self._request_generation or self._requests_disabled:
            self._task_queue.cancel(
                record.identifier,
                "A newer tag fragment replaced this suggestion task.",
            )
            return
        self._remote_busy = True
        if record.state == TaskState.RETRY_WAIT:
            self._notice_text = "标签建议请求暂时失败，正在等待重试。"
        self.viewChanged.emit()
        if generation != self._request_generation or self._requests_disabled:
            self._task_queue.cancel(
                record.identifier,
                "A newer tag fragment replaced this suggestion task.",
            )

    def _task_completed(
        self,
        generation: int,
        fragment: str,
        result: TaskFinalResult,
    ) -> None:
        if generation != self._request_generation or self._requests_disabled:
            return
        if self._active_task_id is not None and result.task_id != self._active_task_id:
            return
        self._active_task_id = None
        self._remote_busy = False

        if result.state == TaskState.CANCELLED:
            self.viewChanged.emit()
            return
        if result.state == TaskState.FAILED:
            self._notice_text = result.error or "远程标签建议请求失败。"
            self.viewChanged.emit()
            return
        remote = result.value
        if not isinstance(remote, tuple) or not all(
            isinstance(item, TagSuggestion) for item in remote
        ):
            self._notice_text = "远程标签建议返回了无法识别的数据。"
            self.viewChanged.emit()
            return

        merged = merge_tag_suggestions(
            self._local_suggestions,
            remote,
            fragment,
        )
        self._notice_text = ""
        self.suggestionsReplaced.emit(generation, merged)
        if generation != self._request_generation or self._requests_disabled:
            return
        self.viewChanged.emit()

    def invalidate_request(self) -> int:
        """Invalidate only this service's remote work and return its new identity."""
        self._request_generation += 1
        generation = self._request_generation
        self._timer.stop()
        task_id = self._active_task_id
        self._active_task_id = None
        self._remote_busy = False
        if task_id is not None:
            self._task_queue.cancel(
                task_id,
                "A newer tag fragment replaced this suggestion task.",
            )
        return generation

    @Slot(int, object, object)
    def _import_finished(
        self,
        generation: int,
        result: TagCsvImportResult | None,
        error: Exception | None,
    ) -> None:
        thread = self._import_thread
        import_path = self._import_path
        persist_path = self._persist_import_path
        if thread is not None and generation == self._import_generation:
            thread.wait()
            thread.deleteLater()
            self._import_thread = None
            self._import_path = None
            self._persist_import_path = False
        if (
            generation != self._import_generation
            or self._closing
            or self._shutdown
        ):
            return

        self._import_busy = False
        if error is not None or result is None:
            if isinstance(error, TagCsvImportError):
                code = error.code
                message = str(error)
            else:
                code = "import_failed"
                message = str(error or "标签 CSV 导入失败。")
            outcome = TagImportOutcome(
                False,
                message,
                error_code=code,
                path=import_path,
                generation=generation,
            )
            self._import_status_text = message
            self.importStateChanged.emit()
            if (
                generation != self._import_generation
                or self._closing
                or self._shutdown
            ):
                return
            self.importCompleted.emit(outcome)
            return

        request_generation = self._request_generation
        self._translation_store.replace_snapshot(result.snapshot)
        if (
            generation != self._import_generation
            or self._closing
            or self._shutdown
        ):
            return
        path_saved: bool | None = None
        save_error: Exception | None = None
        if (
            persist_path
            and self._csv_path_saver is not None
            and import_path is not None
        ):
            try:
                self._csv_path_saver(import_path)
                path_saved = True
            except Exception as exc:
                path_saved = False
                save_error = exc
        if (
            generation != self._import_generation
            or self._closing
            or self._shutdown
        ):
            return
        success_text = (
            f"导入完成：总记录 {result.total_records}，有效 {result.valid_records}，"
            f"跳过 {result.skipped_records}，合并重复 {result.duplicate_records}，"
            f"唯一标签 {result.unique_tags}。"
        )
        if save_error is not None:
            success_text += f" 路径保存失败：{save_error}"
        self._import_status_text = success_text
        outcome = TagImportOutcome(
            True,
            success_text,
            error_code=("settings_save_failed" if save_error is not None else None),
            result=result,
            path=import_path,
            generation=generation,
            path_saved=path_saved,
        )
        self.importStateChanged.emit()
        if (
            generation != self._import_generation
            or self._closing
            or self._shutdown
        ):
            return
        self.importCompleted.emit(outcome)
        if (
            generation != self._import_generation
            or request_generation != self._request_generation
            or self._closing
            or self._shutdown
        ):
            return
        self.refreshRequested.emit(generation, request_generation)

    def reject_import(self, generation: int, code: str, message: str) -> None:
        """Publish a URL-validation failure for this import identity."""
        if self.closed or not self.is_current_import(generation):
            return
        self._import_busy = False
        self._import_status_text = message
        self.importStateChanged.emit()
        if (
            generation != self._import_generation
            or self._closing
            or self._shutdown
        ):
            return
        self.importCompleted.emit(
            TagImportOutcome(
                False,
                message,
                error_code=code,
                generation=generation,
            )
        )

    def _cancel_import_thread(self, *, wait: bool) -> None:
        thread = self._import_thread
        if thread is None:
            self._import_path = None
            self._persist_import_path = False
            return
        self._import_thread = None
        self._import_path = None
        self._persist_import_path = False
        thread.request_cancel()
        if wait:
            thread.wait()
        thread.deleteLater()

    @property
    def _requests_disabled(self) -> bool:
        return self._closing or self._shutdown or self._task_queue.is_shutdown
