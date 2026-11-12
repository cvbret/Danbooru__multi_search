"""Virtualized Qt projections of download summaries and one selected batch."""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QByteArray, QModelIndex, Property, Qt, Signal

from danbooru_multi_search.core.downloads import DOWNLOAD_TERMINAL_STATES, DownloadTaskState


STATE_TEXT = {"waiting": "等待下载", "running": "下载中", "retry_wait": "等待自动重试",
              "awaiting_resume": "等待手动恢复", "completed": "已完成", "skipped": "已有文件，已跳过",
              "failed": "下载失败", "cancelled": "已取消"}
ERROR_TEXT = {"http": "服务器返回错误", "network": "网络连接失败", "timeout": "下载超时",
              "empty_response": "未收到文件内容", "response_too_large": "文件超过下载大小限制",
              "file_error": "无法完成文件保存，请检查或更换下载目录", "file_cleanup_error": "临时文件清理失败",
              "storage_error": "下载记录保存失败，已停止后续请求", "invalid_record": "下载记录无效",
              "stale_download": "下载状态已经改变，请重新选择操作", "cancelled": "用户已取消下载"}


def bytes_text(value: int | None) -> str:
    if value is None:
        return "未知大小"
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value / (1024 * 1024):.1f} MiB"


class _Rows(QAbstractListModel):
    changed = Signal()
    names: tuple[str, ...] = ()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = ()

    @Property(int, notify=changed)
    def count(self):
        return len(self.items)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def roleNames(self):
        return {int(Qt.UserRole) + index + 1: QByteArray(name.encode()) for index, name in enumerate(self.names)}

    def data(self, index, role=int(Qt.DisplayRole)):
        number = role - int(Qt.UserRole) - 1
        if not index.isValid() or not 0 <= index.row() < len(self.items) or not 0 <= number < len(self.names):
            return None
        return self.mapping(index.row()).get(self.names[number])

    def replace(self, items):
        items = tuple(items)
        if items == self.items:
            return
        before_keys = tuple(self.key(item) for item in self.items)
        keys = tuple(self.key(item) for item in items)
        if keys == before_keys:
            self.items = items
            if items:
                self.dataChanged.emit(self.index(0), self.index(len(items) - 1), [])
        elif keys[:len(before_keys)] == before_keys and len(items) > len(self.items):
            self.beginInsertRows(QModelIndex(), len(self.items), len(items) - 1)
            self.items = items
            self.endInsertRows()
        else:
            self.beginResetModel()
            self.items = items
            self.endResetModel()
        self.changed.emit()


class DownloadBatchListModel(_Rows):
    names = ("batchKey", "titleText", "summaryText", "progressText", "progressValue")

    @staticmethod
    def key(item):
        return item.batch_id

    def mapping(self, row):
        item = self.items[row]
        counts = dict(item.state_counts)
        finished = sum(counts.get(state.value, 0) for state in DOWNLOAD_TERMINAL_STATES)
        return {"batchKey": item.batch_id, "titleText": f"下载批次 · {item.count} 项 · {item.batch_id[:8]}",
                "summaryText": " · ".join(f"{STATE_TEXT[state]} {count}" for state, count in item.state_counts if count),
                "progressText": f"已结束 {finished}/{item.count} · {bytes_text(item.bytes_written)}",
                "progressValue": finished / item.count if item.count else 0.0}


class DownloadItemListModel(_Rows):
    names = ("itemKey", "generationKey", "postKey", "extensionText", "fileName", "targetText", "stateCode", "stateText",
             "progressText", "progressValue", "indeterminate", "attemptsText", "reasonText", "detailText", "canCancel", "canRetry")

    @staticmethod
    def key(item):
        return item.item_id

    def mapping(self, row):
        item = self.items[row]
        return {"itemKey": item.item_id, "generationKey": str(item.generation), "postKey": str(item.request.post_id),
                "extensionText": item.request.file_ext.upper(), "fileName": item.resolved_name or item.request.filename,
                "targetText": str(item.destination or item.directory / item.request.filename),
                "stateCode": item.state.value, "stateText": STATE_TEXT[item.state.value],
                "progressText": f"{bytes_text(item.bytes_written)} / {bytes_text(item.total_bytes)}",
                "progressValue": min(1.0, item.bytes_written / item.total_bytes) if item.total_bytes else 0.0,
                "indeterminate": item.state is DownloadTaskState.RUNNING and item.total_bytes is None,
                "attemptsText": f"累计尝试 {item.attempts} 次", "reasonText": ERROR_TEXT.get(item.error_code, "下载未能完成" if item.state is DownloadTaskState.FAILED else ""),
                "detailText": item.message, "canCancel": item.state not in DOWNLOAD_TERMINAL_STATES,
                "canRetry": item.state in {DownloadTaskState.FAILED, DownloadTaskState.CANCELLED, DownloadTaskState.AWAITING_RESUME}}
