"""Bounded metadata preparation; all file work belongs to the existing manager."""

from pathlib import Path

from PySide6.QtCore import QObject, Signal

from danbooru_multi_search.core.downloads import DownloadConflictPolicy, DownloadError
from danbooru_multi_search.core.pool_downloads import (
    POOL_DOWNLOAD_GROUP_SIZE, PoolDownloadPageState, PoolDownloadPlan,
)
from danbooru_multi_search.core.pool_pages import PoolPageBatch
from danbooru_multi_search.core.pool_progress import normalize_api_source
from danbooru_multi_search.core.pools import DanbooruPool, PoolPageReference
from danbooru_multi_search.danbooru.download_manager import PersistentDownloadManager
from danbooru_multi_search.danbooru.pool_pages import PoolPageRequest, PoolPageRequestService
from danbooru_multi_search.storage.pool_downloads import PoolDownloadRepository
from danbooru_multi_search.tasks.qt_scheduling import QtScheduler
from danbooru_multi_search.tasks.scheduling import ScheduledCall, Scheduler


class PoolDownloadManager(QObject):
    """One in-flight <=40-position request; confirmed plans survive navigation.

    changed(plan_id) is a committed preparation fact, never a file completion.
    Signals permit synchronous stop, replacement and application shutdown.
    """

    changed = Signal(str)
    errorOccurred = Signal(str)

    def __init__(
        self, repository: PoolDownloadRepository, service: PoolPageRequestService,
        downloads: PersistentDownloadManager, source: str, parent: QObject | None = None,
        *, scheduler: Scheduler | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository, self._service, self._downloads = repository, service, downloads
        self.source = normalize_api_source(source)
        self._scheduler = scheduler or QtScheduler(self)
        self._call: ScheduledCall | None = None
        self._active: PoolDownloadPlan | None = None
        self._request: PoolPageRequest | None = None
        self._intent: tuple[PoolDownloadPlan, tuple[PoolPageReference, ...]] | None = None
        self._closing = self._closed = False
        self._final: PoolDownloadPlan | None = None
        self._adopt_done = True
        self._adopt_offset = 0
        self._adopt_keys: list[str] = []
        self.error = ""
        try:
            repository.recover_unfinished()
        except DownloadError as exc:
            self.error = str(exc)
        service.accepted.connect(self._accepted)
        service.completed.connect(self._completed)
        downloads.errorOccurred.connect(self._download_error)

    @property
    def active(self) -> PoolDownloadPlan | None:
        return self._active

    def _available(self) -> None:
        if self._closing or self._closed or self._downloads.halted or self._downloads.closed:
            raise DownloadError("manager_unavailable", self._downloads.error or "整池下载准备已经关闭。")
        if self._active is not None:
            raise DownloadError("preparation_busy", "另一个整池任务正在准备，请先停止准备。")

    def start(self, pool: DanbooruPool, directory: Path, policy: DownloadConflictPolicy) -> PoolDownloadPlan:
        self._available()
        plan = self.repository.create(pool, self.source, directory, policy)
        self._activate(plan)
        return plan

    def resume(self, key: str, generation: int, *, retry_missing: bool = False) -> bool:
        self._available()
        plan = self.repository.get(key)
        if plan.generation != generation:
            raise DownloadError("stale_download", "整池任务已经改变，请重新选择。")
        claimed = self.repository.resume(plan, self.source, retry_missing=retry_missing)
        self._activate(claimed, adopt=True)
        return True

    def _activate(self, plan: PoolDownloadPlan, *, adopt: bool = False) -> None:
        self._active = plan
        self._adopt_done, self._adopt_offset, self._adopt_keys = not adopt, 0, []
        self.error = ""
        self.changed.emit(plan.plan_id)
        if self._current(plan):
            self._schedule(plan)

    def _current(self, plan: PoolDownloadPlan) -> bool:
        return not self._closed and not self._closing and self._active is plan

    def _schedule(self, plan: PoolDownloadPlan) -> None:
        if self._current(plan) and self._call is None:
            self._call = self._scheduler.call_later(0, lambda: self._pump(plan))

    def _pump(self, plan: PoolDownloadPlan) -> None:
        self._call = None
        if not self._current(plan) or self._intent is not None:
            return
        try:
            if self._downloads.halted or self._downloads.closed:
                raise DownloadError("manager_unavailable", self._downloads.error or "文件下载管理器不可用。")
            if not self._adopt_done:
                if not self._adopt_keys:
                    keys = self.repository.batches(plan.plan_id, limit=20, offset=self._adopt_offset, expected=plan)
                    self._adopt_keys = list(keys)
                    self._adopt_offset += len(keys)
                    self._adopt_done = not keys
                if self._adopt_keys:
                    # A crash/stop after atomic insertion may precede queue registration.
                    # The existing manager deduplicates WAITING items; paused downloads
                    # remain available for explicit recovery in their ordinary batch.
                    self._downloads.schedule_persisted_batch(self._adopt_keys.pop(0))
                    if self._current(plan):
                        self._schedule(plan)
                    return
            work = self.repository.next_work(plan)
            if not self._current(plan):
                return
            if not work:
                self.repository.complete(plan)
                if self._current(plan):
                    self._active = None
                    self.changed.emit(plan.plan_id)
                return
            waiting = tuple(page.reference for page in work if page.state is PoolDownloadPageState.WAITING)[:40]
            if waiting:
                self._intent = (plan, waiting)
                self._service.fetch_pages(waiting)
                return
            first = (work[0].reference.page_number - 1) // POOL_DOWNLOAD_GROUP_SIZE * POOL_DOWNLOAD_GROUP_SIZE + 1
            batch = self.repository.commit_group(plan, first)
            if not self._current(plan):
                return
            if batch is not None:
                self._downloads.schedule_persisted_batch(batch.batch_id)
            if self._current(plan):
                self.changed.emit(plan.plan_id)
            if self._current(plan):
                self._schedule(plan)
        except DownloadError as exc:
            self._pause(plan, str(exc))

    def _accepted(self, request: PoolPageRequest) -> None:
        intent = self._intent
        if intent is None or not self._current(intent[0]) or request.references != intent[1]:
            self._service.cancel_current(request.request_id)
            return
        self._request = request

    def _completed(self, request: PoolPageRequest, batch: PoolPageBatch | None, error: str, code: str) -> None:
        intent = self._intent
        # Queue rejection can complete without an accepted notification.
        if (intent is None or not self._current(intent[0]) or request.references != intent[1]
                or self._request is not request and not (self._request is None and code == "queue_rejected")):
            return
        plan = intent[0]
        self._request = self._intent = None
        if code or batch is None:
            self._pause(plan, error or "页面元数据请求失败，已停止准备。")
            return
        try:
            if tuple(slot.reference for slot in batch.slots) != request.references:
                raise DownloadError("invalid_response", "整池页面响应与请求页位不一致。")
            self.repository.record_metadata(plan, batch)
            if self._current(plan):
                self.changed.emit(plan.plan_id)
            if self._current(plan):
                self._schedule(plan)
        except DownloadError as exc:
            self._pause(plan, str(exc))

    def _detach(self) -> None:
        self._active = None
        self._intent = self._request = None
        if self._call:
            self._call.cancel()
            self._call = None
        self._service.cancel_current()

    def stop(self, key: str, generation: int) -> bool:
        plan = self._active
        if plan is None or plan.plan_id != key or plan.generation != generation or not self._current(plan):
            return False
        self._pause(plan, "已停止准备；已加入的文件下载继续，可在关联批次取消或重试。")
        return True

    def _pause(self, plan: PoolDownloadPlan, message: str) -> None:
        if not self._current(plan):
            return
        self._detach()
        try:
            self.repository.stop(plan, message)
        except DownloadError as exc:
            message = f"{message}；暂停状态保存失败：{exc}"
        self.error = message
        if not self._closing and not self._closed:
            self.errorOccurred.emit(message)
            if not self._closing and not self._closed:
                self.changed.emit(plan.plan_id)

    def _download_error(self, message: str) -> None:
        if self._active and self._downloads.halted:
            self._pause(self._active, message)

    def begin_shutdown(self) -> None:
        """Stop all preparation before waiting for an existing database writer."""
        if self._closing or self._closed:
            return
        self._closing = True
        self._final = self._active
        self._detach()
        self._service.shutdown()

    def finish_shutdown(self) -> None:
        """Settle the captured plan after the existing import worker has finished."""
        if self._closed:
            return
        if not self._closing:
            self.begin_shutdown()
        try:
            if self._final is not None:
                self.repository.stop(self._final, "应用已关闭，等待手动继续准备；已加入的下载可在原批次恢复。")
        finally:
            self._final = None
            self._closed = True
