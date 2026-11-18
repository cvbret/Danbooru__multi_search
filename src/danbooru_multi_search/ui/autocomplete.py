"""QML input context, popup state and view models for tag suggestions."""

from __future__ import annotations

from enum import IntEnum
from pathlib import Path
from typing import Callable

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    Property,
    QUrl,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtNetwork import QNetworkAccessManager

from danbooru_multi_search.app.tag_suggestions import TagSuggestionService
from danbooru_multi_search.danbooru.autocomplete import (
    ActiveTagFragment,
    QueryTagFragment,
    TagReplacement,
    locate_active_tag_fragment,
    locate_query_tag_fragment,
    replace_active_tag_fragment,
    replace_query_tag_fragment,
)
from danbooru_multi_search.danbooru.client import DEFAULT_TIMEOUT_MS
from danbooru_multi_search.danbooru.tag_index import (
    TagCsvImportResult,
    TagIndexSnapshot,
    load_tag_csv,
)
from danbooru_multi_search.danbooru.tag_types import TagSuggestion
from danbooru_multi_search.danbooru.tag_translation import TagTranslationStore
from danbooru_multi_search.tasks import BoundedTaskQueue, TaskQueueConfig


class TagSuggestionRole(IntEnum):
    TAG_NAME = int(Qt.ItemDataRole.UserRole) + 1
    CHINESE_NAME_TEXT = TAG_NAME + 1
    CATEGORY_TEXT = TAG_NAME + 2
    POST_COUNT_TEXT = TAG_NAME + 3
    CATEGORY = TAG_NAME + 4


class TagSuggestionListModel(QAbstractListModel):
    countChanged = Signal()

    _ROLE_NAMES = {
        TagSuggestionRole.TAG_NAME: QByteArray(b"tagName"),
        TagSuggestionRole.CHINESE_NAME_TEXT: QByteArray(b"chineseNameText"),
        TagSuggestionRole.CATEGORY_TEXT: QByteArray(b"categoryText"),
        TagSuggestionRole.POST_COUNT_TEXT: QByteArray(b"postCountText"),
        TagSuggestionRole.CATEGORY: QByteArray(b"category"),
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
            TagSuggestionRole.CATEGORY: suggestion.category,
        }[suggestion_role]

    def set_items(self, items: tuple[TagSuggestion, ...]) -> None:
        self.beginResetModel()
        self._items = items
        self.endResetModel()
        self.countChanged.emit()

    def clear(self) -> None:
        if self._items:
            self.set_items(())


