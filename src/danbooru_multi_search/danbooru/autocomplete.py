"""Cursor-aware local and remote tag suggestions for the QML search field."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Callable

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QCoreApplication,
    QModelIndex,
    QObject,
    Property,
    QThread,
    QTimer,
    QUrl,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtNetwork import QNetworkAccessManager

from danbooru_multi_search.danbooru.client import (
    DEFAULT_TIMEOUT_MS,
    select_preferred_tls_backend,
)
from danbooru_multi_search.danbooru.gallery import (
    GalleryQueryError,
    build_gallery_request_spec,
)
from danbooru_multi_search.danbooru.tag_index import (
    TagCsvImportCancelled,
    TagCsvImportError,
    TagCsvImportResult,
    TagIndexSnapshot,
    load_tag_csv,
)
from danbooru_multi_search.danbooru.tags import (
    DEFAULT_TAG_SUGGESTION_LIMIT,
    MAX_TAG_RESPONSE_BYTES,
    QtTagsRequestAttempt,
    TagSuggestion,
    build_tags_request,
    normalize_tag_fragment,
)
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


TAG_SUGGESTION_DEBOUNCE_MS = 300
_KNOWN_CATEGORIES = frozenset({0, 1, 3, 4, 5})


@dataclass(frozen=True, slots=True)
class ActiveTagFragment:
    text: str
    cursor: int
    start: int
    end: int
    fragment: str
    token_count: int

    @property
    def is_empty(self) -> bool:
        return not self.fragment


@dataclass(frozen=True, slots=True)
class TagReplacement:
    text: str
    cursor: int
    changed: bool
    should_search: bool

    def to_mapping(self) -> dict[str, object]:
        return {
            "text": self.text,
            "cursor": self.cursor,
            "changed": self.changed,
            "shouldSearch": self.should_search,
        }


@dataclass(frozen=True, slots=True)
class TagImportOutcome:
    succeeded: bool
    message: str
    error_code: str | None = None
    result: TagCsvImportResult | None = None


def locate_active_tag_fragment(text: str, cursor: int) -> ActiveTagFragment:
    """Locate the complete non-whitespace token adjacent to a Qt cursor."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not isinstance(cursor, int) or isinstance(cursor, bool):
        raise TypeError("cursor must be an integer")
    if not 0 <= cursor <= len(text):
        raise ValueError("cursor must be within the text")

    seed: int | None = None
    if cursor < len(text) and not text[cursor].isspace():
        seed = cursor
    elif cursor > 0 and not text[cursor - 1].isspace():
        seed = cursor - 1

    token_count = len(text.split())
    if seed is None:
        return ActiveTagFragment(text, cursor, cursor, cursor, "", token_count)

    start = seed
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    end = seed + 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return ActiveTagFragment(
        text=text,
        cursor=cursor,
        start=start,
        end=end,
        fragment=text[start:end],
        token_count=token_count,
    )


def replace_active_tag_fragment(
    text: str,
    cursor: int,
    tag_name: str,
) -> TagReplacement:
    context = locate_active_tag_fragment(text, cursor)
    if context.is_empty or context.token_count > 2:
        return TagReplacement(text, cursor, False, False)
    try:
        replacement_spec = build_gallery_request_spec(tag_name)
    except GalleryQueryError:
        return TagReplacement(text, cursor, False, False)
    if len(replacement_spec.tags) != 1:
        return TagReplacement(text, cursor, False, False)
    normalized_tag = replacement_spec.tags[0]

    replaced = text[: context.start] + normalized_tag + text[context.end :]
    new_cursor = context.start + len(normalized_tag)
    if len(replaced.split()) > 2:
        return TagReplacement(text, cursor, False, False)
    try:
        build_gallery_request_spec(replaced)
    except GalleryQueryError:
        should_search = False
    else:
        should_search = replaced.isascii()
    return TagReplacement(replaced, new_cursor, True, should_search)


