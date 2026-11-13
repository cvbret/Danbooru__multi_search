"""Stable user confirmations for history, checkpoints and ordinary images."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from PySide6.QtCore import QObject, Property, Signal, Slot

from danbooru_multi_search.danbooru.query_checkpoint import CheckpointError
from danbooru_multi_search.storage.history import HistoryError, HistoryRepository, HistoryCleanupIdentity
from danbooru_multi_search.storage.query_checkpoints import QueryCheckpointRepository, CheckpointCleanupIdentity
from danbooru_multi_search.ui.cache_protection import CacheProtectionController
from danbooru_multi_search.ui.favorites import FavoriteController
from danbooru_multi_search.ui.image_cache import ImageCacheController
from danbooru_multi_search.ui.query_checkpoints import QueryCheckpointController
from danbooru_multi_search.ui.session_state import BrowserSessionController


@dataclass(frozen=True, slots=True)
class _Cleanup:
    token: str
    kind: str
    identities: tuple[HistoryCleanupIdentity | CheckpointCleanupIdentity, ...]
    count: int
    message: str


class CacheCleanupController(QObject):
    changed = Signal()

    def __init__(self, history: HistoryRepository, checkpoints: QueryCheckpointRepository,
                 query_checkpoints: QueryCheckpointController, session: BrowserSessionController,
                 favorites: FavoriteController, images: ImageCacheController,
                 protection: CacheProtectionController, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._history, self._checkpoints = history, checkpoints
        self._query_checkpoints, self._session = query_checkpoints, session
        self._favorites, self._images, self._protection = favorites, images, protection
        self._pending: _Cleanup | None = None
        self._closed = False
        self._generation = 0
        self._notice = ""
        favorites.busyChanged.connect(self._busy_changed)

    @Property(bool, notify=changed)
    def canWrite(self) -> bool:
        return not self._closed and not self._favorites.busy

    @Property(str, notify=changed)
    def noticeText(self) -> str:
        return self._notice

    @Property("QVariantMap", notify=changed)
    def confirmation(self) -> dict[str, object]:
        if self._pending is None:
            return {"token": "", "kind": "", "count": 0, "message": ""}
        return {"token": self._pending.token, "kind": self._pending.kind, "count": self._pending.count, "message": self._pending.message}

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
            if kind == "history":
                identities = self._history.capture_cleanup()
                count = len(identities)
                message = f"清理 {count} 条查询历史、对应检查点及最近使用标签？将停止相关活动查询，保留当前画廊和草稿。"
            elif kind == "checkpoints":
                identities = self._checkpoints.capture_cleanup()
                count = len(identities)
                message = f"清理 {count} 条执行检查点及保存的扫描结果？将停止相关活动查询，保留历史条件、当前画廊和草稿。清理后这些历史只能从头执行。"
            elif kind == "images":
                identities = ()
                cache = self._images.cache
                count = len(cache.entries()) if cache is not None and hasattr(cache, "entries") else 0
                message = f"当前有 {count} 张已缓存图片。确认后清理未收藏的图片缓存，并按确认时的最新收藏保留受保护图片。图片请求会停止，查询和下载任务继续运行。"
            else:
                raise ValueError("请选择有效的清理范围。")
            if self._closed or self._generation != generation:
                return False
            pending = _Cleanup(uuid4().hex, kind, identities, count, message)
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

    @staticmethod
    def _owners(pending: _Cleanup) -> frozenset[tuple[int, str]]:
        if pending.kind == "history":
            return frozenset((item.history_id, item.checkpoint_owner) for item in pending.identities if item.checkpoint_owner is not None)
        return frozenset((item.history_id, item.owner) for item in pending.identities)

    @Slot(str, result=bool)
    def confirmCleanup(self, token: str) -> bool:
        pending = self._pending
        if not self.canWrite or pending is None or not token or pending.token != token:
            return False
        owner = uuid4().hex
        self._protection.set_cleanup_owner(owner)
        self._query_checkpoints.set_maintenance_owner(owner)
        if not self._favorites.begin_external_write(owner):
            self._query_checkpoints.set_maintenance_owner(None)
            self._protection.set_cleanup_owner(None)
            return False
        try:
            if self._closed or self._pending is not pending:
                return False
            if pending.kind in {"history", "checkpoints"}:
                current = self._history.capture_cleanup() if pending.kind == "history" else self._checkpoints.capture_cleanup()
                if self._closed or self._pending is not pending:
                    return False
                if current != pending.identities:
                    self._query_checkpoints.reconcile_ownership()
                    raise ValueError("记录归属已改变，请重新打开清理确认。")
                owners = self._owners(pending)
                active = self._query_checkpoints.current_ownership
                if active is not None and (active.history_id, active.owner) in owners:
                    self._query_checkpoints._controller.cancel()
                if self._closed or self._pending is not pending:
                    return False
                if pending.kind == "history":
                    if not self._session.clearHistory(expected=pending.identities,
                        on_cleared=lambda: self._query_checkpoints.retire_checkpoints(owners)):
                        raise HistoryError(self._session.noticeText or "历史清理未完成。")
                else:
                    self._checkpoints.clear_checkpoints(expected=pending.identities)
                    self._query_checkpoints.retire_checkpoints(owners)
                    self._session._refresh_history()
                notice = f"已清理 {pending.count} 条" + ("查询历史及对应检查点。" if pending.kind == "history" else "执行检查点；历史条件和当前画廊已保留。")
            else:
                if not self._images.clearCache():
                    raise ValueError(self._images.statusText or "图片缓存清理未完成。")
                notice = self._images.statusText or "图片缓存清理完成。"
            if not self._closed and self._pending is pending:
                self._pending, self._notice = None, notice
                self.changed.emit()
            return True
        except (CheckpointError, HistoryError, OSError, RuntimeError, ValueError) as exc:
            return self._fail(exc)
        finally:
            self._query_checkpoints.set_maintenance_owner(None)
            self._protection.set_cleanup_owner(None)
            self._favorites.finish_external_write(owner)

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
