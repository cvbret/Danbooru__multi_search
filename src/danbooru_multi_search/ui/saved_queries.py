"""Coordinate complete named query drafts without running a query."""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from PySide6.QtCore import QObject, Property, Signal, Slot

from danbooru_multi_search.app.write_access import SharedWriteAccess
from danbooru_multi_search.core.saved_queries import SavedQuery, SavedQueryDraft, SavedQueryError
from danbooru_multi_search.danbooru.query_definition import GalleryQueryDefinition
from danbooru_multi_search.storage.saved_queries import SavedQueryRepository
from danbooru_multi_search.ui.query_state import M2QueryBridgeController

_MODE_TEXT = {"standard": "普通查询", "rank": "近期热门", "latest": "最新作品"}


class SavedQueryController(QObject):
    changed = Signal()
    draftLoaded = Signal()
    PAGE_SIZE = 50

    def __init__(self, repository: SavedQueryRepository, query: M2QueryBridgeController,
                 access: SharedWriteAccess, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._repository, self._query, self._access = repository, query, access
        self._queries: tuple[SavedQuery, ...] = ()
        self._generation = 0
        self._draft_generation = 0
        self._active = self._closed = self._more = False
        self._error = ""
        self._capture: tuple[str, GalleryQueryDefinition] | None = None
        self._pending: tuple[str, SavedQuery, int] | None = None
        query.draftChanged.connect(self._draft_changed)
        access.changed.connect(self._busy_changed)

    @Property(int, notify=changed)
    def generation(self) -> int:
        return self._generation

    @Property(bool, notify=changed)
    def canWrite(self) -> bool:
        return self._active and not self._closed and not self._access.busy

    @Property(bool, notify=changed)
    def hasMore(self) -> bool:
        return self._more

    @Property(str, notify=changed)
    def errorText(self) -> str:
        return self._error

    @Property("QVariantList", notify=changed)
    def queries(self) -> list[dict[str, str]]:
        return [{"key": value.query_id, "revision": str(value.revision), "name": value.name,
                 "expression": value.definition.normalized_expression or "（无标签条件）",
                 "mode": _MODE_TEXT[value.definition.mode.value]} for value in self._queries]

    @Property("QVariantMap", notify=changed)
    def capturedDraft(self) -> dict[str, str]:
        if self._capture is None:
            return {"token": "", "expression": "", "mode": ""}
        return {"token": self._capture[0], "expression": self._capture[1].normalized_expression or "（无标签条件）",
                "mode": _MODE_TEXT[self._capture[1].mode.value]}

    @Property("QVariantMap", notify=changed)
    def deleteConfirmation(self) -> dict[str, str]:
        return {"token": self._pending[0], "name": self._pending[1].name} if self._pending else {"token": "", "name": ""}

    @Slot(bool)
    def setActive(self, active: bool) -> None:
        if self._closed or active == self._active:
            return
        self._active = active
        if active:
            self._reload(preserve_error=True)
        else:
            self._generation += 1
            self._capture = self._pending = None
            self.changed.emit()

    @Slot(result=bool)
    def reload(self) -> bool:
        return self._reload()

    def _reload(self, *, preserve_error: bool = False) -> bool:
        if self._closed or not self._active:
            return False
        self._generation += 1
        self._pending = None
        generation = self._generation
        try:
            page = self._repository.list_queries(limit=self.PAGE_SIZE)
        except SavedQueryError as exc:
            return self._fail(exc)
        if not self._current(generation):
            return False
        self._queries, self._more = page, len(page) == self.PAGE_SIZE
        if not preserve_error:
            self._error = ""
        self.changed.emit()
        return self._current(generation)

    @Slot(result=bool)
    def loadMore(self) -> bool:
        if self._closed or not self._active or not self._more:
            return False
        generation, previous = self._generation, self._queries
        try:
            page = self._repository.list_queries(limit=self.PAGE_SIZE, offset=len(previous))
        except SavedQueryError as exc:
            return self._fail(exc)
        if not self._current(generation) or self._queries != previous:
            return False
        self._queries += page
        self._more = len(page) == self.PAGE_SIZE
        self.changed.emit()
        return self._current(generation)

    @Slot(result=str)
    def captureDraft(self) -> str:
        if self._closed:
            return ""
        definition = self._query.draft_definition
        if definition is None:
            self._fail(SavedQueryError("invalid_definition", self._query.errorText or "请先填写有效的查询条件。"))
            return ""
        captured = (uuid4().hex, definition)
        self._capture, self._error = captured, ""
        self.changed.emit()
        return captured[0] if not self._closed and self._capture == captured else ""

    @Slot(str, str, result=bool)
    def saveCaptured(self, token: str, name: str) -> bool:
        captured = self._capture
        if captured is None or captured[0] != token:
            return False
        return self._write(self._generation, lambda: self._repository.create(SavedQueryDraft(name, captured[1])),
                           guard=lambda: self._capture == captured and self._query.draft_definition == captured[1])

    @Slot(str, str, int, result=bool)
    def loadDraft(self, key: str, revision: str, generation: int) -> bool:
        try:
            expected = self._record(key, revision, generation)
            draft_generation = self._draft_generation
            offset = 0
            while True:
                page = self._repository.list_queries(limit=1000, offset=offset)
                found = next((value for value in page if value.query_id == key), None)
                if found is not None or len(page) < 1000:
                    break
                offset += len(page)
            if found != expected:
                raise SavedQueryError("stale_query", "常用查询已改变，请刷新后重新选择。")
            if not self._current(generation) or self._draft_generation != draft_generation:
                return False
            if not self._query.loadDraftDefinition(expected.definition):
                return self._fail(SavedQueryError("draft_load_failed", self._query.errorText or "无法载入常用查询，请检查查询条件。"))
            if not self._current(generation) or self._query.draft_definition != expected.definition:
                return False
            self._error = ""
            self.changed.emit()
            if not self._current(generation) or self._query.draft_definition != expected.definition:
                return False
            self.draftLoaded.emit()
            return True
        except SavedQueryError as exc:
            return self._fail(exc)

    @Slot(str, str, int, str, result=bool)
    def rename(self, key: str, revision: str, generation: int, name: str) -> bool:
        try:
            expected = self._record(key, revision, generation)
        except SavedQueryError as exc:
            return self._fail(exc)
        return self._write(generation, lambda: self._repository.rename(expected, name))

    @Slot(str, str, int, result=bool)
    def prepareDelete(self, key: str, revision: str, generation: int) -> bool:
        try:
            expected = self._record(key, revision, generation)
        except SavedQueryError as exc:
            return self._fail(exc)
        self._pending = (uuid4().hex, expected, generation)
        self.changed.emit()
        return self._current(generation) and self._pending is not None

    @Slot(str, result=bool)
    def confirmDelete(self, token: str) -> bool:
        pending = self._pending
        if pending is None or pending[0] != token:
            return False
        return self._write(pending[2], lambda: self._repository.delete(pending[1]), guard=lambda: self._pending == pending)

    @Slot()
    def cancelDelete(self) -> None:
        if not self._closed:
            self._pending = None
            self.changed.emit()

    def _record(self, key: str, revision: str, generation: int) -> SavedQuery:
        if self._current(generation):
            for value in self._queries:
                if value.query_id == key and str(value.revision) == revision:
                    return value
        raise SavedQueryError("stale_query", "常用查询已改变，请刷新后重新选择。")

    def _write(self, generation: int, operation: Callable[[], object], *, guard: Callable[[], bool] = lambda: True) -> bool:
        if not self._current(generation) or not guard() or not self.canWrite:
            return False
        owner = uuid4().hex
        if not self._access.begin_external_write(owner):
            return False
        try:
            if not self._current(generation) or not guard():
                return False
            operation()
            if not self._current(generation):
                return False
            self._capture = None
            return self.reload()
        except SavedQueryError as exc:
            return self._fail(exc)
        finally:
            self._access.finish_external_write(owner)

    def _current(self, generation: int) -> bool:
        return not self._closed and self._active and self._generation == generation

    def _fail(self, error: SavedQueryError) -> bool:
        if not self._closed:
            self._error = str(error)
            self.changed.emit()
        return False

    @Slot()
    def _draft_changed(self) -> None:
        self._draft_generation += 1
        if not self._closed and self._capture and self._query.draft_definition != self._capture[1]:
            self._capture = None
            self.changed.emit()

    @Slot()
    def _busy_changed(self) -> None:
        if not self._closed:
            self.changed.emit()

    def shutdown(self) -> None:
        self._closed, self._active = True, False
        self._generation += 1
        self._capture = self._pending = None
