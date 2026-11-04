"""QML-facing recent-query, restore-plan, and debounced session coordination."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum
from typing import Sequence

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    Property,
    QTimer,
    Qt,
    Signal,
    Slot,
)

from danbooru_multi_search.core.settings import AppSettings
from danbooru_multi_search.danbooru.gallery import (
    DanbooruGalleryController,
    GalleryPageOutcome,
    GalleryRequestAccepted,
    GalleryRequestOrigin,
    RequestState,
)
from danbooru_multi_search.storage import (
    GalleryPosition,
    HistoryError,
    HistoryRepository,
    RecentQuery,
    WindowGeometry as StoredWindowGeometry,
)
from danbooru_multi_search.ui.window_state import (
    MINIMUM_WINDOW_HEIGHT,
    MINIMUM_WINDOW_WIDTH,
    ScreenGeometry,
    WindowGeometry,
    restore_window_geometry,
)


SESSION_SAVE_DELAY_MS = 500
_RATING_TO_STORAGE = {
    "all": "all",
    "general": "general",
    "sensitive": "sensitive",
    "questionable": "questionable",
    "explicit": "explicit",
}
_RATING_TO_UI = {
    "all": "all",
    "g": "general",
    "general": "general",
    "s": "sensitive",
    "sensitive": "sensitive",
    "q": "questionable",
    "questionable": "questionable",
    "e": "explicit",
    "explicit": "explicit",
}


class RecentQueryRole(IntEnum):
    IDENTIFIER = int(Qt.ItemDataRole.UserRole) + 1
    DISPLAY_TEXT = IDENTIFIER + 1
    RATING = IDENTIFIER + 2
    PAGE_SIZE = IDENTIFIER + 3
    RESTORE_PAGE = IDENTIFIER + 4
    SUMMARY = IDENTIFIER + 5


class RecentQueryListModel(QAbstractListModel):
    countChanged = Signal()

    _ROLE_NAMES = {
        RecentQueryRole.IDENTIFIER: QByteArray(b"historyId"),
        RecentQueryRole.DISPLAY_TEXT: QByteArray(b"displayText"),
        RecentQueryRole.RATING: QByteArray(b"ratingValue"),
        RecentQueryRole.PAGE_SIZE: QByteArray(b"pageSize"),
        RecentQueryRole.RESTORE_PAGE: QByteArray(b"restorePage"),
        RecentQueryRole.SUMMARY: QByteArray(b"summaryText"),
    }

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: tuple[RecentQuery, ...] = ()

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._items)

    @property
    def items(self) -> tuple[RecentQuery, ...]:
        return self._items

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def roleNames(self) -> dict[int, QByteArray]:
        return {int(role): name for role, name in self._ROLE_NAMES.items()}

    def data(self, index: QModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        try:
            query_role = RecentQueryRole(role)
        except ValueError:
            return None
        values = {
            RecentQueryRole.IDENTIFIER: item.id,
            RecentQueryRole.DISPLAY_TEXT: item.display_text,
            RecentQueryRole.RATING: _RATING_TO_UI.get(item.rating, "all"),
            RecentQueryRole.PAGE_SIZE: item.page_size,
            RecentQueryRole.RESTORE_PAGE: item.restore_page,
            RecentQueryRole.SUMMARY: (
                f"{item.display_text} · 第 {item.restore_page} 页 · "
                f"每页 {item.page_size}"
            ),
        }
        return values[query_role]

    def replace(self, items: tuple[RecentQuery, ...]) -> None:
        self.beginResetModel()
        self._items = items
        self.endResetModel()
        self.countChanged.emit()

    def at(self, row: int) -> RecentQuery | None:
        if not isinstance(row, int) or isinstance(row, bool):
            return None
        return self._items[row] if 0 <= row < len(self._items) else None


@dataclass(frozen=True, slots=True)
class _RestorePlan:
    history_id: int
    request_id: int
    generation: int


@dataclass(frozen=True, slots=True)
class _AcceptedQuery:
    history_id: int
    preserve_gallery_position: bool


class BrowserSessionController(QObject):
    """Coordinate persistence without allowing late saves or restores to revive state."""

    controlsRequested = Signal(str, str, int)
    historyChanged = Signal()
    selectionChanged = Signal()
    noticeTextChanged = Signal()
    galleryRestoreReady = Signal()

    def __init__(
        self,
        repository: HistoryRepository,
        gallery: DanbooruGalleryController,
        settings: AppSettings,
        screens: Sequence[ScreenGeometry],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._repository = repository
        self._gallery = gallery
        self._settings = settings
        self._history_model = RecentQueryListModel(self)
        self._notice_text = ""
        self._closed = False
        self._clearing_history = False
        self._state_generation = 0
        self._restore_generation = 0
        self._restore_plan: _RestorePlan | None = None
        self._restore_ready: RecentQuery | None = None
        self._restore_application: tuple[int, int] | None = None
        self._active_restore_request_id: int | None = None
        self._accepted_history: dict[int, _AcceptedQuery] = {}
        self._latest_accepted_request_id = 0
        self._history_cleared_through_request_id = 0
        self._restore_invalidated_through_request_id = 0
        self._pending_history_id: int | None = None
        self._active_history_id: int | None = None
        self._displayed_model_revision = gallery.postsModel.revision
        self._gallery_position: GalleryPosition | None = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SESSION_SAVE_DELAY_MS)
        self._save_timer.timeout.connect(self._save_timeout)
        self._scheduled_generation = 0

        try:
            browser_session = repository.load_session()
            latest = repository.latest_query()
            queries = repository.list_queries()
        except HistoryError as exc:
            browser_session = None
            latest = None
            queries = ()
            self._set_notice(f"历史记录读取失败：{exc}")
        self._history_model.replace(queries)
        if browser_session is not None:
            self._active_history_id = browser_session.active_query_id
            saved_geometry = browser_session.geometry
            self._is_maximized = browser_session.is_maximized
        else:
            saved_geometry = None
            self._is_maximized = False

        if saved_geometry is None:
            requested_geometry = WindowGeometry(1_000_000, 1_000_000, 1280, 820)
        else:
            requested_geometry = WindowGeometry(
                saved_geometry.x,
                saved_geometry.y,
                saved_geometry.width,
                saved_geometry.height,
            )
        self._normal_geometry = restore_window_geometry(requested_geometry, screens)

        if latest is None:
            self._initial_tags = "landscape"
            self._initial_rating = "all"
            self._initial_page_size = settings.page_size
        else:
            self._pending_history_id = latest.id
            self._initial_tags = latest.display_text
            self._initial_rating = _RATING_TO_UI.get(latest.rating, "all")
            self._initial_page_size = latest.page_size

        gallery.requestAccepted.connect(self._request_accepted)
        gallery.pageDisplayed.connect(self._page_displayed)
        gallery.pageRequestCompleted.connect(self._page_completed)

    @Property(QObject, constant=True)
    def historyModel(self) -> QObject:
        return self._history_model

    @Property(str, constant=True)
    def initialTags(self) -> str:
        return self._initial_tags

    @Property(str, constant=True)
    def initialRating(self) -> str:
        return self._initial_rating

    @Property(int, constant=True)
    def initialPageSize(self) -> int:
        return self._initial_page_size

    @Property(int, constant=True)
    def initialWindowX(self) -> int:
        return self._normal_geometry.x

    @Property(int, constant=True)
    def initialWindowY(self) -> int:
        return self._normal_geometry.y

    @Property(int, constant=True)
    def initialWindowWidth(self) -> int:
        return self._normal_geometry.width

    @Property(int, constant=True)
    def initialWindowHeight(self) -> int:
        return self._normal_geometry.height

    @Property(int, constant=True)
    def minimumWindowWidth(self) -> int:
        return min(MINIMUM_WINDOW_WIDTH, self._normal_geometry.width)

    @Property(int, constant=True)
    def minimumWindowHeight(self) -> int:
        return min(MINIMUM_WINDOW_HEIGHT, self._normal_geometry.height)

    @Property(bool, constant=True)
    def startMaximized(self) -> bool:
        return self._is_maximized

    @Property(bool, notify=selectionChanged)
    def canRestoreSelected(self) -> bool:
        return self._pending_history_id is not None and not self._closed

    @Property(str, notify=noticeTextChanged)
    def noticeText(self) -> str:
        return self._notice_text

    @Slot(int)
    def selectHistory(self, row: int) -> None:
        if self._closed:
            return
        query = self._history_model.at(row)
        if query is None:
            return
        expected_generation = self._restore_generation + 1
        self._invalidate_restore()
        if self._closed or self._restore_generation != expected_generation:
            return
        self._pending_history_id = query.id
        selection_generation = self._restore_generation
        self.selectionChanged.emit()
        if (
            self._closed
            or self._pending_history_id != query.id
            or self._restore_generation != selection_generation
        ):
            return
        self.controlsRequested.emit(
            query.display_text,
            _RATING_TO_UI.get(query.rating, "all"),
            query.page_size,
        )

    @Slot()
    def controlsEdited(self) -> None:
        if self._closed:
            return
        self._pending_history_id = None
        self._invalidate_restore()
        self.selectionChanged.emit()

    @Slot()
    def restoreSelected(self) -> None:
        if self._closed or self._pending_history_id is None:
            return
        try:
            query = self._repository.get_query(self._pending_history_id)
        except HistoryError as exc:
            self._set_notice(f"恢复查询读取失败：{exc}")
            return
        if query is None:
            self._pending_history_id = None
            self.selectionChanged.emit()
            self._refresh_history()
            return
        expected_generation = self._restore_generation + 1
        self._invalidate_restore()
        if (
            self._closed
            or self._restore_generation != expected_generation
            or self._pending_history_id != query.id
        ):
            return
        self._gallery.restoreSearch(
            query.id,
            query.display_text,
            query.rating,
            query.page_size,
            query.restore_page,
        )

    @Slot()
    def cancelRestore(self) -> None:
        self._invalidate_restore()

    @Slot()
    def clearHistory(self) -> None:
        if self._closed or self._clearing_history:
            return
        self._clearing_history = True
        try:
            self._repository.clear_history()
        except HistoryError as exc:
            self._clearing_history = False
            self._set_notice(f"清理历史失败：{exc}")
            return
        self._save_timer.stop()
        self._state_generation += 1
        self._scheduled_generation = self._state_generation
        self._history_cleared_through_request_id = max(
            self._history_cleared_through_request_id,
            self._gallery.request_generation,
        )
        self._invalidate_restore()
        self._accepted_history.clear()
        self._active_history_id = None
        self._pending_history_id = None
        self._gallery_position = None
        self._history_model.replace(())
        self._history_cleared_through_request_id = max(
            self._history_cleared_through_request_id,
            self._gallery.request_generation,
        )
        self._clearing_history = False
        self.historyChanged.emit()
        self.selectionChanged.emit()

    @Slot(int, int, int, int, bool, bool)
    def updateWindowState(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        maximized: bool,
        minimized: bool,
    ) -> None:
        if self._closed or minimized:
            return
        self._is_maximized = bool(maximized)
        if not maximized and width > 0 and height > 0:
            self._normal_geometry = WindowGeometry(x, y, width, height)
        self._schedule_save()

    @Slot(int, int, float, float)
    @Slot(int, int, float, float, int)
    def updateGalleryPosition(
        self,
        post_id: int,
        index: int,
        anchor_offset: float,
        scroll_position: float,
        model_revision: int = -1,
    ) -> None:
        if model_revision == -1:
            model_revision = self._displayed_model_revision
        if (
            self._closed
            or self._active_history_id is None
            or model_revision != self._displayed_model_revision
        ):
            return
        if not math.isfinite(anchor_offset) or not math.isfinite(scroll_position):
            return
        if post_id < 1 or index < 0:
            position = GalleryPosition(
                query_id=self._active_history_id,
                anchor_post_id=None,
                anchor_index=None,
                anchor_offset=None,
                scroll_position=scroll_position,
            )
        else:
            position = GalleryPosition(
                query_id=self._active_history_id,
                anchor_post_id=post_id,
                anchor_index=index,
                anchor_offset=anchor_offset,
                scroll_position=scroll_position,
            )
        self._gallery_position = position
        self._schedule_save()

    @Slot(float, float, float, result="QVariantMap")
    def takeGalleryRestorePlan(
        self,
        origin_y: float,
        content_height: float,
        viewport_height: float,
    ) -> dict[str, object]:
        query = self._restore_ready
        self._restore_ready = None
        if self._closed or query is None:
            return {"available": False}
        token = self._restore_generation
        model_revision = self._gallery.postsModel.revision
        self._restore_application = (token, model_revision)
        maximum = max(origin_y, origin_y + content_height - viewport_height)
        absolute = max(origin_y, min(query.scroll_position, maximum))
        index = -1
        if query.anchor_post_id is not None:
            index = self._gallery.postsModel.row_for_post_id(query.anchor_post_id)
        if index < 0 and query.anchor_index is not None:
            count = self._gallery.postsModel.count
            if 0 <= query.anchor_index < count:
                index = query.anchor_index
        return {
            "available": True,
            "index": index,
            "anchorOffset": query.anchor_offset or 0.0,
            "absoluteContentY": absolute,
            "hasAnchor": index >= 0 and query.anchor_offset is not None,
            "token": token,
            "modelRevision": model_revision,
        }

    @Slot(int, int, result=bool)
    def galleryRestorePlanCurrent(self, token: int, model_revision: int) -> bool:
        return (
            not self._closed
            and self._restore_application == (token, model_revision)
            and token == self._restore_generation
            and model_revision == self._gallery.postsModel.revision
        )

    @Slot(int)
    def finishGalleryRestorePlan(self, token: int) -> None:
        active = self._restore_application
        if active is not None and active[0] == token:
            self._restore_application = None

    def save_final_state(self) -> None:
        """Persist one final transaction; callers must continue shutdown on failure."""

        if self._closed:
            return
        self._save_timer.stop()
        self._save_state(self._state_generation)

    def begin_shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._save_timer.stop()
        self._save_state(self._state_generation, allow_closed=True)
        self._state_generation += 1
        self._invalidate_restore()

    @Slot(object)
    def _request_accepted(self, event: object) -> None:
        if self._closed or not isinstance(event, GalleryRequestAccepted):
            return
        if self._clearing_history:
            self._history_cleared_through_request_id = max(
                self._history_cleared_through_request_id,
                event.request_id,
            )
            self._gallery.cancel_request(event.request_id)
            return
        signal_generation = self._state_generation
        is_latest = event.request_id > self._latest_accepted_request_id
        if is_latest:
            self._latest_accepted_request_id = event.request_id
        if event.request_id <= self._history_cleared_through_request_id:
            self._gallery.cancel_request(event.request_id)
            return
        if is_latest and event.origin == GalleryRequestOrigin.SEARCH:
            self._invalidate_restore()
            self._pending_history_id = None
        restore_is_current = (
            event.request_id > self._restore_invalidated_through_request_id
        )
        is_restore_request = (
            event.restore_history_id is not None
            and event.origin
            in (GalleryRequestOrigin.RESTORE, GalleryRequestOrigin.RETRY)
        )
        if is_restore_request and (not is_latest or not restore_is_current):
            self._accepted_history.pop(event.request_id, None)
            self._gallery.cancel_request(event.request_id)
            return
        if (
            is_latest
            and restore_is_current
            and event.origin == GalleryRequestOrigin.RESTORE
            and event.restore_history_id is not None
        ):
            self._active_restore_request_id = event.request_id
            self._restore_plan = _RestorePlan(
                event.restore_history_id,
                event.request_id,
                self._restore_generation,
            )
        elif (
            is_latest
            and restore_is_current
            and event.origin == GalleryRequestOrigin.RETRY
            and self._restore_plan is not None
            and event.restore_history_id == self._restore_plan.history_id
        ):
            self._active_restore_request_id = event.request_id
            self._restore_plan = _RestorePlan(
                self._restore_plan.history_id,
                event.request_id,
                self._restore_plan.generation,
            )
        try:
            query = self._repository.record_accepted_query(
                display_text=event.display_text,
                normalized_tags=event.spec.tags,
                rating=_RATING_TO_STORAGE[event.spec.rating.value],
                page_size=event.spec.limit,
                page=event.spec.page,
                count_tag_usage=event.counts_as_query_use,
                used_at=event.accepted_at,
            )
        except (HistoryError, KeyError, TypeError, ValueError) as exc:
            self._set_notice(f"查询历史保存失败：{exc}")
            if (
                is_latest
                and event.origin == GalleryRequestOrigin.SEARCH
                and not self._closed
            ):
                self.selectionChanged.emit()
            return
        if query is None:
            if self._active_restore_request_id == event.request_id:
                self._invalidate_restore()
            if is_latest and event.origin == GalleryRequestOrigin.SEARCH:
                self.selectionChanged.emit()
                if self._closed or signal_generation != self._state_generation:
                    return
            self._refresh_history()
            return
        self._accepted_history[event.request_id] = _AcceptedQuery(
            query.id,
            event.preserve_gallery_position,
        )
        if (
            self._restore_plan is not None
            and self._restore_plan.request_id == event.request_id
        ):
            self._restore_plan = _RestorePlan(
                query.id,
                event.request_id,
                self._restore_plan.generation,
            )
        if is_latest and event.origin == GalleryRequestOrigin.SEARCH:
            self.selectionChanged.emit()
            if self._closed or signal_generation != self._state_generation:
                return
        self._refresh_history()

    @Slot(object)
    def _page_displayed(self, outcome: object) -> None:
        if self._closed or not isinstance(outcome, GalleryPageOutcome):
            return
        request_id = outcome.request_id
        if request_id is None:
            return
        accepted = self._accepted_history.pop(request_id, None)
        if accepted is None:
            return
        query_id = accepted.history_id
        try:
            if not self._repository.mark_query_committed(
                query_id,
                outcome.page,
                preserve_position=accepted.preserve_gallery_position,
            ):
                if plan := self._restore_plan:
                    if plan.request_id == request_id:
                        self._invalidate_restore()
                return
            query = self._repository.get_query(query_id)
        except HistoryError as exc:
            self._set_notice(f"画廊状态保存失败：{exc}")
            if plan := self._restore_plan:
                if plan.request_id == request_id:
                    self._invalidate_restore()
            return
        self._active_history_id = query_id
        self._displayed_model_revision = self._gallery.postsModel.revision
        self._gallery_position = None
        plan = self._restore_plan
        if (
            query is not None
            and plan is not None
            and plan.request_id == request_id
            and plan.generation == self._restore_generation
        ):
            self._active_restore_request_id = None
            self._restore_ready = query
            self._restore_plan = None
            restore_generation = self._restore_generation
            self.galleryRestoreReady.emit()
            if (
                self._closed
                or restore_generation != self._restore_generation
            ):
                return
        self._refresh_history()

    @Slot(object)
    def _page_completed(self, outcome: object) -> None:
        if self._closed or not isinstance(outcome, GalleryPageOutcome):
            return
        request_id = outcome.request_id
        if request_id is None:
            return
        self._accepted_history.pop(request_id, None)
        plan = self._restore_plan
        if outcome.state == RequestState.CANCELLED:
            if plan is not None and plan.request_id == request_id:
                self._active_restore_request_id = None
                self._invalidate_restore()
            return
        if outcome.state == RequestState.EMPTY:
            if plan is not None and plan.request_id == request_id:
                self._active_restore_request_id = None
                self._invalidate_restore()
            return

    @Slot()
    def _save_timeout(self) -> None:
        self._save_state(self._scheduled_generation)

    def _schedule_save(self) -> None:
        self._scheduled_generation = self._state_generation
        self._save_timer.start()

    def _save_state(self, generation: int, *, allow_closed: bool = False) -> None:
        if (
            (self._closed and not allow_closed)
            or generation != self._state_generation
        ):
            return
        geometry = StoredWindowGeometry(
            self._normal_geometry.x,
            self._normal_geometry.y,
            self._normal_geometry.width,
            self._normal_geometry.height,
        )
        try:
            saved = self._repository.save_browser_state(
                geometry=geometry,
                is_maximized=self._is_maximized,
                active_query_id=self._active_history_id,
                gallery_position=self._gallery_position,
            )
        except HistoryError as exc:
            self._set_notice(f"会话状态保存失败：{exc}")
            return
        if not saved and self._gallery_position is not None:
            self._active_history_id = None
            self._gallery_position = None

    def _invalidate_restore(self) -> None:
        request_id = self._active_restore_request_id
        self._active_restore_request_id = None
        if request_id is not None:
            self._accepted_history.pop(request_id, None)
        self._restore_generation += 1
        self._restore_invalidated_through_request_id = max(
            self._restore_invalidated_through_request_id,
            self._gallery.request_generation,
            request_id or 0,
        )
        self._restore_plan = None
        self._restore_ready = None
        self._restore_application = None
        if request_id is not None:
            self._gallery.cancel_request(request_id)

    def _refresh_history(self) -> None:
        try:
            self._history_model.replace(self._repository.list_queries())
        except HistoryError as exc:
            self._set_notice(f"查询历史读取失败：{exc}")
            return
        self.historyChanged.emit()

    def _set_notice(self, text: str) -> None:
        if text == self._notice_text:
            return
        self._notice_text = text
        self.noticeTextChanged.emit()