def merge_tag_suggestions(
    local: Sequence[TagSuggestion],
    remote: Sequence[TagSuggestion],
    fragment: str,
    *,
    limit: int = DEFAULT_TAG_SUGGESTION_LIMIT,
) -> tuple[TagSuggestion, ...]:
    """Merge field-by-field, then apply deterministic query-specific sorting."""

    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    local_by_name: dict[str, TagSuggestion] = {}
    for suggestion in local:
        local_by_name[suggestion.name] = _merge_same_source(
            local_by_name.get(suggestion.name), suggestion
        )
    remote_by_name: dict[str, TagSuggestion] = {}
    for suggestion in remote:
        remote_by_name[suggestion.name] = _merge_same_source(
            remote_by_name.get(suggestion.name), suggestion
        )

    merged = dict(local_by_name)
    for name, suggestion in remote_by_name.items():
        current = merged.get(name)
        if current is None:
            merged[name] = suggestion
            continue
        merged[name] = TagSuggestion(
            name=name,
            chinese_names=_ordered_union(
                current.chinese_names,
                suggestion.chinese_names,
            ),
            post_count=(
                suggestion.post_count
                if suggestion.post_count is not None
                else current.post_count
            ),
            category=(
                suggestion.category
                if suggestion.category in _KNOWN_CATEGORIES
                else current.category
            ),
        )

    chinese = not fragment.isascii()
    normalized_fragment = fragment.strip()
    if not chinese:
        try:
            normalized_fragment = normalize_tag_fragment(fragment)
        except ValueError:
            return ()

    candidates: list[TagSuggestion] = []
    for suggestion in merged.values():
        if chinese:
            matched_names = [
                name
                for name in suggestion.chinese_names
                if normalized_fragment in name
            ]
            if not matched_names:
                continue
            exact_names = [name for name in matched_names if name == normalized_fragment]
            preferred = exact_names[0] if exact_names else matched_names[0]
            candidates.append(suggestion.prefer_chinese_name(preferred))
        elif suggestion.name.startswith(normalized_fragment):
            candidates.append(suggestion)

    candidates.sort(
        key=lambda item: _merged_sort_key(item, normalized_fragment, chinese)
    )
    return tuple(candidates[:limit])


def _merge_same_source(
    current: TagSuggestion | None,
    incoming: TagSuggestion,
) -> TagSuggestion:
    if current is None:
        return incoming
    categories = {
        value
        for value in (current.category, incoming.category)
        if value in _KNOWN_CATEGORIES
    }
    return TagSuggestion(
        name=incoming.name,
        chinese_names=_ordered_union(
            current.chinese_names,
            incoming.chinese_names,
        ),
        post_count=max(
            (
                value
                for value in (current.post_count, incoming.post_count)
                if value is not None
            ),
            default=None,
        ),
        category=next(iter(categories)) if len(categories) == 1 else None,
    )


def _ordered_union(first: Sequence[str], second: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*first, *second)))


def _merged_sort_key(
    suggestion: TagSuggestion,
    fragment: str,
    chinese: bool,
) -> tuple[int, bool, int, str]:
    exact = (
        fragment in suggestion.chinese_names
        if chinese
        else suggestion.name == fragment
    )
    return (
        0 if exact else 1,
        suggestion.post_count is None,
        -(suggestion.post_count or 0),
        suggestion.name,
    )


class TagSuggestionRole(IntEnum):
    TAG_NAME = int(Qt.ItemDataRole.UserRole) + 1
    CHINESE_NAME_TEXT = TAG_NAME + 1
    CATEGORY_TEXT = TAG_NAME + 2
    POST_COUNT_TEXT = TAG_NAME + 3


