"""Session-only Eagle view state and user action identities."""

from uuid import uuid4

from PySide6.QtCore import QObject, Property, Signal, Slot

from danbooru_multi_search.app.eagle_runtime import EagleRuntime
from danbooru_multi_search.app.eagle_service import (
    EagleOperation,
    EagleRequest,
    EagleResult,
    EagleSnapshot,
)
from danbooru_multi_search.ui.eagle_messages import result_message


class EagleController(QObject):
    changed = Signal()
    foldersChanged = Signal()

    def __init__(self, runtime: EagleRuntime, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.runtime = runtime
        self._snapshot: EagleSnapshot | None = None
        self._revision = 0
        self._target: tuple[str, str | None] | None = None
        self._operation: str | None = None
        self._closed = False
        self._result: EagleResult | None = None
        runtime.changed.connect(self._runtime_changed)
        runtime.completed.connect(self._finished)

    @Property(bool, notify=changed)
    def supported(self) -> bool:
        return self.runtime.availability.available

    @Property(bool, notify=changed)
    def busy(self) -> bool:
        return self._operation is not None or self.runtime.busy

    @Property(bool, notify=changed)
    def ready(self) -> bool:
        return not self._closed and self.supported and self._snapshot is not None

    @Property(int, notify=changed)
    def revision(self) -> int:
        return self._revision

    @Property(str, notify=changed)
    def statusText(self) -> str:
        if not self.supported:
            return "当前系统不支持 Eagle 本地集成。"
        if self.busy:
            return "正在取消，等待当前本机请求结束…" if self.runtime.cancelling else "等待本机 Eagle 请求队列…"
        if self._result is None:
            return "尚未连接；点击“测试连接 / 刷新”后才会访问本机 Eagle。"
        return result_message(self._result)

    @Property(str, notify=changed)
    def detailsText(self) -> str:
        return self._result.diagnostic if self._result else ""

    @Property(bool, notify=changed)
    def uncertain(self) -> bool:
        return bool(self._result and self._result.uncertain)

    @Property(dict, notify=changed)
    def library(self) -> dict:
        snapshot = self._snapshot
        return {
            "name": snapshot.library.name if snapshot else "",
            "path": str(snapshot.library.path) if snapshot else "",
            "version": snapshot.connection.version if snapshot else "",
            "build": (snapshot.connection.build_version or "") if snapshot else "",
        }

    @Property(list, notify=foldersChanged)
    def folders(self) -> list:
        if not self.ready:
            return []
        labels: dict[str, str] = {}
        rows = [{"key": "root", "name": "素材库根目录"}]
        for folder in self._snapshot.folders:
            prefix = labels.get(folder.parent_identifier, "") if folder.parent_identifier else ""
            path = prefix + " / " + folder.name if prefix else folder.name
            # A bounded list must not expand deep ancestry into unbounded labels.
            if len(path) > 300:
                path = "…/" + path[-298:]
            labels[folder.identifier] = path
            rows.append({"key": "folder:" + folder.identifier, "name": path})
        return rows

    @Property(dict, notify=changed)
    def target(self) -> dict:
        key = ""
        name = "尚未选择目标文件夹"
        if self.ready and self._target and self._target[0] == self._snapshot.library.identifier:
            identifier = self._target[1]
            folder = next((folder for folder in self._snapshot.folders if folder.identifier == identifier), None)
            if identifier is None or folder is not None:
                key = "root" if identifier is None else "folder:" + identifier
                name = "素材库根目录" if identifier is None else folder.name
        return {"key": key, "name": name, "valid": bool(key)}

    def selected_target(self, revision: int) -> tuple[str, str | None] | None:
        """Capture an explicitly selected target from the displayed folder revision."""
        return self._target if revision == self._revision and self.target["valid"] else None

    @Slot(result=bool)
    def refresh(self) -> bool:
        return self._start(EagleRequest(uuid4().hex, EagleOperation.REFRESH))

    @Slot(str, int, result=bool)
    def selectTarget(self, key: str, revision: int) -> bool:
        if not self.ready or self.busy or revision != self._revision:
            return False
        identifier = self._folder_id(key)
        if key != "root" and identifier is None:
            return False
        target = (self._snapshot.library.identifier, identifier)
        self._target = target
        self._revision += 1
        selected_revision = self._revision
        self.changed.emit()
        return self.ready and self._revision == selected_revision and self._target == target

    def _folder_id(self, key: str) -> str | None:
        if key.startswith("folder:"):
            if any("folder:" + folder.identifier == key for folder in self._snapshot.folders):
                return key[7:]
        return None

    @Slot(str, str, int, result=bool)
    def createFolder(self, name: str, parent_key: str, revision: int) -> bool:
        if (
            not self.ready or self.busy or revision != self._revision
            or not isinstance(name, str) or not name.strip()
            or self.target["key"] != parent_key
        ):
            return False
        parent = self._folder_id(parent_key)
        if parent_key != "root" and parent is None:
            return False
        return self._start(EagleRequest(
            uuid4().hex, EagleOperation.CREATE_FOLDER,
            self._snapshot.library.identifier, name, parent,
        ))

    def _start(self, request: EagleRequest) -> bool:
        if self._closed or not self.supported or self.busy:
            return False
        self._operation = request.request_id
        self._snapshot = None
        self._result = None
        self._revision += 1
        self.changed.emit()
        if self._operation != request.request_id or self._closed:
            return False
        self.foldersChanged.emit()
        if self._operation != request.request_id or self._closed:
            return False
        return self.runtime.start(request)

    @Slot(object)
    def _finished(self, result: EagleResult) -> None:
        if self._closed or self._operation != result.request_id:
            return
        self._operation = None
        self._snapshot = result.snapshot
        self._result = result
        self._revision += 1
        revision = self._revision
        self.changed.emit()
        if not self._closed and self._operation is None and self._revision == revision:
            self.foldersChanged.emit()

    @Slot()
    def _runtime_changed(self) -> None:
        if not self._closed:
            self.changed.emit()

    @Slot(result=bool)
    def cancel(self) -> bool:
        if self._closed or self._operation is None:
            return False
        if self.runtime.active_request is not None:
            return self.runtime.cancel()
        request_id = self._operation
        self._operation = None
        self._result = EagleResult(request_id, error_code="eagle_cancelled")
        self._revision += 1
        self.changed.emit()
        return True

    def shutdown(self) -> None:
        self._closed = True
        self._operation = None
        self._revision += 1
