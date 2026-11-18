"""Confirmation identities and paginated views of persistent pool preparation."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, Property, QUrl, Signal, Slot

from danbooru_multi_search.core.downloads import DownloadError
from danbooru_multi_search.core.pool_downloads import PoolDownloadPlan
from danbooru_multi_search.core.pools import DanbooruPool, PoolCategory
from danbooru_multi_search.app.settings import SettingsController
from danbooru_multi_search.core.settings import SettingsError
from danbooru_multi_search.app.pool_downloads import PoolDownloadManager
from danbooru_multi_search.app.download_submission import DownloadSubmissionService
from danbooru_multi_search.ui.download_models import PoolDownloadListModel, PoolDownloadPageListModel
from danbooru_multi_search.ui.downloads import DownloadUiController
from danbooru_multi_search.ui.pool_reader import PoolReaderController
from danbooru_multi_search.ui.pools import PoolBrowserController


@dataclass(frozen=True, slots=True)
class _Intent:
    token: str
    pool: DanbooruPool
    directory: Path
    guard: Callable[[], bool]


class PoolDownloadController(QObject):
    changed = Signal()
    queued = Signal(str)

    def __init__(
        self, manager: PoolDownloadManager, settings: SettingsController, downloads: DownloadUiController,
        browser: PoolBrowserController, reader: PoolReaderController, parent: QObject | None = None,
        *, submission: DownloadSubmissionService,
    ) -> None:
        super().__init__(parent)
        self._manager, self._settings, self._downloads = manager, settings, downloads
        self._browser, self._reader = browser, reader
        self._submission = submission
        self._plans = PoolDownloadListModel(self)
        self._pages = PoolDownloadPageListModel(self)
        self._intent: _Intent | None = None
        self._choice: tuple[str, str] | None = None
        self._action: tuple[str, PoolDownloadPlan, str] | None = None
        self._selected = ""
        self._selected_plan: PoolDownloadPlan | None = None
        self._page_offset = 0
        self._batch_offset = 0
        self._batches: list[str] = []
        self._more = False
        self._active = self._busy = self._closed = False
        self._generation = 0
        self._visible_sources: set[str] = set()
        self._error = manager.error
        manager.changed.connect(self._committed)
        manager.errorOccurred.connect(self._failed)
        browser.changed.connect(self._source_changed)
        reader.changed.connect(self._source_changed)

    @Property(QObject, constant=True)
    def plansModel(self) -> PoolDownloadListModel:
        return self._plans

    @Property(QObject, constant=True)
    def pagesModel(self) -> PoolDownloadPageListModel:
        return self._pages

    @Property(bool, notify=changed)
    def busy(self) -> bool:
        return self._busy

    @Property(str, notify=changed)
    def errorText(self) -> str:
        return self._error

    @Property(bool, notify=changed)
    def canLoadMore(self) -> bool:
        return self._more

    @Property(dict, notify=changed)
    def pending(self) -> dict:
        value = self._intent
        return {"valid": bool(value and not self._closed and value.guard()),
                "count": value.pool.post_count if value else 0,
                "directory": str(value.directory) if value else "",
                "directoryUrl": QUrl.fromLocalFile(str(value.directory)) if value else QUrl()}

    @Property(dict, notify=changed)
    def selected(self) -> dict:
        plan = self._selected_plan
        return {"key": plan.plan_id if plan else "", "generation": str(plan.generation) if plan else "",
                "name": plan.name if plan else "", "total": plan.total if plan else 0,
                "message": plan.message if plan else "", "directory": str(plan.directory) if plan else "",
                "preparing": bool(plan and self._manager.active is not None
                                  and self._manager.active.plan_id == plan.plan_id),
                "firstPage": self._page_offset + 1,
                "hasPrevious": self._page_offset > 0,
                "hasNext": bool(plan and self._page_offset + 40 < plan.total)}

    @Property(list, notify=changed)
    def batchKeys(self) -> list[str]:
        return self._batches

    @Property(int, notify=changed)
    def batchOffset(self) -> int:
        return self._batch_offset

    @Slot(str, str, result=str)
    def capturePool(self, source: str, generation: str) -> str:
        if self._closed or self._busy:
            return ""
        self._intent = self._choice = None
        if source == "reader":
            pool = self._reader.current_pool
            session = self._reader.readingSession
            guard = lambda: ("reader" in self._visible_sources and self._reader.active
                             and self._reader.current_pool is pool and self._reader.readingSession == session)
            current = str(session)
        elif source == "browser":
            pool = self._browser.current_pool
            session = self._browser.generation
            guard = lambda: ("browser" in self._visible_sources and self._browser.current_pool is pool
                             and self._browser.generation == session)
            current = str(session)
        else:
            return ""
        if generation != current or pool is None or not guard():
            return ""
        if pool.category is not PoolCategory.SERIES or pool.is_deleted or not pool.post_count:
            self._failed("请选择包含页面的有效系列作品池。")
            return ""
        value = _Intent(uuid4().hex, pool, Path(self._downloads.defaultDirectory), guard)
        self._intent = value
        self._error = ""
        self.changed.emit()
        return value.token if self._intent is value and not self._closed and guard() else ""

    @Slot(str, bool)
    def setSourceVisible(self, source: str, visible: bool) -> None:
        """Track the actual page, independent of legacy browser private fields."""
        if self._closed or source not in {"browser", "reader"}:
            return
        if visible:
            self._visible_sources.add(source)
        else:
            self._visible_sources.discard(source)
        self._source_changed()

    @Slot(str, result=bool)
    def openRequest(self, token: str) -> bool:
        return bool(not self._closed and self._intent and self._intent.token == token and self._intent.guard())

    @Slot(str)
    def cancelRequest(self, token: str) -> None:
        if self._intent and self._intent.token == token:
            self._intent = self._choice = None
            if not self._closed:
                self.changed.emit()

    @Slot(str, result=str)
    def beginDirectorySelection(self, purpose: str) -> str:
        if purpose != "request" or self._closed or self._busy or not self._intent or not self.openRequest(self._intent.token):
            return ""
        token = uuid4().hex
        self._choice = (token, self._intent.token)
        return token

    @Slot(str)
    def cancelDirectorySelection(self, token: str) -> None:
        if self._choice and self._choice[0] == token:
            self._choice = None

    @Slot(QUrl, str, result=bool)
    def acceptDirectory(self, url: QUrl, token: str) -> bool:
        if self._closed or self._busy or not self._choice or self._choice[0] != token:
            return False
        choice, self._choice = self._choice, None
        path = Path(url.toLocalFile()) if url.isLocalFile() else Path()
        if not self.openRequest(choice[1]):
            return self._failed("作品池来源已改变，请重新选择。")
        if not path.is_absolute() or not path.is_dir():
            return self._failed("请选择有效的本地文件夹。")
        self._intent = replace(self._intent, directory=path)
        self.changed.emit()
        return True

    @Slot(str, str, bool, result=bool)
    def confirmRequest(self, token: str, policy: str, save_default: bool) -> bool:
        value = self._intent
        if self._closed or self._busy or not self.openRequest(token):
            return False
        self._busy = True
        self.changed.emit()
        try:
            plan = self._submission.submit_pool(
                self._manager,
                value.pool,
                value.directory,
                policy,
                save_default,
                source_current=lambda: (
                    not self._closed
                    and self._intent is value
                    and value.guard()
                ),
            )
            if self._intent is value:
                self._intent = self._choice = None
            if not self._closed:
                self._error = ""
                self.queued.emit(plan.plan_id)
            return True
        except (DownloadError, SettingsError, ValueError, OSError) as exc:
            return self._failed(str(exc))
        finally:
            self._busy = False
            if not self._closed:
                self.changed.emit()

    @Slot(bool)
    def setActive(self, active: bool) -> None:
        if self._closed:
            return
        self._active = active
        self._generation += 1
        if not active:
            self._action = None
        if active:
            self.reload()

    def _current(self, generation: int) -> bool:
        return not self._closed and self._active and generation == self._generation

    @Slot(result=bool)
    def reload(self) -> bool:
        return self._load_plans(20)

    @Slot(result=bool)
    def loadMore(self) -> bool:
        return self._load_plans(len(self._plans.items) + 20)

    def _load_plans(self, count: int) -> bool:
        if not self._active or self._closed:
            return False
        self._generation += 1
        generation = self._generation
        try:
            # SQL pagination stays bounded even when the visible list grows.
            values = []
            for offset in range(0, count, 20):
                values.extend(self._manager.repository.summaries(limit=20, offset=offset))
            if not self._current(generation):
                return False
            self._plans.replace(values)
            if not self._current(generation):
                return False
            self._more = len(values) == count
            if self._selected:
                return self.selectPlan(self._selected, preserve=True)
            self.changed.emit()
            return self._current(generation)
        except DownloadError as exc:
            return self._failed(str(exc))

    @Slot(str, result=bool)
    def selectPlan(self, key: str, *, preserve: bool = False,
                   page_offset: int | None = None, batch_offset: int | None = None) -> bool:
        if self._closed or not self._active:
            return False
        self._generation += 1
        generation = self._generation
        try:
            plan = self._manager.repository.get(key)
            offset = self._page_offset if preserve and key == self._selected else 0
            target_batch = self._batch_offset if preserve and key == self._selected else 0
            if page_offset is not None:
                offset = page_offset
            if batch_offset is not None:
                target_batch = batch_offset
            pages = self._manager.repository.pages(key, offset=offset)
            batches = self._manager.repository.batches(key, offset=target_batch, limit=10)
            if not self._current(generation):
                return False
            if self._selected != key:
                self._action = None
            self._selected, self._selected_plan = key, plan
            self._page_offset, self._batch_offset = offset, target_batch
            self._pages.replace(pages)
            if not self._current(generation):
                return False
            self._batches = list(batches)
            self.changed.emit()
            return self._current(generation)
        except DownloadError as exc:
            return self._failed(str(exc))

    @Slot(int)
    def turnPages(self, direction: int) -> None:
        if self._selected_plan and direction in {-1, 1}:
            offset = min(max(0, self._page_offset + direction * 40),
                         (self._selected_plan.total - 1) // 40 * 40)
            self.selectPlan(self._selected, preserve=True, page_offset=offset)

    @Slot(int)
    def turnBatches(self, direction: int) -> None:
        if self._selected_plan and direction in {-1, 1}:
            offset = max(0, self._batch_offset + direction * 10)
            self.selectPlan(self._selected, preserve=True, batch_offset=offset)

    @Slot(str, str, str, result=str)
    def captureAction(self, key: str, generation: str, action: str) -> str:
        if self._closed or not self._active or key != self._selected or action not in {"stop", "resume", "retry"}:
            return ""
        try:
            plan = self._manager.repository.get(key)
        except DownloadError as exc:
            self._failed(str(exc))
            return ""
        if generation != str(plan.generation):
            return ""
        token = uuid4().hex
        self._action = (token, plan, action)
        return token

    @Slot(str, result=bool)
    def runAction(self, token: str) -> bool:
        value, self._action = self._action, None
        if (self._closed or not self._active or not value or value[0] != token
                or value[1].plan_id != self._selected):
            return False
        try:
            plan, action = value[1:]
            if action == "stop":
                success = self._manager.stop(plan.plan_id, plan.generation)
            else:
                success = self._manager.resume(plan.plan_id, plan.generation, retry_missing=action == "retry")
            if success and not self._closed:
                self._error = ""
                self.changed.emit()
            return success
        except DownloadError as exc:
            return self._failed(str(exc))

    def _committed(self, key: str) -> None:
        if self._active and not self._closed:
            self._load_plans(max(20, len(self._plans.items)))

    def _failed(self, message: str) -> bool:
        self._error = message
        if not self._closed:
            self.changed.emit()
        return False

    def _source_changed(self) -> None:
        if self._intent and not self._closed:
            self.changed.emit()

    def shutdown(self) -> None:
        self._closed = True
        self._generation += 1
        self._intent = self._choice = self._action = None