class TagSuggestionListModel(QAbstractListModel):
    countChanged = Signal()

    _ROLE_NAMES = {
        TagSuggestionRole.TAG_NAME: QByteArray(b"tagName"),
        TagSuggestionRole.CHINESE_NAME_TEXT: QByteArray(b"chineseNameText"),
        TagSuggestionRole.CATEGORY_TEXT: QByteArray(b"categoryText"),
        TagSuggestionRole.POST_COUNT_TEXT: QByteArray(b"postCountText"),
    }

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: tuple[TagSuggestion, ...] = ()

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._items)

    @property
    def items(self) -> tuple[TagSuggestion, ...]:
        return self._items

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def roleNames(self) -> dict[int, QByteArray]:
        return {int(role): name for role, name in self._ROLE_NAMES.items()}

    def data(self, index: QModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        suggestion = self._items[index.row()]
        try:
            suggestion_role = TagSuggestionRole(role)
        except ValueError:
            return None
        return {
            TagSuggestionRole.TAG_NAME: suggestion.name,
            TagSuggestionRole.CHINESE_NAME_TEXT: suggestion.chinese_name_text,
            TagSuggestionRole.CATEGORY_TEXT: suggestion.category_text,
            TagSuggestionRole.POST_COUNT_TEXT: suggestion.post_count_text,
        }[suggestion_role]

    def set_items(self, items: tuple[TagSuggestion, ...]) -> None:
        self.beginResetModel()
        self._items = items
        self.endResetModel()
        self.countChanged.emit()

    def clear(self) -> None:
        if self._items:
            self.set_items(())


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


class DanbooruTagSuggestionController(QObject):
    openChanged = Signal()
    remoteStateChanged = Signal()
    noticeTextChanged = Signal()
    importStateChanged = Signal()
    importCompleted = Signal(object)

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
        self._model = TagSuggestionListModel(self)
        if translation_store is not None and index_snapshot is not None:
            raise ValueError(
                "index_snapshot and translation_store cannot both be provided"
            )
        self._translation_store = translation_store or TagTranslationStore(
            index_snapshot,
            self,
        )
        self._csv_loader = csv_loader
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(TAG_SUGGESTION_DEBOUNCE_MS)
        self._timer.timeout.connect(self._start_remote_request)
        self._request_generation = 0
        self._active_task_id: str | None = None
        self._local_suggestions: tuple[TagSuggestion, ...] = ()
        self._current_text = ""
        self._current_cursor = 0
        self._current_fragment: ActiveTagFragment | None = None
        self._is_open = False
        self._remote_busy = False
        self._notice_text = ""
        self._import_generation = 0
        self._import_thread: _CsvImportThread | None = None
        self._import_busy = False
        self._import_status_text = "尚未导入本地标签库。"
        self._shutdown = False

        application = QCoreApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self.shutdown)

    @Property(QObject, constant=True)
    def suggestionsModel(self) -> QObject:
        return self._model

    @Property(bool, notify=openChanged)
    def isOpen(self) -> bool:
        return self._is_open

    @Property(bool, notify=remoteStateChanged)
    def remoteBusy(self) -> bool:
        return self._remote_busy

    @Property(bool, notify=remoteStateChanged)
    def debouncePending(self) -> bool:
        return self._timer.isActive()

    @Property(str, notify=noticeTextChanged)
    def noticeText(self) -> str:
        return self._notice_text

    @Property(bool, notify=importStateChanged)
    def importBusy(self) -> bool:
        return self._import_busy

    @Property(str, notify=importStateChanged)
    def importStatusText(self) -> str:
        return self._import_status_text

    @Property(int, notify=importStateChanged)
    def localTagCount(self) -> int:
        return len(self._translation_store.snapshot.records)

    @Slot(str, int)
    def updateInput(self, text: str, cursor: int) -> None:
        if self._shutdown:
            return
        generation = self._invalidate_remote_request()
        self._current_text = text
        self._current_cursor = cursor
        self._notice_text = ""
        try:
            context = locate_active_tag_fragment(text, cursor)
        except (TypeError, ValueError):
            context = None
        self._current_fragment = context

        if (
            context is None
            or context.is_empty
            or context.token_count > 2
        ):
            self._local_suggestions = ()
            self._is_open = False
            self._model.clear()
            self._emit_view_changes()
            return

        if context.fragment.isascii():
            local = self._translation_store.snapshot.search_english(
                context.fragment,
                limit=DEFAULT_TAG_SUGGESTION_LIMIT * 2,
            )
        else:
            local = self._translation_store.snapshot.search_chinese(
                context.fragment
            )
        self._local_suggestions = local
        visible_local = local[:DEFAULT_TAG_SUGGESTION_LIMIT]
        self._is_open = bool(visible_local)
        self._model.set_items(visible_local)
        self._emit_view_changes()
        if generation != self._request_generation or self._requests_disabled:
            return

        if context.fragment.isascii():
            try:
                normalize_tag_fragment(context.fragment)
            except ValueError:
                return
            self._timer.start()
            self.remoteStateChanged.emit()

    @Slot()
    def closeSuggestions(self) -> None:
        if self._shutdown:
            return
        self._invalidate_remote_request()
        self._local_suggestions = ()
        self._is_open = False
        self._notice_text = ""
        self._model.clear()
        self._emit_view_changes()

    @Slot(str, int, int, result="QVariantMap")
    def applySuggestion(
        self,
        text: str,
        cursor: int,
        index: int,
    ) -> dict[str, object]:
        if self._shutdown:
            return TagReplacement(text, cursor, False, False).to_mapping()
        if not 0 <= index < len(self._model.items):
            return TagReplacement(text, cursor, False, False).to_mapping()
        replacement = replace_active_tag_fragment(
            text,
            cursor,
            self._model.items[index].name,
        )
        self.closeSuggestions()
        return replacement.to_mapping()

    @Slot(QUrl)
    def importCsv(self, url: QUrl) -> None:
        if self._shutdown:
            return
        self._import_generation += 1
        generation = self._import_generation
        self._cancel_import_thread(wait=True)
        if not isinstance(url, QUrl) or not url.isLocalFile():
            self._set_import_error(
                "invalid_file_url",
                "请选择本机上的 CSV 文件。",
            )
            return
        local_path = url.toLocalFile()
        if not local_path:
            self._set_import_error(
                "invalid_file_url",
                "请选择本机上的 CSV 文件。",
            )
            return

        thread = _CsvImportThread(
            generation,
            Path(local_path),
            self._csv_loader,
            self,
        )
        thread.completed.connect(self._import_finished)
        self._import_thread = thread
        self._import_busy = True
        self._import_status_text = "正在读取本地标签 CSV……"
        self.importStateChanged.emit()
        if (
            self._shutdown
            or generation != self._import_generation
            or self._import_thread is not thread
        ):
            return
        thread.start()

    @Slot()
    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self._invalidate_remote_request()
        self._import_generation += 1
        self._cancel_import_thread(wait=True)
        self._import_busy = False
        self._local_suggestions = ()
        self._is_open = False
        self._notice_text = ""
        self._model.clear()
        if self._owns_queue:
            self._task_queue.shutdown()
        self._emit_view_changes()
        self.importStateChanged.emit()

    @Slot()
    def _start_remote_request(self) -> None:
        generation = self._request_generation
        context = self._current_fragment
        self.remoteStateChanged.emit()
        if (
            generation != self._request_generation
            or self._requests_disabled
            or context is None
            or context.is_empty
            or context.token_count > 2
            or not context.fragment.isascii()
        ):
            return
        try:
            request = build_tags_request(
                self._base_url,
                context.fragment,
                timeout_ms=self._timeout_ms,
            )
        except ValueError as exc:
            self._notice_text = str(exc)
            self.noticeTextChanged.emit()
            return

        self._remote_busy = True
        self._notice_text = ""
        self._emit_view_changes()
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
            self._task_completed(generation, context.fragment, result)

        try:
            record = self._task_queue.submit(
                TaskKind.METADATA,
                starter,
                completed,
                state_listener=state_changed,
            )
        except (TaskQueueFullError, TaskQueueShutdownError) as exc:
            if generation == self._request_generation and not self._shutdown:
                self._remote_busy = False
                self._notice_text = f"标签建议暂时不可用：{exc}"
                self._emit_view_changes()
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
        self._emit_view_changes()
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
        if generation != self._request_generation or self._shutdown:
            return
        if self._active_task_id is not None and result.task_id != self._active_task_id:
            return
        self._active_task_id = None
        self._remote_busy = False

        if result.state == TaskState.CANCELLED:
            self._emit_view_changes()
            return
        if result.state == TaskState.FAILED:
            self._notice_text = result.error or "远程标签建议请求失败。"
            self._emit_view_changes()
            return
        remote = result.value
        if not isinstance(remote, tuple) or not all(
            isinstance(item, TagSuggestion) for item in remote
        ):
            self._notice_text = "远程标签建议返回了无法识别的数据。"
            self._emit_view_changes()
            return

        merged = merge_tag_suggestions(
            self._local_suggestions,
            remote,
            fragment,
        )
        self._notice_text = ""
        self._is_open = bool(merged)
        self._model.set_items(merged)
        if generation != self._request_generation or self._requests_disabled:
            return
        self._emit_view_changes()

    def _invalidate_remote_request(self) -> int:
        self._request_generation += 1
        self._timer.stop()
        task_id = self._active_task_id
        self._active_task_id = None
        self._remote_busy = False
        if task_id is not None:
            self._task_queue.cancel(
                task_id,
                "A newer tag fragment replaced this suggestion task.",
            )
        return self._request_generation

    @Slot(int, object, object)
    def _import_finished(
        self,
        generation: int,
        result: TagCsvImportResult | None,
        error: Exception | None,
    ) -> None:
        thread = self._import_thread
        if thread is not None and generation == self._import_generation:
            thread.wait()
            thread.deleteLater()
            self._import_thread = None
        if generation != self._import_generation or self._shutdown:
            return

        self._import_busy = False
        if error is not None or result is None:
            if isinstance(error, TagCsvImportError):
                code = error.code
                message = str(error)
            else:
                code = "import_failed"
                message = str(error or "标签 CSV 导入失败。")
            outcome = TagImportOutcome(False, message, error_code=code)
            self._import_status_text = message
            self.importStateChanged.emit()
            if generation != self._import_generation or self._shutdown:
                return
            self.importCompleted.emit(outcome)
            return

        request_generation = self._request_generation
        self._translation_store.replace_snapshot(result.snapshot)
        if generation != self._import_generation or self._shutdown:
            return
        self._import_status_text = (
            f"导入完成：总记录 {result.total_records}，有效 {result.valid_records}，"
            f"跳过 {result.skipped_records}，合并重复 {result.duplicate_records}，"
            f"唯一标签 {result.unique_tags}。"
        )
        outcome = TagImportOutcome(True, self._import_status_text, result=result)
        self.importStateChanged.emit()
        if generation != self._import_generation or self._shutdown:
            return
        self.importCompleted.emit(outcome)
        if (
            generation != self._import_generation
            or request_generation != self._request_generation
            or self._shutdown
        ):
            return
        self.updateInput(self._current_text, self._current_cursor)

    def _set_import_error(self, code: str, message: str) -> None:
        generation = self._import_generation
        self._import_busy = False
        self._import_status_text = message
        self.importStateChanged.emit()
        if generation != self._import_generation or self._shutdown:
            return
        self.importCompleted.emit(TagImportOutcome(False, message, error_code=code))

    def _cancel_import_thread(self, *, wait: bool) -> None:
        thread = self._import_thread
        if thread is None:
            return
        self._import_thread = None
        thread.request_cancel()
        if wait:
            thread.wait()
        thread.deleteLater()

    @property
    def _requests_disabled(self) -> bool:
        return self._shutdown or self._task_queue.is_shutdown

    def _emit_view_changes(self) -> None:
        self.openChanged.emit()
        self.remoteStateChanged.emit()
        self.noticeTextChanged.emit()

    @property
    def translation_store(self) -> TagTranslationStore:
        return self._translation_store

    @property
    def _index(self) -> TagIndexSnapshot:
        """Compatibility view for existing tests while the store is shared."""

        return self._translation_store.snapshot

    @_index.setter
    def _index(self, snapshot: TagIndexSnapshot) -> None:
        self._translation_store.replace_snapshot(snapshot)
