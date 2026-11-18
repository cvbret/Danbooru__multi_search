"""Qt confirmations and image/session adapters for cache cleanup."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from PySide6.QtCore import QObject, Property, Signal, Slot

from danbooru_multi_search.app.cache_cleanup import CacheCleanupService, CleanupSnapshot
from danbooru_multi_search.app.write_access import SharedWriteAccess
from danbooru_multi_search.danbooru.query_checkpoint import CheckpointError
from danbooru_multi_search.storage.history import HistoryError
from danbooru_multi_search.ui.image_cache import ImageCacheController
from danbooru_multi_search.ui.session_state import BrowserSessionController


@dataclass(frozen=True, slots=True)
class _Cleanup:
    token: str
    snapshot: CleanupSnapshot
    message: str


class CacheCleanupController(QObject):
    changed = Signal()

    def __init__(
        self,
        service: CacheCleanupService,
        session: BrowserSessionController,
        access: SharedWriteAccess,
        images: ImageCacheController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._service, self._session = service, session
        self._access, self._images = access, images
        self._pending: _Cleanup | None = None
        self._closed = False
        self._generation = 0
        self._notice = ""
        access.changed.connect(self._busy_changed)

    @Property(bool, notify=changed)
    def canWrite(self) -> bool:
        return not self._closed and not self._access.busy

    @Property(str, notify=changed)
    def noticeText(self) -> str:
        return self._notice

    @Property("QVariantMap", notify=changed)
    def confirmation(self) -> dict[str, object]:
        pending = self._pending
        if pending is None:
            return {"token": "", "kind": "", "count": 0, "message": ""}
        return {
            "token": pending.token, "kind": pending.snapshot.kind,
            "count": pending.snapshot.count, "message": pending.message,
        }

    @Slot(str, result=bool)
    def prepareCleanup(self, kind: str) -> bool:
        if self._closed:
            return False
        self._generation += 1
        generation = self._generation
        self._pending, self._notice = None, ""
        if not self.canWrite:
            return self._fail(ValueError("收藏正在变更，请稍后再清理。"))
        try:
            image_count = 0
            if kind == "images":
                cache = self._images.cache
                image_count = len(cache.entries()) if cache is not None and hasattr(cache, "entries") else 0
            snapshot = self._service.capture(kind, image_count=image_count)
            count = snapshot.count
            if kind == "history":
                message = f"清理 {count} 条查询历史、对应检查点及最近使用标签？将停止相关活动查询，保留当前画廊和草稿。"
            elif kind == "checkpoints":
                message = f"清理 {count} 条执行检查点及保存的扫描结果？将停止相关活动查询，保留历史条件、当前画廊和草稿。清理后这些历史只能从头执行。"
            else:
                message = f"当前有 {count} 张已缓存图片。确认后清理未收藏的图片缓存，并按确认时的最新收藏保留受保护图片。图片请求会停止，查询和下载任务继续运行。"
            if self._closed or self._generation != generation:
                return False
            pending = _Cleanup(uuid4().hex, snapshot, message)
            self._pending, self._notice = pending, ""
            self.changed.emit()
            return not self._closed and self._pending is pending
        except (CheckpointError, HistoryError, OSError, RuntimeError, ValueError) as exc:
            return self._fail(exc)

    @Slot()
    def cancelCleanup(self) -> None:
        if not self._closed:
            self._generation += 1
            self._pending = None
            self.changed.emit()

    def _clear_history(
        self, delete_operation: Callable[[], None], on_cleared: Callable[[], None],
    ) -> None:
        if not self._session.clearHistory(
            delete_operation=delete_operation, on_cleared=on_cleared,
        ):
            raise HistoryError(self._session.noticeText or "历史清理未完成。")

    def _clear_images(self) -> str:
        if not self._images.clearCache():
            raise ValueError(self._images.statusText or "图片缓存清理未完成。")
        return self._images.statusText or "图片缓存清理完成。"

    @Slot(str, result=bool)
    def confirmCleanup(self, token: str) -> bool:
        pending = self._pending
        if not self.canWrite or pending is None or not token or pending.token != token:
            return False

        def current() -> bool:
            return not self._closed and self._pending is pending

        def publish(notice: str) -> None:
            if current():
                self._pending, self._notice = None, notice
                self.changed.emit()

        def failed(error: Exception) -> None:
            if current():
                self._fail(error)

        return self._service.execute(
            pending.snapshot,
            guard=current,
            clear_history=self._clear_history,
            refresh_history=self._session._refresh_history,
            clear_images=self._clear_images,
            publish=publish,
            failed=failed,
        )

    def _fail(self, error: Exception) -> bool:
        if not self._closed:
            self._notice = str(error)
            self.changed.emit()
        return False

    @Slot()
    def _busy_changed(self) -> None:
        if not self._closed:
            self.changed.emit()

    def shutdown(self) -> None:
        self._closed = True
        self._generation += 1
        self._pending = None