class DanbooruTagSuggestionController(QObject):
    """Keep the public QML interface while delegating work to one service."""

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
        csv_path_saver: Callable[[Path], None] | None = None,
        connect_application_quit: bool = True,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._model = TagSuggestionListModel(self)
        self._current_text = ""
        self._current_cursor = 0
        self._current_fragment: ActiveTagFragment | QueryTagFragment | None = None
        self._current_query_input = False
        self._is_open = False
        self._service = TagSuggestionService(
            base_url,
            timeout_ms=timeout_ms,
            network_manager=network_manager,
            task_queue=task_queue,
            queue_config=queue_config,
            index_snapshot=index_snapshot,
            translation_store=translation_store,
            csv_loader=csv_loader,
            csv_path_saver=csv_path_saver,
            connect_application_quit=connect_application_quit,
            parent=self,
        )
        self._service.suggestionsReplaced.connect(self._replace_suggestions)
        self._service.suggestionsCleared.connect(self._clear_suggestions)
        self._service.viewChanged.connect(self._emit_view_changes)
        self._service.remoteStateChanged.connect(self.remoteStateChanged)
        self._service.noticeTextChanged.connect(self.noticeTextChanged)
        self._service.importStateChanged.connect(self.importStateChanged)
        self._service.importCompleted.connect(self.importCompleted)
        self._service.refreshRequested.connect(self._refresh_after_import)

    @Property(QObject, constant=True)
    def suggestionsModel(self) -> QObject:
        return self._model

    @Property(bool, notify=openChanged)
    def isOpen(self) -> bool:
        return self._is_open

    @Property(bool, notify=remoteStateChanged)
    def remoteBusy(self) -> bool:
        return self._service.remote_busy

    @Property(bool, notify=remoteStateChanged)
    def debouncePending(self) -> bool:
        return self._service.debounce_pending

    @Property(str, notify=noticeTextChanged)
    def noticeText(self) -> str:
        return self._service.notice_text

    @Property(bool, notify=importStateChanged)
    def importBusy(self) -> bool:
        return self._service.import_busy

    @Property(str, notify=importStateChanged)
    def importStatusText(self) -> str:
        return self._service.import_status_text

    @Property(int, notify=importStateChanged)
    def localTagCount(self) -> int:
        return self._service.local_tag_count

    @Slot(str, int)
    def updateInput(self, text: str, cursor: int) -> None:
        self._update_suggestion_input(text, cursor, query_input=False)

    @Slot(str, int)
    def updateQueryInput(self, text: str, cursor: int) -> None:
        """Update suggestions for an unrestricted M2 boolean expression."""

        self._update_suggestion_input(text, cursor, query_input=True)

    def _update_suggestion_input(
        self,
        text: str,
        cursor: int,
        *,
        query_input: bool,
    ) -> None:
        if self._service.closed:
            return
        generation = self._service.invalidate_request()
        if self._service.closed or not self._service.is_current_request(generation):
            return
        self._current_text = text
        self._current_cursor = cursor
        self._current_query_input = query_input
        try:
            context = (
                locate_query_tag_fragment(text, cursor)
                if query_input
                else locate_active_tag_fragment(text, cursor)
            )
        except (TypeError, ValueError):
            context = None
        self._current_fragment = context

        fragment = None
        if (
            context is not None
            and not context.is_empty
            and not (
                not query_input
                and isinstance(context, ActiveTagFragment)
                and context.token_count > 2
            )
        ):
            fragment = context.fragment
        self._service.suggest(fragment, generation=generation)

    @Slot()
    def closeSuggestions(self) -> None:
        if self._service.closed:
            return
        generation = self._service.invalidate_request()
        self._service.suggest(None, generation=generation)

    @Slot(str, int, int, result="QVariantMap")
    def applySuggestion(
        self,
        text: str,
        cursor: int,
        index: int,
    ) -> dict[str, object]:
        if self._service.closed:
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

    @Slot(str, int, int, result="QVariantMap")
    def applyQuerySuggestion(
        self,
        text: str,
        cursor: int,
        index: int,
    ) -> dict[str, object]:
        """Apply one suggestion to M2 text without submitting a query."""

        if self._service.closed:
            return TagReplacement(text, cursor, False, False).to_mapping()
        if not 0 <= index < len(self._model.items):
            return TagReplacement(text, cursor, False, False).to_mapping()
        replacement = replace_query_tag_fragment(
            text,
            cursor,
            self._model.items[index].name,
        )
        self.closeSuggestions()
        return replacement.to_mapping()

    @Slot(QUrl)
    def importCsv(self, url: QUrl) -> None:
        if self._service.closed:
            return
        generation = self._service.begin_import()
        if not isinstance(url, QUrl) or not url.isLocalFile():
            self._service.reject_import(
                generation, "invalid_file_url", "请选择本机上的 CSV 文件。",
            )
            return
        local_path = url.toLocalFile()
        if not local_path:
            self._service.reject_import(
                generation, "invalid_file_url", "请选择本机上的 CSV 文件。",
            )
            return
        self._service.start_import(
            generation,
            Path(local_path).expanduser().resolve(),
            persist_path=True,
        )

    @Slot(str)
    def importSavedCsv(self, path: str) -> None:
        """Restore the local index without saving the same path again."""
        if self._service.closed or not isinstance(path, str) or not path:
            return
        generation = self._service.begin_import()
        self._service.start_import(
            generation,
            Path(path).expanduser().resolve(),
            persist_path=False,
        )

    @Slot()
    def begin_shutdown(self) -> None:
        self._service.begin_shutdown()

    @Slot()
    def shutdown(self) -> None:
        self._service.shutdown()

    @Slot(int, object)
    def _replace_suggestions(self, generation: int, items: tuple[TagSuggestion, ...]) -> None:
        if not self._service.is_current_request(generation):
            return
        self._is_open = bool(items)
        self._model.set_items(items)

    @Slot(int)
    def _clear_suggestions(self, generation: int) -> None:
        if not self._service.is_current_request(generation):
            return
        self._is_open = False
        self._model.clear()

    @Slot(int, int)
    def _refresh_after_import(self, import_generation: int, request_generation: int) -> None:
        if (
            self._service.closed
            or not self._service.is_current_import(import_generation)
            or not self._service.is_current_request(request_generation)
        ):
            return
        if self._current_query_input:
            self.updateQueryInput(self._current_text, self._current_cursor)
        else:
            self.updateInput(self._current_text, self._current_cursor)

    @Slot()
    def _emit_view_changes(self) -> None:
        self.openChanged.emit()
        self.remoteStateChanged.emit()
        self.noticeTextChanged.emit()

    @property
    def translation_store(self) -> TagTranslationStore:
        return self._service.translation_store
