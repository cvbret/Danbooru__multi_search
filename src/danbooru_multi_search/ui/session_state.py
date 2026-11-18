"""QML-facing recent-query, restore-plan, and debounced session coordination."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import TYPE_CHECKING, Callable, Sequence

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

from danbooru_multi_search.app.cache_cleanup import delete_history
from danbooru_multi_search.ui.query_controller import DanbooruM2QueryController
from danbooru_multi_search.danbooru.query_events import (
    GalleryQueryAccepted,
    GalleryQueryOrigin,
    GalleryResultEvent,
    GalleryViewDefinitionChanged,
)
if TYPE_CHECKING:
    from danbooru_multi_search.app.query_checkpoints import QueryCheckpointService

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
from danbooru_multi_search.ui.query_state import M2QueryBridgeController


SESSION_SAVE_DELAY_MS = 500


class RecentQueryRole(IntEnum):
    IDENTIFIER = int(Qt.ItemDataRole.UserRole) + 1
    HISTORY_KEY = IDENTIFIER + 18
    MODE_VALUE = IDENTIFIER + 6
    MODE_TEXT = IDENTIFIER + 7
    EXPRESSION_TEXT = IDENTIFIER + 8
    RATING_TEXT = IDENTIFIER + 9
    STATE_VALUE = IDENTIFIER + 10
    STATE_TEXT = IDENTIFIER + 11
    REASON_TEXT = IDENTIFIER + 12
    REMOTE_ROWS = IDENTIFIER + 13
    UNIQUE_CANDIDATES = IDENTIFIER + 14
    LOCAL_MATCHES = IDENTIFIER + 15
    VISIBLE_COUNT = IDENTIFIER + 16
    HAS_RESULT_SUMMARY = IDENTIFIER + 17


_MODE_TEXT = {
    "standard": "普通查询",
    "rank": "近期热门",
    "latest": "最新作品",
}
_RATING_TEXT = {
    "all": "全部分级",
    "general": "General",
    "sensitive": "Sensitive",
    "questionable": "Questionable",
    "explicit": "Explicit",
}
_RESULT_STATE_TEXT = {
    "idle": "尚无结果",
    "waiting": "等待队列",
    "running": "正在扫描",
    "retry_wait": "等待自动重试",
    "complete": "扫描完成",
    "partial_cancelled": "已取消并保留部分结果",
    "partial_limited": "有限扫描完成",
    "partial_failed": "部分结果请求失败",
    "failed": "查询失败",
}
_LIMIT_REASON_TEXT = {
    "candidate_limit": "已经达到候选数量上限",
    "page_limit": "已经达到分支页数上限",
    "time_limit": "已经达到活动时间上限",
}
_FAILURE_REASON_TEXT = {
    "authentication": "身份或访问权限失败",
    "rate_limited": "请求过于频繁",
    "http": "HTTP 请求失败",
    "timeout": "请求超时",
    "network": "网络连接失败",
    "invalid_response": "响应数据无效",
    "request_start": "请求无法启动",
    "queue_rejected": "任务队列暂时无法接受请求",
    "persistence": "检查点保存失败",
}


class RecentQueryListModel(QAbstractListModel):
    countChanged = Signal()

    _ROLE_NAMES = {
        RecentQueryRole.IDENTIFIER: QByteArray(b"historyId"),
        RecentQueryRole.MODE_VALUE: QByteArray(b"modeValue"),
        RecentQueryRole.HISTORY_KEY: QByteArray(b"historyKey"),
        RecentQueryRole.MODE_TEXT: QByteArray(b"modeText"),
        RecentQueryRole.EXPRESSION_TEXT: QByteArray(b"expressionText"),
        RecentQueryRole.RATING_TEXT: QByteArray(b"ratingText"),
        RecentQueryRole.STATE_VALUE: QByteArray(b"stateValue"),
        RecentQueryRole.STATE_TEXT: QByteArray(b"stateText"),
        RecentQueryRole.REASON_TEXT: QByteArray(b"reasonText"),
        RecentQueryRole.REMOTE_ROWS: QByteArray(b"remoteRows"),
        RecentQueryRole.UNIQUE_CANDIDATES: QByteArray(b"uniqueCandidates"),
        RecentQueryRole.LOCAL_MATCHES: QByteArray(b"localMatches"),
        RecentQueryRole.VISIBLE_COUNT: QByteArray(b"visibleCount"),
        RecentQueryRole.HAS_RESULT_SUMMARY: QByteArray(b"hasResultSummary"),
    }

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: tuple[RecentQuery, ...] = ()
        self._revision = 0

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._items)

    @property
    def items(self) -> tuple[RecentQuery, ...]:
        return self._items

    @property
    def revision(self) -> int:
        return self._revision

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
        definition = item.definition
        summary = item.result_summary
        mode_value = definition.mode.value
        expression_text = definition.normalized_expression
        if expression_text is None:
            expression_text = "全部作品" if mode_value == "rank" else "最新作品"
        state_value = summary.state.value if summary is not None else ""
        limit_reason = (
            summary.limit_reason.value
            if summary is not None and summary.limit_reason is not None
            else ""
        )
        failure_reason = (
            summary.failure_error_code.value
            if summary is not None and summary.failure_error_code is not None
            else ""
        )
        reason_text = _LIMIT_REASON_TEXT.get(limit_reason, "")
        if not reason_text:
            reason_text = _FAILURE_REASON_TEXT.get(failure_reason, "")
        values = {
            RecentQueryRole.IDENTIFIER: item.id,
            RecentQueryRole.MODE_VALUE: mode_value,
            RecentQueryRole.HISTORY_KEY: str(item.id),
            RecentQueryRole.MODE_TEXT: _MODE_TEXT.get(mode_value, mode_value),
            RecentQueryRole.EXPRESSION_TEXT: expression_text,
            RecentQueryRole.RATING_TEXT: _RATING_TEXT.get(
                definition.rating.value,
                definition.rating.value,
            ),
            RecentQueryRole.STATE_VALUE: state_value,
            RecentQueryRole.STATE_TEXT: (
                _RESULT_STATE_TEXT.get(state_value, state_value)
                if summary is not None
                else "尚无结果"
            ),
            RecentQueryRole.REASON_TEXT: reason_text,
            RecentQueryRole.REMOTE_ROWS: summary.remote_rows if summary else 0,
            RecentQueryRole.UNIQUE_CANDIDATES: (
                summary.unique_candidates if summary else 0
            ),
            RecentQueryRole.LOCAL_MATCHES: summary.local_matches if summary else 0,
            RecentQueryRole.VISIBLE_COUNT: summary.visible_count if summary else 0,
            RecentQueryRole.HAS_RESULT_SUMMARY: summary is not None,
        }
        return values[query_role]

    def replace(self, items: tuple[RecentQuery, ...]) -> None:
        self.beginResetModel()
        self._items = items
        self._revision += 1
        self.endResetModel()
        self.countChanged.emit()

    def at(self, row: int) -> RecentQuery | None:
        if not isinstance(row, int) or isinstance(row, bool):
            return None
        return self._items[row] if 0 <= row < len(self._items) else None


@dataclass(frozen=True, order=True, slots=True)
class _M2HistoryIdentity:
    operation_id: int
    execution_id: int
    view_generation: int


@dataclass(frozen=True, slots=True)
class _M2HistoryLink:
    history_id: int
    preserve_position: bool
    restore_kind: str


@dataclass(frozen=True, slots=True)
class _M2DisplayBinding:
    identity: _M2HistoryIdentity
    history_id: int
    model_revision: int


@dataclass(frozen=True, slots=True)
class _M2RestoreToken:
    generation: int
    history_id: int
    source_query: RecentQuery
    kind: str
    operation_id: int | None = None
    execution_id: int | None = None
    view_generation: int | None = None
    model_revision: int | None = None


class BrowserSessionController(QObject):
    """Coordinate persistence without allowing late saves or restores to revive state."""

    historyChanged = Signal()
    selectionChanged = Signal()
    noticeTextChanged = Signal()
    galleryRestoreReady = Signal()
    activeGalleryChanged = Signal()

    def __init__(
        self,
        repository: HistoryRepository,
        screens: Sequence[ScreenGeometry],
        parent: QObject | None = None,
        *,
        m2_controller: DanbooruM2QueryController,
        query_bridge: M2QueryBridgeController,
        checkpoint_service: QueryCheckpointService | None = None,
    ) -> None:
        super().__init__(parent)
        self._repository = repository
        self._m2_controller = m2_controller
        self._query_bridge = query_bridge
        self._checkpoint_service = checkpoint_service
        self._posts_model = m2_controller.posts_model
        self._history_model = RecentQueryListModel(self)
        self._notice_text = ""
        self._closed = False
        self._events_detached = False
        self._clearing_history = False
        self._state_generation = 0
        self._restore_generation = 0
        self._restore_ready: RecentQuery | None = None
        self._restore_application: tuple[int, int] | None = None
        self._pending_history_id: int | None = None
        self._active_history_id: int | None = None
        self._displayed_model_revision = self._posts_model.revision
        self._gallery_position: GalleryPosition | None = None
        self._restore_ready_revision: int | None = None
        self._restore_ready_fallback_index = True
        self._restore_ready_fallback_scroll = True
        self._m2_history_epoch = 0
        self._m2_clear_cutoff: _M2HistoryIdentity | None = None
        self._m2_history_by_identity: dict[
            _M2HistoryIdentity, _M2HistoryLink
        ] = {}
        self._m2_pending_views: dict[
            _M2HistoryIdentity, GalleryViewDefinitionChanged
        ] = {}
        self._m2_pending_results: dict[
            _M2HistoryIdentity, GalleryResultEvent
        ] = {}
        self._m2_rejected_history_identities: set[_M2HistoryIdentity] = set()
        self._m2_committed_identities: set[_M2HistoryIdentity] = set()
        self._m2_display_binding: _M2DisplayBinding | None = None
        self._m2_restore_token: _M2RestoreToken | None = None
        self._m2_observed_model_identity: tuple[int | None, int] = (
            getattr(self._posts_model, "display_operation_id", None),
            self._posts_model.revision,
        )
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
        # Startup keeps saved queries selectable without fetching remote rows.
        # No history identity owns the empty model until a query commits.
        self._active_history_id = None

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

        if latest is not None:
            self._pending_history_id = latest.id
            if not self._query_bridge.loadDraftDefinition(latest.definition):
                self._pending_history_id = None

        draft_edited = getattr(self._query_bridge, "draftEdited", None)
        if draft_edited is not None:
            draft_edited.connect(self.controlsEdited)
        view_preparing = getattr(self._query_bridge, "viewUpdatePreparing", None)
        if view_preparing is not None:
            view_preparing.connect(self._m2_view_preparing)
        view_aborted = getattr(self._query_bridge, "viewUpdateAborted", None)
        if view_aborted is not None:
            view_aborted.connect(self._m2_view_aborted)
        self._m2_controller.queryAccepted.connect(self._m2_query_accepted)
        self._m2_controller.viewDefinitionChanged.connect(self._m2_view_changed)
        self._m2_controller.resultSummaryChanged.connect(self._m2_result_changed)
        self._posts_model.revisionChanged.connect(self._m2_model_revision_changed)

    @Property(QObject, constant=True)
    def historyModel(self) -> QObject:
        return self._history_model

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

    @Property(int, notify=activeGalleryChanged)
    def activeHistoryId(self) -> int:
        return self._active_history_id or 0

    @Property(int, notify=activeGalleryChanged)
    def activeGalleryOperationId(self) -> int:
        binding = self._m2_display_binding
        return binding.identity.operation_id if binding is not None else 0

    @Property(int, notify=activeGalleryChanged)
    def activeGalleryExecutionId(self) -> int:
        binding = self._m2_display_binding
        return binding.identity.execution_id if binding is not None else 0

    @Property(int, notify=activeGalleryChanged)
    def activeGalleryViewGeneration(self) -> int:
        binding = self._m2_display_binding
        return binding.identity.view_generation if binding is not None else 0

    @Property(int, notify=activeGalleryChanged)
    def activeGalleryModelRevision(self) -> int:
        binding = self._m2_display_binding
        return binding.model_revision if binding is not None else 0

    @Slot(int)
    def selectHistory(self, row: int) -> None:
        self._select_m2_history(row)

    @Slot()
    def controlsEdited(self) -> None:
        if self._closed:
            return
        self._pending_history_id = None
        self._invalidate_m2_restore(cancel=True)
        self.selectionChanged.emit()

    @Slot()
    def restoreSelected(self) -> None:
        self._restore_selected_m2()

    @Slot()
    def cancelRestore(self) -> None:
        self._invalidate_m2_restore(cancel=True)

    @Slot(result=bool)
    def cancelM2Query(self) -> bool:
        """Invalidate a pending history restore and cancel the current query."""

        if self._closed:
            return False
        self._invalidate_m2_restore(cancel=False)
        return bool(self._query_bridge.cancel())

    @Slot()
    def clearHistory(
        self, expected=None, on_cleared: Callable[[], None] | None = None,
        delete_operation: Callable[[], None] | None = None,
    ) -> bool:
        return bool(self._clear_m2_history(expected, on_cleared, delete_operation))

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

    @Slot(int, int, int, int, int, float, float)
    @Slot(int, int, int, int, int, float, float, int)
    def updateM2GalleryPosition(
        self,
        operation_id: int,
        execution_id: int,
        view_generation: int,
        post_id: int,
        index: int,
        anchor_offset: float,
        scroll_position: float,
        model_revision: int = -1,
    ) -> None:
        binding = self._m2_display_binding
        if binding is None or binding.identity != _M2HistoryIdentity(
            operation_id,
            execution_id,
            view_generation,
        ):
            return
        if model_revision == -1:
            model_revision = binding.model_revision
        if model_revision != binding.model_revision:
            return
        self._store_gallery_position(
            post_id,
            index,
            anchor_offset,
            scroll_position,
            model_revision,
        )

    def _store_gallery_position(
        self,
        post_id: int,
        index: int,
        anchor_offset: float,
        scroll_position: float,
        model_revision: int,
    ) -> None:
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
        ready_revision = self._restore_ready_revision
        fallback_index = self._restore_ready_fallback_index
        fallback_scroll = self._restore_ready_fallback_scroll
        self._restore_ready = None
        self._restore_ready_revision = None
        if (
            self._closed
            or query is None
            or (
                ready_revision is not None
                and ready_revision != self._posts_model.revision
            )
        ):
            return {"available": False}
        token = self._restore_generation
        model_revision = self._posts_model.revision
        self._restore_application = (token, model_revision)
        maximum = max(origin_y, origin_y + content_height - viewport_height)
        absolute = max(origin_y, min(query.scroll_position, maximum))
        index = -1
        if query.anchor_post_id is not None:
            index = self._posts_model.row_for_post_id(query.anchor_post_id)
        if index < 0 and fallback_index and query.anchor_index is not None:
            count = self._posts_model.count
            if 0 <= query.anchor_index < count:
                index = query.anchor_index
        if index < 0 and not fallback_scroll:
            absolute = origin_y
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
            and model_revision == self._posts_model.revision
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
        self._invalidate_m2_restore(cancel=True)

    def detach_events(self) -> None:
        """Disconnect controller events after the final session save."""

        if self._events_detached:
            return
        self._events_detached = True
        connections = (
            (self._query_bridge.draftEdited, self.controlsEdited),
            (self._query_bridge.viewUpdatePreparing, self._m2_view_preparing),
            (self._query_bridge.viewUpdateAborted, self._m2_view_aborted),
            (self._m2_controller.queryAccepted, self._m2_query_accepted),
            (
                self._m2_controller.viewDefinitionChanged,
                self._m2_view_changed,
            ),
            (
                self._m2_controller.resultSummaryChanged,
                self._m2_result_changed,
            ),
            (
                self._posts_model.revisionChanged,
                self._m2_model_revision_changed,
            ),
        )
        for signal, slot in connections:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    @property
    def events_detached(self) -> bool:
        return self._events_detached

    def _select_m2_history(self, row: int) -> None:
        if self._closed:
            return
        query = self._history_model.at(row)
        if query is None:
            return
        expected_generation = self._restore_generation + 1
        self._invalidate_m2_restore(cancel=True)
        if self._closed or self._restore_generation != expected_generation:
            return
        selection_generation = self._restore_generation
        self._pending_history_id = query.id
        self.selectionChanged.emit()
        if (
            self._closed
            or self._restore_generation != selection_generation
            or self._pending_history_id != query.id
        ):
            return
        assert self._query_bridge is not None
        if not self._query_bridge.loadDraftDefinition(query.definition):
            if (
                not self._closed
                and self._restore_generation == selection_generation
                and self._pending_history_id == query.id
            ):
                self._pending_history_id = None
                self.selectionChanged.emit()

    def resumeCheckpoint(self, checkpoint, history_id: int) -> bool:
        if self._closed or self._pending_history_id != history_id:
            return False
        return bool(self._restore_selected_m2(checkpoint))

    def _restore_selected_m2(self, checkpoint=None):
        if self._closed or self._pending_history_id is None:
            return
        selected_id = self._pending_history_id
        selection_generation = self._restore_generation
        try:
            query = self._repository.get_query(selected_id)
        except HistoryError as exc:
            self._set_notice(f"恢复查询读取失败：{exc}")
            return
        if (
            self._closed
            or self._pending_history_id != selected_id
            or self._restore_generation != selection_generation
        ):
            return
        if query is None:
            self._pending_history_id = None
            self.selectionChanged.emit()
            self._refresh_history()
            return

        assert self._query_bridge is not None
        loaded = getattr(
            self._query_bridge,
            "loaded_history_definition",
            getattr(self._query_bridge, "loaded_definition", None),
        )
        if loaded != query.definition:
            if not self._query_bridge.loadDraftDefinition(query.definition):
                return
            if (
                self._closed
                or self._pending_history_id != query.id
                or self._restore_generation != selection_generation
            ):
                return

        expected_generation = self._restore_generation + 1
        self._invalidate_m2_restore(cancel=True)
        if (
            self._closed
            or self._restore_generation != expected_generation
            or self._pending_history_id != query.id
        ):
            return
        generation = self._restore_generation
        token = _M2RestoreToken(
            generation=generation,
            history_id=query.id,
            source_query=query,
            kind="history",
        )
        self._m2_restore_token = token
        try:
            execute = getattr(
                self._query_bridge,
                "reexecute_loaded_history",
                None,
            )
            if execute is None:
                execute = self._query_bridge.reexecuteLoadedHistory
            returned = execute(query.display_text) if checkpoint is None else self._query_bridge.resumeCheckpoint(checkpoint)
        except Exception as exc:
            if self._m2_restore_token is token:
                self._invalidate_m2_restore(cancel=False)
            self._set_notice(f"历史查询重新执行失败：{exc}")
            return

        operation_id = (
            returned
            if isinstance(returned, int) and not isinstance(returned, bool)
            else None
        )
        controller = self._m2_controller
        assert controller is not None
        if self._m2_restore_token is not token:
            if (
                operation_id is not None
                and controller.operation_id == operation_id
                and not self._closed
            ):
                controller.cancel()
            return
        if operation_id is None:
            self._invalidate_m2_restore(cancel=False)
            return
        if (
            controller.operation_id is not None
            and controller.operation_id != operation_id
        ):
            self._invalidate_m2_restore(cancel=False)
            return
        current_identity: _M2HistoryIdentity | None = None
        if (
            controller.operation_id == operation_id
            and controller.execution_id is not None
        ):
            current_identity = _M2HistoryIdentity(
                operation_id,
                controller.execution_id,
                controller.view_generation,
            )
        if current_identity is not None:
            link = self._m2_history_by_identity.get(current_identity)
            if (
                self._m2_identity_cleared(current_identity)
                or current_identity in self._m2_rejected_history_identities
                or (
                    controller.gallery_operation_id == operation_id
                    and link is None
                )
                or (
                    link is not None
                    and link.history_id != token.history_id
                )
            ):
                self._invalidate_m2_restore(cancel=False)
                return
        bound = replace(token, operation_id=operation_id)
        if controller.operation_id == operation_id:
            bound = replace(
                bound,
                execution_id=controller.execution_id,
                view_generation=controller.view_generation,
            )
        self._m2_restore_token = bound
        self._finish_m2_restore_if_displayed()
        return True

    @Slot(object)
    def _m2_query_accepted(self, value: object) -> None:
        if self._closed or not isinstance(value, GalleryQueryAccepted):
            return
        identity = _M2HistoryIdentity(
            value.operation_id,
            value.execution_id,
            0,
        )
        if not self._m2_operation_current(identity) or self._m2_identity_cleared(
            identity
        ):
            return
        epoch = self._m2_history_epoch
        if value.origin in {
            GalleryQueryOrigin.STANDARD,
            GalleryQueryOrigin.RANK,
            GalleryQueryOrigin.LATEST,
        }:
            self._pending_history_id = None
            self._invalidate_m2_restore(cancel=True)
            self.selectionChanged.emit()
            if not self._m2_event_current(identity, epoch, allow_later_view=True):
                return
        elif value.origin == GalleryQueryOrigin.REFRESH:
            restore_generation = self._restore_generation
            source = self._m2_active_query_with_position()
            if not self._m2_event_current(
                identity,
                epoch,
                allow_later_view=True,
            ) or self._restore_generation != restore_generation:
                return
            self._invalidate_m2_restore(cancel=False)
            if source is not None:
                self._m2_restore_token = _M2RestoreToken(
                    generation=self._restore_generation,
                    history_id=source.id,
                    source_query=source,
                    kind="refresh",
                    operation_id=identity.operation_id,
                    execution_id=identity.execution_id,
                    view_generation=0,
                )
        try:
            query = self._checkpoint_service.record_accepted(value) if self._checkpoint_service is not None else self._repository.record_accepted_definition(
                definition=value.definition,
                display_text=value.display_text,
                page=1,
                count_tag_usage=value.count_tag_usage,
                used_at=value.accepted_at,
            )
        except (HistoryError, TypeError, ValueError) as exc:
            if self._checkpoint_service is not None:
                self._checkpoint_service.persistence_failed(exc)
            self._m2_remove_mapping(identity, clear_display=True)
            if value.origin == GalleryQueryOrigin.HISTORY:
                self._discard_unbound_m2_history_restore()
            self._set_notice(f"查询历史保存失败：{exc}")
            return
        if not self._m2_event_current(identity, epoch, allow_later_view=True):
            return
        if query is None:
            self._m2_remove_mapping(identity, clear_display=True)
            if value.origin == GalleryQueryOrigin.HISTORY:
                self._discard_unbound_m2_history_restore()
            self._refresh_m2_history(identity, epoch, allow_later_view=True)
            return
        token = self._m2_restore_token
        if (
            value.origin in {GalleryQueryOrigin.HISTORY, GalleryQueryOrigin.CHECKPOINT}
            and token is not None
            and token.kind == "history"
        ):
            kind = "history"
        elif (
            value.origin == GalleryQueryOrigin.REFRESH
            and token is not None
            and token.kind == "refresh"
            and token.operation_id == identity.operation_id
        ):
            kind = "refresh"
        else:
            kind = "new"
        self._m2_history_by_identity[identity] = _M2HistoryLink(
            history_id=query.id,
            preserve_position=kind in {"history", "refresh"},
            restore_kind=kind,
        )
        self._m2_rejected_history_identities.discard(identity)
        self._drain_m2_facts(value.operation_id, value.execution_id, epoch)
        if not self._m2_event_current(identity, epoch, allow_later_view=True):
            return
        if not self._refresh_m2_history(identity, epoch, allow_later_view=True):
            return
        self._finish_m2_restore_if_displayed()

    @Slot(int, int, int)
    def _m2_view_preparing(
        self,
        operation_id: int,
        execution_id: int,
        view_generation: int,
    ) -> None:
        source_identity = _M2HistoryIdentity(
            operation_id,
            execution_id,
            view_generation,
        )
        if not self._m2_event_current(
            source_identity,
            self._m2_history_epoch,
        ):
            return
        binding = self._m2_display_binding
        if binding is None or binding.identity != source_identity:
            return
        source_query = self._m2_query_with_position(binding.history_id)
        if source_query is None:
            return
        if (
            not self._m2_event_current(
                source_identity,
                self._m2_history_epoch,
            )
            or self._m2_display_binding != binding
        ):
            return
        self._invalidate_m2_restore(cancel=False)
        self._m2_restore_token = _M2RestoreToken(
            generation=self._restore_generation,
            history_id=source_query.id,
            source_query=source_query,
            kind="local_view",
            operation_id=operation_id,
            execution_id=execution_id,
            view_generation=view_generation + 1,
        )

    @Slot(int, int, int)
    def _m2_view_aborted(
        self,
        operation_id: int,
        execution_id: int,
        view_generation: int,
    ) -> None:
        token = self._m2_restore_token
        if (
            token is not None
            and token.kind == "local_view"
            and token.operation_id == operation_id
            and token.execution_id == execution_id
            and token.view_generation == view_generation + 1
        ):
            self._invalidate_m2_restore(cancel=False)

    @Slot(object)
    def _m2_view_changed(self, value: object) -> None:
        if self._closed or not isinstance(value, GalleryViewDefinitionChanged):
            return
        identity = _M2HistoryIdentity(
            value.operation_id,
            value.execution_id,
            value.view_generation,
        )
        if not self._m2_event_current(identity, self._m2_history_epoch):
            return
        self._m2_pending_views[identity] = value
        self._drain_m2_facts(
            value.operation_id,
            value.execution_id,
            self._m2_history_epoch,
        )
        source_identity = _M2HistoryIdentity(
            identity.operation_id,
            identity.execution_id,
            identity.view_generation - 1,
        )
        if (
            identity in self._m2_pending_views
            and (
                self._m2_identity_cleared(source_identity)
                or source_identity in self._m2_rejected_history_identities
            )
        ):
            self._m2_remove_mapping(
                identity,
                clear_display=value.applied_to_gallery,
            )

    @Slot(object)
    def _m2_result_changed(self, value: object) -> None:
        if self._closed or not isinstance(value, GalleryResultEvent):
            return
        identity = _M2HistoryIdentity(
            value.operation_id,
            value.execution_id,
            value.view_generation,
        )
        if not self._m2_event_current(identity, self._m2_history_epoch):
            return
        self._m2_pending_results[identity] = value
        self._drain_m2_facts(
            value.operation_id,
            value.execution_id,
            self._m2_history_epoch,
        )
        if (
            identity in self._m2_pending_results
            and identity in self._m2_rejected_history_identities
        ):
            self._m2_pending_results.pop(identity, None)
            if (
                value.gallery_operation_id == identity.operation_id
                and getattr(self._posts_model, "display_operation_id", None)
                == identity.operation_id
            ):
                self._clear_m2_active_display()

    @Slot()
    def _m2_model_revision_changed(self) -> None:
        if self._closed:
            return
        operation_id = getattr(self._posts_model, "display_operation_id", None)
        revision = self._posts_model.revision
        self._m2_observed_model_identity = (operation_id, revision)
        identity = self._m2_current_identity()
        if identity is None or operation_id != identity.operation_id:
            return
        link = self._m2_history_by_identity.get(identity)
        if link is not None:
            self._commit_m2_display(identity, link, revision)

    def _drain_m2_facts(
        self,
        operation_id: int,
        execution_id: int,
        epoch: int,
    ) -> None:
        if self._closed or epoch != self._m2_history_epoch:
            return
        while True:
            candidates = sorted(
                identity
                for identity in self._m2_pending_views
                if identity.operation_id == operation_id
                and identity.execution_id == execution_id
                and _M2HistoryIdentity(
                    operation_id,
                    execution_id,
                    identity.view_generation - 1,
                )
                in self._m2_history_by_identity
            )
            if not candidates:
                break
            identity = candidates[0]
            event = self._m2_pending_views.pop(identity)
            self._process_m2_view(identity, event, epoch)
            if self._closed or epoch != self._m2_history_epoch:
                return

        identity = self._m2_current_identity()
        if identity is not None:
            for stale in tuple(self._m2_pending_results):
                if (
                    stale.operation_id == operation_id
                    and stale.execution_id == execution_id
                    and stale.view_generation < identity.view_generation
                ):
                    self._m2_pending_results.pop(stale, None)
        if (
            identity is not None
            and identity.operation_id == operation_id
            and identity.execution_id == execution_id
            and identity in self._m2_pending_results
            and identity in self._m2_history_by_identity
        ):
            event = self._m2_pending_results.pop(identity)
            self._process_m2_result(identity, event, epoch)

    def _process_m2_view(
        self,
        identity: _M2HistoryIdentity,
        event: GalleryViewDefinitionChanged,
        epoch: int,
    ) -> None:
        if not self._m2_event_current(identity, epoch, allow_later_view=True):
            return
        source_identity = _M2HistoryIdentity(
            identity.operation_id,
            identity.execution_id,
            identity.view_generation - 1,
        )
        source = self._m2_history_by_identity.get(source_identity)
        if source is None:
            return
        token = self._m2_restore_token
        token_matches_target = (
            token is not None
            and token.kind == "local_view"
            and token.operation_id == identity.operation_id
            and token.execution_id == identity.execution_id
            and token.view_generation == identity.view_generation
        )
        if event.applied_to_gallery and not token_matches_target:
            source_query = self._m2_query_with_position(source.history_id)
            if not self._m2_event_current(
                identity,
                epoch,
                allow_later_view=True,
            ):
                return
            self._invalidate_m2_restore(cancel=False)
            if source_query is not None:
                self._m2_restore_token = _M2RestoreToken(
                    generation=self._restore_generation,
                    history_id=source_query.id,
                    source_query=source_query,
                    kind="local_view",
                    operation_id=identity.operation_id,
                    execution_id=identity.execution_id,
                    view_generation=identity.view_generation,
                )
                token_matches_target = True
        try:
            query = self._checkpoint_service.record_view(source.history_id, event) if self._checkpoint_service is not None else self._repository.record_view_definition(
                source_query_id=source.history_id,
                definition=event.definition,
            )
        except (HistoryError, TypeError, ValueError) as exc:
            if self._checkpoint_service is not None:
                self._checkpoint_service.persistence_failed(exc)
            self._m2_remove_mapping(
                identity,
                clear_display=event.applied_to_gallery,
            )
            self._set_notice(f"查询视图保存失败：{exc}")
            return
        if not self._m2_event_current(identity, epoch, allow_later_view=True):
            return
        if query is None:
            self._m2_remove_mapping(
                identity,
                clear_display=event.applied_to_gallery,
            )
            self._refresh_m2_history(identity, epoch, allow_later_view=True)
            return
        restore_kind = "local_view" if token_matches_target else "new"
        link = _M2HistoryLink(
            query.id,
            restore_kind == "local_view",
            restore_kind,
        )
        self._m2_history_by_identity[identity] = link
        self._m2_rejected_history_identities.discard(identity)
        current = self._m2_current_identity()
        if (
            event.applied_to_gallery
            and current == identity
            and getattr(self._posts_model, "display_operation_id", None)
            == identity.operation_id
        ):
            self._commit_m2_display(identity, link, self._posts_model.revision)
        self._refresh_m2_history(identity, epoch, allow_later_view=True)

    def _process_m2_result(
        self,
        identity: _M2HistoryIdentity,
        event: GalleryResultEvent,
        epoch: int,
    ) -> None:
        if not self._m2_event_current(identity, epoch):
            return
        link = self._m2_history_by_identity.get(identity)
        if link is None:
            return
        try:
            updated = self._checkpoint_service.record_result(link.history_id, event) if self._checkpoint_service is not None else self._repository.update_result_summary(
                link.history_id,
                event.summary,
            )
        except (HistoryError, TypeError, ValueError) as exc:
            if self._checkpoint_service is not None:
                self._checkpoint_service.persistence_failed(exc)
            self._m2_remove_mapping(identity, clear_display=True)
            self._set_notice(f"查询结果摘要保存失败：{exc}")
            return
        if not self._m2_event_current(identity, epoch):
            return
        if not updated:
            self._m2_remove_mapping(identity, clear_display=True)
            self._refresh_history()
            return
        if (
            event.gallery_operation_id == identity.operation_id
            and getattr(self._posts_model, "display_operation_id", None)
            == identity.operation_id
        ):
            self._commit_m2_display(
                identity,
                link,
                self._posts_model.revision,
            )
        self._refresh_m2_history(identity, epoch)

    def _commit_m2_display(
        self,
        identity: _M2HistoryIdentity,
        link: _M2HistoryLink,
        model_revision: int,
    ) -> None:
        epoch = self._m2_history_epoch
        if (
            not self._m2_event_current(identity, epoch)
            or getattr(self._posts_model, "display_operation_id", None)
            != identity.operation_id
            or self._m2_history_by_identity.get(identity) != link
        ):
            return
        current_binding = self._m2_display_binding
        if (
            current_binding is not None
            and current_binding.identity == identity
            and current_binding.history_id == link.history_id
        ):
            self._displayed_model_revision = model_revision
            self._m2_display_binding = replace(
                current_binding,
                model_revision=model_revision,
            )
            self.activeGalleryChanged.emit()
            if not self._m2_display_still_bound(
                identity,
                link,
                model_revision,
            ):
                return
            self._finish_m2_restore_if_displayed()
            return
        if identity not in self._m2_committed_identities:
            try:
                committed = self._repository.mark_query_committed(
                    link.history_id,
                    1,
                    preserve_position=link.preserve_position,
                )
            except (HistoryError, TypeError, ValueError) as exc:
                self._m2_remove_mapping(identity, clear_display=True)
                self._set_notice(f"画廊状态保存失败：{exc}")
                return
            if not self._m2_event_current(identity, epoch):
                return
            if not committed:
                self._m2_remove_mapping(identity, clear_display=True)
                self._refresh_history()
                return
            self._m2_committed_identities.add(identity)

        previous_history_id = self._active_history_id
        self._active_history_id = link.history_id
        self._displayed_model_revision = model_revision
        self._m2_display_binding = _M2DisplayBinding(
            identity,
            link.history_id,
            model_revision,
        )
        if link.restore_kind == "new":
            self._gallery_position = None
        elif (
            self._gallery_position is not None
            and self._gallery_position.query_id == previous_history_id
        ):
            self._gallery_position = replace(
                self._gallery_position,
                query_id=link.history_id,
            )
        self.activeGalleryChanged.emit()
        if not self._m2_display_still_bound(
            identity,
            link,
            model_revision,
        ):
            return
        self._finish_m2_restore_if_displayed()
        if link.restore_kind == "new" and self._m2_restore_token is None:
            try:
                query = self._repository.get_query(link.history_id)
            except HistoryError as exc:
                self._set_notice(f"画廊恢复状态读取失败：{exc}")
                return
            if query is not None and self._m2_display_still_bound(
                identity,
                link,
                model_revision,
            ):
                self._publish_m2_restore(
                    replace(
                        query,
                        anchor_post_id=None,
                        anchor_index=None,
                        anchor_offset=None,
                        scroll_position=0.0,
                    ),
                    model_revision,
                    fallback_index=False,
                    fallback_scroll=False,
                )

    def _m2_display_still_bound(
        self,
        identity: _M2HistoryIdentity,
        link: _M2HistoryLink,
        model_revision: int,
    ) -> bool:
        binding = self._m2_display_binding
        return (
            not self._closed
            and binding is not None
            and binding.identity == identity
            and binding.history_id == link.history_id
            and binding.model_revision == model_revision
            and self._m2_history_by_identity.get(identity) == link
            and getattr(self._posts_model, "display_operation_id", None)
            == identity.operation_id
            and self._posts_model.revision == model_revision
        )

    def _finish_m2_restore_if_displayed(self) -> None:
        token = self._m2_restore_token
        binding = self._m2_display_binding
        if token is None or binding is None:
            return
        if token.operation_id is None:
            return
        if (
            token.operation_id != binding.identity.operation_id
            or (
                token.execution_id is not None
                and token.execution_id != binding.identity.execution_id
            )
            or (
                token.view_generation is not None
                and token.view_generation != binding.identity.view_generation
            )
        ):
            return
        if token.generation != self._restore_generation:
            return
        self._m2_restore_token = None
        source = token.source_query
        fallback_index = token.kind == "history"
        fallback_scroll = token.kind == "history"
        self._publish_m2_restore(
            source,
            binding.model_revision,
            fallback_index=fallback_index,
            fallback_scroll=fallback_scroll,
        )

    def _publish_m2_restore(
        self,
        query: RecentQuery,
        model_revision: int,
        *,
        fallback_index: bool,
        fallback_scroll: bool,
    ) -> None:
        if self._closed:
            return
        generation = self._restore_generation
        self._restore_ready = query
        self._restore_ready_revision = model_revision
        self._restore_ready_fallback_index = fallback_index
        self._restore_ready_fallback_scroll = fallback_scroll
        self.galleryRestoreReady.emit()
        if (
            self._closed
            or generation != self._restore_generation
            or model_revision != self._posts_model.revision
        ):
            self._restore_ready = None
            self._restore_ready_revision = None

    def _m2_active_query_with_position(self) -> RecentQuery | None:
        if self._active_history_id is None:
            return None
        return self._m2_query_with_position(self._active_history_id)

    def _m2_query_with_position(self, history_id: int) -> RecentQuery | None:
        try:
            query = self._repository.get_query(history_id)
        except HistoryError as exc:
            self._set_notice(f"画廊位置读取失败：{exc}")
            return None
        position = self._gallery_position
        if query is None or position is None or position.query_id != history_id:
            return query
        return replace(
            query,
            anchor_post_id=position.anchor_post_id,
            anchor_index=position.anchor_index,
            anchor_offset=position.anchor_offset,
            scroll_position=position.scroll_position,
        )

    def _m2_current_identity(self) -> _M2HistoryIdentity | None:
        controller = self._m2_controller
        if controller is None:
            return None
        operation_id = controller.operation_id
        execution_id = controller.execution_id
        if operation_id is None or execution_id is None:
            return None
        return _M2HistoryIdentity(
            operation_id,
            execution_id,
            controller.view_generation,
        )

    def _m2_operation_current(self, identity: _M2HistoryIdentity) -> bool:
        controller = self._m2_controller
        return (
            not self._closed
            and controller is not None
            and controller.operation_id == identity.operation_id
            and controller.execution_id == identity.execution_id
        )

    def _m2_identity_cleared(self, identity: _M2HistoryIdentity) -> bool:
        cutoff = self._m2_clear_cutoff
        return cutoff is not None and identity <= cutoff

    def _m2_event_current(
        self,
        identity: _M2HistoryIdentity,
        epoch: int,
        *,
        allow_later_view: bool = False,
    ) -> bool:
        if (
            self._closed
            or epoch != self._m2_history_epoch
            or self._m2_identity_cleared(identity)
            or not self._m2_operation_current(identity)
        ):
            return False
        assert self._m2_controller is not None
        current_view = self._m2_controller.view_generation
        return (
            current_view >= identity.view_generation
            if allow_later_view
            else current_view == identity.view_generation
        )

    def _refresh_m2_history(
        self,
        identity: _M2HistoryIdentity,
        epoch: int,
        *,
        allow_later_view: bool = False,
    ) -> bool:
        try:
            items = self._repository.list_queries()
        except HistoryError as exc:
            self._set_notice(f"查询历史读取失败：{exc}")
            return False
        if not self._m2_event_current(
            identity,
            epoch,
            allow_later_view=allow_later_view,
        ):
            return False
        self._history_model.replace(items)
        if not self._m2_event_current(
            identity,
            epoch,
            allow_later_view=allow_later_view,
        ):
            return False
        self.historyChanged.emit()
        return self._m2_event_current(
            identity,
            epoch,
            allow_later_view=allow_later_view,
        )

    def _m2_remove_mapping(
        self,
        identity: _M2HistoryIdentity,
        *,
        clear_display: bool,
    ) -> None:
        affected = {
            candidate
            for candidate in (
                set(self._m2_history_by_identity)
                | set(self._m2_pending_views)
                | set(self._m2_pending_results)
                | {identity}
            )
            if candidate.operation_id == identity.operation_id
            and candidate.execution_id == identity.execution_id
            and candidate.view_generation >= identity.view_generation
        }
        for candidate in affected:
            self._m2_history_by_identity.pop(candidate, None)
            self._m2_pending_views.pop(candidate, None)
            self._m2_pending_results.pop(candidate, None)
            self._m2_committed_identities.discard(candidate)
            self._m2_rejected_history_identities.add(candidate)
        token = self._m2_restore_token
        if (
            token is not None
            and token.operation_id == identity.operation_id
            and (
                token.execution_id is None
                or token.execution_id == identity.execution_id
            )
            and (
                token.view_generation is None
                or token.view_generation >= identity.view_generation
            )
        ):
            self._invalidate_m2_restore(cancel=False)
        if not clear_display:
            return
        displayed = self._m2_display_binding
        model_operation = getattr(self._posts_model, "display_operation_id", None)
        current = self._m2_current_identity()
        if (
            (displayed is not None and displayed.identity in affected)
            or (
                model_operation == identity.operation_id
                and current is not None
                and current.operation_id == identity.operation_id
                and current.execution_id == identity.execution_id
                and current.view_generation >= identity.view_generation
            )
        ):
            self._clear_m2_active_display()

    def _discard_unbound_m2_history_restore(self) -> None:
        token = self._m2_restore_token
        if (
            token is not None
            and token.kind == "history"
            and token.operation_id is None
        ):
            self._invalidate_m2_restore(cancel=False)

    def _clear_m2_active_display(self) -> None:
        self._active_history_id = None
        self._gallery_position = None
        self._m2_display_binding = None
        try:
            self._repository.set_active_query(None)
        except HistoryError as exc:
            self._set_notice(f"活动画廊状态清除失败：{exc}")
        self.activeGalleryChanged.emit()

    def _invalidate_m2_restore(self, *, cancel: bool) -> None:
        token = self._m2_restore_token
        self._m2_restore_token = None
        self._restore_generation += 1
        self._restore_ready = None
        self._restore_ready_revision = None
        self._restore_application = None
        if not cancel:
            return
        self._cancel_m2_restore_token(token)

    def _cancel_m2_restore_token(
        self,
        token: _M2RestoreToken | None,
    ) -> None:
        if token is None or token.operation_id is None:
            return
        controller = self._m2_controller
        if controller is None:
            return
        if (
            controller.operation_id == token.operation_id
            and (
                token.execution_id is None
                or controller.execution_id == token.execution_id
            )
            and (
                token.view_generation is None
                or controller.view_generation == token.view_generation
            )
        ):
            controller.cancel()

    def _clear_m2_history(
        self, expected=None, on_cleared: Callable[[], None] | None = None,
        delete_operation: Callable[[], None] | None = None,
    ):
        if self._closed or self._clearing_history:
            return
        self._clearing_history = True
        previous_cutoff = self._m2_clear_cutoff
        previous_epoch = self._m2_history_epoch
        previous_pending_history_id = self._pending_history_id
        previous_restore_generation = self._restore_generation
        previous_restore_token = self._m2_restore_token
        previous_restore_ready = self._restore_ready
        previous_restore_ready_revision = self._restore_ready_revision
        previous_restore_application = self._restore_application
        previous_fallback_index = self._restore_ready_fallback_index
        previous_fallback_scroll = self._restore_ready_fallback_scroll
        current = self._m2_current_identity()
        if current is not None and (
            self._m2_clear_cutoff is None or current > self._m2_clear_cutoff
        ):
            self._m2_clear_cutoff = current
        self._m2_history_epoch += 1
        self._pending_history_id = None
        self._invalidate_m2_restore(cancel=False)
        try:
            if delete_operation is None:
                delete_history(self._repository, expected)
            elif delete_operation() is False:
                raise HistoryError("历史删除操作未完成。")
        except HistoryError as exc:
            self._m2_clear_cutoff = previous_cutoff
            self._m2_history_epoch = previous_epoch
            if self._restore_generation == previous_restore_generation + 1:
                self._restore_generation = previous_restore_generation
                self._m2_restore_token = previous_restore_token
                self._restore_ready = previous_restore_ready
                self._restore_ready_revision = previous_restore_ready_revision
                self._restore_application = previous_restore_application
                self._restore_ready_fallback_index = previous_fallback_index
                self._restore_ready_fallback_scroll = previous_fallback_scroll
            if self._pending_history_id is None:
                self._pending_history_id = previous_pending_history_id
            self._clearing_history = False
            self._set_notice(f"清理历史失败：{exc}")
            return

        if on_cleared is not None:
            on_cleared()
        self._state_generation += 1
        self._scheduled_generation = self._state_generation
        self._save_timer.stop()
        self._m2_history_by_identity = {
            identity: link
            for identity, link in self._m2_history_by_identity.items()
            if not self._m2_identity_cleared(identity)
        }
        self._m2_pending_views = {
            identity: event
            for identity, event in self._m2_pending_views.items()
            if not self._m2_identity_cleared(identity)
        }
        self._m2_pending_results = {
            identity: event
            for identity, event in self._m2_pending_results.items()
            if not self._m2_identity_cleared(identity)
        }
        self._m2_rejected_history_identities = {
            identity
            for identity in self._m2_rejected_history_identities
            if not self._m2_identity_cleared(identity)
        }
        self._m2_committed_identities = {
            identity
            for identity in self._m2_committed_identities
            if not self._m2_identity_cleared(identity)
        }
        binding = self._m2_display_binding
        if (
            binding is None
            or self._m2_identity_cleared(binding.identity)
            or binding.identity not in self._m2_history_by_identity
        ):
            self._m2_display_binding = None
            self._active_history_id = None
            self._gallery_position = None
        else:
            self._active_history_id = binding.history_id
            if (
                self._gallery_position is not None
                and self._gallery_position.query_id != binding.history_id
            ):
                self._gallery_position = None

        retained_ids = {
            link.history_id for link in self._m2_history_by_identity.values()
        }
        model_revision = self._history_model.revision
        try:
            final_queries = self._repository.list_queries()
        except HistoryError as exc:
            final_queries = tuple(
                query
                for query in self._history_model.items
                if query.id in retained_ids
            )
            self._set_notice(f"查询历史读取失败：{exc}")
        if not self._closed and self._history_model.revision == model_revision:
            self._history_model.replace(final_queries)
        elif self._history_model.revision != model_revision:
            final_queries = self._history_model.items
        valid_history_ids = {query.id for query in final_queries}
        missing_identities = {
            identity
            for identity, link in self._m2_history_by_identity.items()
            if link.history_id not in valid_history_ids
        }
        for identity in missing_identities:
            self._m2_history_by_identity.pop(identity, None)
            self._m2_committed_identities.discard(identity)
            self._m2_rejected_history_identities.add(identity)
        binding = self._m2_display_binding
        if binding is not None and binding.history_id not in valid_history_ids:
            self._m2_display_binding = None
            self._active_history_id = None
            self._gallery_position = None
        if (
            self._m2_restore_token is not None
            and self._m2_restore_token.history_id not in valid_history_ids
        ):
            self._invalidate_m2_restore(cancel=False)
        if self._pending_history_id not in valid_history_ids:
            self._pending_history_id = None
        if self._pending_history_id is None:
            clear_exact = getattr(
                self._query_bridge,
                "clear_loaded_history_definition",
                None,
            )
            if clear_exact is not None:
                clear_exact()
        self._cancel_m2_restore_token(previous_restore_token)
        self.historyChanged.emit()
        self.selectionChanged.emit()
        self.activeGalleryChanged.emit()
        self._clearing_history = False
        if (
            not self._closed
            and (
                self._active_history_id is not None
                or self._gallery_position is not None
            )
        ):
            self._schedule_save()
        return True

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
