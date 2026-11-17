"""GUI transfer state and one dedicated worker thread for local JSON files."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from uuid import uuid4

from PySide6.QtCore import QObject, Property, QThread, QUrl, Qt, Signal, Slot

from danbooru_multi_search.app.write_access import SharedWriteAccess
from danbooru_multi_search.storage.favorite_transfer import FavoriteImportMode
from danbooru_multi_search.app.favorite_transfer import (
    TransferOperation, TransferRequest, TransferResult, execute_transfer,
)
from danbooru_multi_search.ui.favorites import FavoriteController


class _TransferWorker(QObject):
    completed = Signal(object)

    def __init__(self, closing: Event):
        super().__init__()
        self._closing = closing

    @Slot(object, object)
    def execute(self, request: TransferRequest, cancelled: Event):
        result = execute_transfer(request, cancelled.is_set)
        self.completed.emit(result)
        if self._closing.is_set():
            QThread.currentThread().quit()


class FavoriteTransferController(QObject):
    """Keep immutable previews on the GUI thread and all file work off it."""

    changed = Signal()
    _dispatch = Signal(object, object)

    def __init__(self, database_path: Path, favorites: FavoriteController,
                 access: SharedWriteAccess, parent=None):
        super().__init__(parent)
        self._database_path = Path(database_path)
        self._favorites = favorites
        self._access = access
        self._thread: QThread | None = None
        self._worker: _TransferWorker | None = None
        self._closing_event = Event()
        self._closing = False
        self._closed = False
        self._active: TransferRequest | None = None
        self._cancel_event: Event | None = None
        self._collection_epoch = 0
        self._request_epoch = 0
        self._choice: tuple[str, str] | None = None
        self._mode = FavoriteImportMode.MERGE
        self._source = ""
        self._preview = None
        self._preview_token = ""
        self._message = "JSON 版本 1 只包含本地收藏与文件夹。"
        self._error_code = ""
        self._residual = ""
        favorites.collectionChanged.connect(self._collection_changed)
        access.changed.connect(self._shared_changed)

    @Property(bool, notify=changed)
    def busy(self):
        return self._active is not None

    @Property(bool, notify=changed)
    def canConfirm(self):
        return not self._closing and not self.busy and not self._access.busy and self._preview is not None

    @Property(bool, notify=changed)
    def canCancel(self):
        return not self._closing and (
            self._preview is not None or (
                self._active is not None and self._active.operation is not TransferOperation.APPLY
                and not self._cancel_event.is_set()
            )
        )

    @Property(str, notify=changed)
    def importMode(self):
        return self._mode.value

    @Property(str, notify=changed)
    def sourcePath(self):
        return self._source

    @Property(str, notify=changed)
    def message(self):
        return self._message

    @Property(str, notify=changed)
    def errorCode(self):
        return self._error_code

    @Property(str, notify=changed)
    def residualPath(self):
        return self._residual

    @Property("QVariantMap", notify=changed)
    def preview(self):
        value = self._preview
        if value is None:
            return {"token": ""}
        return {
            "token": self._preview_token, "mode": value.mode.value,
            "addedFolders": value.added_folders, "reusedFolders": value.reused_folders,
            "addedFavorites": value.added_favorites, "preservedFavorites": value.preserved_favorites,
            "deletedFolders": value.deleted_folders, "deletedFavorites": value.deleted_favorites,
        }

    def _begin_choice(self, kind: str) -> str:
        if self._closing or self.busy:
            return ""
        if kind == "import":
            self._clear_preview()
        token = uuid4().hex
        self._choice = (token, kind)
        self.changed.emit()
        return token

    @Slot(result=str)
    def beginImportSelection(self):
        return self._begin_choice("import")

    @Slot(result=str)
    def beginExportSelection(self):
        return self._begin_choice("export")

    @Slot(str)
    def cancelSelection(self, token: str):
        if not self._closing and self._choice and self._choice[0] == token:
            self._choice = None

    @Slot(QUrl, str, result=bool)
    def acceptImportFile(self, url: QUrl, token: str):
        return self._accept_file(url, token, "import")

    @Slot(QUrl, str, result=bool)
    def acceptExportFile(self, url: QUrl, token: str):
        return self._accept_file(url, token, "export")

    def _accept_file(self, url, token, kind):
        if self._closing or self.busy or self._choice != (token, kind):
            return False
        self._choice = None
        if not isinstance(url, QUrl) or not url.isLocalFile() or not Path(url.toLocalFile()).is_absolute():
            self._error_code = "invalid_path"
            self._message = "请选择本地 JSON 文件。"
            self.changed.emit()
            return False
        path = Path(url.toLocalFile())
        if kind == "import":
            self._source = str(path)
            return self._start(TransferOperation.PREVIEW, path=path)
        return self._start(TransferOperation.EXPORT, path=path)

    @Slot(str, result=bool)
    def setImportMode(self, value: str):
        if self._closing or self.busy:
            return False
        try:
            mode = FavoriteImportMode(value)
        except ValueError:
            return False
        if mode != self._mode:
            self._mode = mode
            self._choice = None
            self._clear_preview()
            self._message = "导入模式已改变，请重新预览。"
            self.changed.emit()
        return True

    @Slot(result=bool)
    def previewAgain(self):
        return bool(self._source) and self._start(TransferOperation.PREVIEW, path=Path(self._source))

    @Slot(str, result=bool)
    def confirmImport(self, token: str):
        if not self.canConfirm or token != self._preview_token:
            return False
        value = self._preview
        self._clear_preview()
        return self._start(TransferOperation.APPLY, preview=value)

    def _ensure_thread(self):
        if self._thread is not None:
            return
        self._thread = QThread(self)
        self._worker = _TransferWorker(self._closing_event)
        self._worker.moveToThread(self._thread)
        self._dispatch.connect(self._worker.execute, Qt.ConnectionType.QueuedConnection)
        self._worker.completed.connect(self._completed, Qt.ConnectionType.QueuedConnection)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    def _start(self, operation: TransferOperation, *, path=None, preview=None):
        if self._closing or self.busy:
            return False
        request = TransferRequest(uuid4().hex, operation, self._database_path, path, self._mode, preview)
        self._active = request
        self._cancel_event = Event()
        self._request_epoch = self._collection_epoch
        self._error_code = ""
        self._residual = ""
        if operation is TransferOperation.PREVIEW:
            self._clear_preview()
        if operation is TransferOperation.APPLY and not self._access.begin_external_write(request.job_id):
            self._active = None
            self._message = "收藏写入正在进行，请稍后重新预览。"
            if not self._closing:
                self.changed.emit()
            return False
        self._message = {
            TransferOperation.PREVIEW: "正在后台读取、解析并生成导入预览…",
            TransferOperation.EXPORT: "正在后台生成并写入收藏 JSON…",
            TransferOperation.APPLY: "正在应用导入，等待事务完成…",
        }[operation]
        if not self._closing:
            self.changed.emit()
        if self._closing:
            self._access.finish_external_write(request.job_id)
            self._active = None
            return False
        if self._cancel_event.is_set():
            self._completed(TransferResult(request.job_id, operation, "cancelled"))
            return False
        self._ensure_thread()
        self._dispatch.emit(request, self._cancel_event)
        return True

    @Slot(object)
    def _completed(self, result: TransferResult):
        request = self._active
        if request is None or request.job_id != result.job_id:
            return
        try:
            if self._closing:
                return
            if result.operation is TransferOperation.PREVIEW and self._cancel_event.is_set():
                self._clear_preview()
                self._message = "导入预览已取消。"
            elif result.status == "success" and result.operation is TransferOperation.APPLY:
                self._favorites.publish_external_snapshot(request.job_id, result.snapshot)
                if self._closing:
                    return
                self._message = "收藏导入完成。"
            elif result.status == "success" and result.operation is TransferOperation.PREVIEW:
                if self._request_epoch != self._collection_epoch:
                    self._error_code = "stale_preview"
                    self._message = "收藏数据已改变，请重新预览。"
                else:
                    self._preview = result.preview
                    self._preview_token = uuid4().hex
                    self._message = "请核对下面的影响数量，再确认导入。"
            elif result.status == "success":
                self._message = f"导出完成，共 {result.exported_count} 项收藏：{request.path}"
            elif result.status == "cancelled":
                self._message = "传输已取消，未开始后续步骤。"
            else:
                self._clear_preview()
                self._error_code = result.error_code
                self._message = result.message
                self._residual = str(result.residual_path) if result.residual_path else ""
        finally:
            self._active = None
            self._cancel_event = None
            # Successful imports publish facts and invalidate page confirmations
            # before this signal can re-enable any GUI favorite write entry.
            self._access.finish_external_write(request.job_id)
            if not self._closing:
                self.changed.emit()

    @Slot(result=bool)
    def cancel(self):
        if self._closing:
            return False
        if self._active is not None:
            if self._active.operation is TransferOperation.APPLY or self._cancel_event.is_set():
                return False
            self._cancel_event.set()
            self._clear_preview()
            self._message = "正在取消，等待当前步骤结束…"
        else:
            if self._preview is None and self._choice is None:
                return False
            self._clear_preview()
            self._choice = None
            self._message = "已取消导入预览。"
        self.changed.emit()
        return True

    def _clear_preview(self):
        self._preview = None
        self._preview_token = ""

    @Slot()
    def _shared_changed(self):
        if not self._closing:
            self.changed.emit()

    @Slot()
    def _collection_changed(self):
        self._collection_epoch += 1
        if not self._closing and self._preview is not None:
            self._clear_preview()
            self._message = "收藏数据已改变，请重新预览。"
            self.changed.emit()

    def begin_shutdown(self):
        if self._closing:
            return
        self._closing = True
        self._closing_event.set()
        self._choice = None
        self._clear_preview()
        if self._cancel_event is not None:
            self._cancel_event.set()

    def shutdown(self):
        if self._closed:
            return
        if not self._closing:
            self.begin_shutdown()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        if self._active is not None:
            self._access.finish_external_write(self._active.job_id)
        self._active = None
        self._cancel_event = None
        self._closed = True

    @property
    def shutdown_completed(self) -> bool:
        return self._closed and (self._thread is None or not self._thread.isRunning())
