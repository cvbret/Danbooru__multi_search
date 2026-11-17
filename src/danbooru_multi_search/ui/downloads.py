"""Download page state, source identities, and explicit directory confirmation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Callable
from pathlib import Path
import re
from uuid import uuid4

from PySide6.QtCore import QObject, Property, QUrl, Signal, Slot

from danbooru_multi_search.core.downloads import MAX_DOWNLOAD_BATCH_ITEMS, DOWNLOAD_TERMINAL_STATES, DownloadConflictPolicy, DownloadError, DownloadRequest, DownloadTaskState
from danbooru_multi_search.core.settings import SettingsController, SettingsError
from danbooru_multi_search.danbooru.download_manager import PersistentDownloadManager
from danbooru_multi_search.danbooru.gallery import DanbooruPostListModel
from danbooru_multi_search.danbooru.details import DanbooruDetailController
from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.storage.downloads import DownloadBatchSummary, DownloadRepository
from danbooru_multi_search.ui.download_models import DownloadBatchListModel, DownloadItemListModel, ERROR_TEXT
from danbooru_multi_search.ui.gallery_selection import GallerySelectionController


@dataclass(frozen=True, slots=True)
class _Request:
    token: str
    posts: tuple[DanbooruPost, ...]
    source: str
    source_revision: int
    selection_revision: int
    directory: Path
    guard: Callable[[], bool] | None = None


class DownloadUiController(QObject):
    changed = Signal()
    queued = Signal(str)
    PAGE_SIZE = 50

    def __init__(self, manager: PersistentDownloadManager, repository: DownloadRepository,
                 settings: SettingsController, gallery: DanbooruPostListModel,
                 selection: GallerySelectionController, detail: DanbooruDetailController,
                 default_directory: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._manager, self._repository, self._settings = manager, repository, settings
        self._gallery, self._selection, self._detail = gallery, selection, detail
        self._fallback_directory = default_directory
        self._batches = DownloadBatchListModel(self)
        self._items = DownloadItemListModel(self)
        self._selected_key = ""
        self._selected_batch = None
        self._active = False
        self._loaded = False
        self._dirty = True
        self._more = False
        self._load_generation = 0
        self._detail_revision = 0
        self._request: _Request | None = None
        self._choice: tuple[str, str, str] | None = None
        self._batch_action = None
        self._busy = False
        self._error = ""
        self._details = ""
        self._notice = ""
        self._manager_error_revision = 0
        self._closed = False
        gallery.revisionChanged.connect(self._source_changed)
        selection.changed.connect(self._source_changed)
        detail.openChanged.connect(self._detail_changed)
        manager.batchChanged.connect(self._batch_changed)
        manager.errorOccurred.connect(self._manager_error)

    @Property(QObject, constant=True)
    def batchesModel(self):
        return self._batches

    @Property(QObject, constant=True)
    def itemsModel(self):
        return self._items

    @Property(str, notify=changed)
    def selectedBatchKey(self):
        return self._selected_key

    @Property(bool, notify=changed)
    def canLoadMore(self):
        return self._more

    @Property(str, notify=changed)
    def defaultDirectory(self):
        return self._settings.settings.download_directory or str(self._fallback_directory)

    @Property(QUrl, notify=changed)
    def defaultDirectoryUrl(self):
        return QUrl.fromLocalFile(self.defaultDirectory)

    @Property(str, notify=changed)
    def errorText(self):
        return self._error

    @Property(str, notify=changed)
    def errorDetails(self):
        return self._details

    @Property(str, notify=changed)
    def noticeText(self):
        return self._notice

    @Property(bool, notify=changed)
    def busy(self):
        return self._busy or self._closed

    @Property(str, notify=changed)
    def detailPostKey(self):
        return str(self._detail.current_post.id) if self._detail.isOpen and self._detail.current_post else ""

    @Property(int, notify=changed)
    def detailRevision(self):
        return self._detail_revision

    @Property(bool, notify=changed)
    def detailDownloadAvailable(self):
        return not self._closed and self._detail.isOpen and bool(self._detail.current_post and self._detail.current_post.download_url)

    @Property("QVariantMap", notify=changed)
    def pending(self):
        value = self._request
        return {"token": value.token if value else "", "count": len(value.posts) if value else 0,
                "directory": str(value.directory) if value else "", "directoryUrl": QUrl.fromLocalFile(str(value.directory)) if value else QUrl(),
                "valid": bool(value and self._source_current(value))}

    @Property(bool, notify=changed)
    def canCancelBatch(self):
        return bool(self._selected_batch and any(item.state not in DOWNLOAD_TERMINAL_STATES for item in self._selected_batch.items))

    @Property(bool, notify=changed)
    def canResumeBatch(self):
        return bool(self._selected_batch and (any(item.state is DownloadTaskState.AWAITING_RESUME for item in self._selected_batch.items)
                    or self._manager.halted and self.canCancelBatch))

    @Slot(bool)
    def setActive(self, active: bool) -> None:
        if self._closed:
            return
        self._active = active
        if active and (not self._loaded or self._dirty):
            self.reload()

    @Slot(result=bool)
    def reload(self) -> bool:
        if self._closed or not self._active:
            return False
        self._load_generation += 1
        generation = self._load_generation
        try:
            rows = self._repository.read_batch_summaries(limit=self.PAGE_SIZE)
            if self._closed or generation != self._load_generation:
                return False
            self._batches.replace(rows)
            if not self._load_current(generation):
                return False
            self._loaded, self._dirty, self._more = True, False, len(rows) == self.PAGE_SIZE
            key = self._selected_key if any(row.batch_id == self._selected_key for row in rows) else rows[0].batch_id if rows else ""
            if key:
                if not self.selectBatch(key):
                    return False
            else:
                self._selected_key, self._selected_batch = "", None
                self._items.replace(())
                if not self._load_current(generation):
                    return False
            if self._closed or not self._active:
                return False
            self.changed.emit()
            return True
        except DownloadError as exc:
            return self._fail(exc)

    @Slot(result=bool)
    def loadMoreBatches(self) -> bool:
        if self._closed or not self._active or not self._more:
            return False
        generation = self._load_generation
        try:
            rows = self._repository.read_batch_summaries(limit=self.PAGE_SIZE, offset=len(self._batches.items))
            if not self._load_current(generation):
                return False
            known = {item.batch_id for item in self._batches.items}
            self._batches.replace(self._batches.items + tuple(item for item in rows if item.batch_id not in known))
            if not self._load_current(generation):
                return False
            self._more = len(rows) == self.PAGE_SIZE
            self.changed.emit()
            return True
        except DownloadError as exc:
            return self._fail(exc)

    @Slot(str, result=bool)
    def selectBatch(self, key: str) -> bool:
        if self._closed or not any(item.batch_id == key for item in self._batches.items):
            return False
        return self._select_batch(key)

    @Slot(str, result=bool)
    def openPersistedBatch(self, key: str) -> bool:
        """Open an explicitly linked persistent batch without scanning older history."""
        if self._closed or not self._active or not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{32}", key) is None:
            return False
        return self._select_batch(key)

    def _select_batch(self, key: str) -> bool:
        self._selected_key = key
        self._batch_action = None
        self._load_generation += 1
        generation = self._load_generation
        try:
            batch = self._repository.get_batch(key)
            if self._closed or self._selected_key != key or generation != self._load_generation:
                return False
            self._selected_batch = batch
            self._items.replace(batch.items)
            if not self._load_current(generation) or self._selected_key != key:
                return False
            self.changed.emit()
            return True
        except DownloadError as exc:
            return self._fail(exc)

    @Slot(str, int, result="QVariantMap")
    def galleryDownloadState(self, key: str, revision: int):
        post = self._gallery_post(key, revision)
        return {"enabled": bool(post and post.download_url and not self._closed),
                "reason": "下载原文件" if post and post.download_url else "此作品没有原文件地址，不能下载预览图代替"}

    @Slot(str, int, result=str)
    def captureGalleryPost(self, key: str, revision: int):
        post = self._gallery_post(key, revision)
        return self._prepare((post,) if post else (), "gallery", revision, -1)

    @Slot(int, int, result=str)
    def captureGallerySelection(self, selection_revision: int, model_revision: int):
        posts = self._selection.selected_posts(selection_revision, model_revision)
        return self._prepare(posts or (), "selection", model_revision, selection_revision)

    @Slot(str, int, result=str)
    def captureDetailPost(self, key: str, revision: int):
        if revision != self._detail_revision or key != self.detailPostKey:
            return ""
        return self._prepare((self._detail.current_post,), "detail", revision, -1)

    def capture_snapshot(self, post: DanbooruPost, *, guard: Callable[[], bool]) -> str:
        """Prepare an explicit saved work; recheck its source through confirmation."""
        if not isinstance(post, DanbooruPost) or not guard():
            return ""
        return self._prepare((post,), "snapshot", -1, -1, guard=guard)

    def refresh_snapshot_source(self) -> None:
        """Publish invalidation after a reader changes page or leaves its source."""
        self._source_changed()

    def _prepare(self, posts, source: str, revision: int, selection_revision: int,
                 *, guard: Callable[[], bool] | None = None) -> str:
        if self._closed or self._busy:
            return ""
        self._request, self._choice = None, None
        if not posts:
            self._fail(DownloadError("invalid_request", "所选作品已经改变，或尚未选择作品。"))
            return ""
        if len(posts) > MAX_DOWNLOAD_BATCH_ITEMS:
            self._fail(DownloadError("invalid_batch", "每个下载批次最多包含 1,000 项，请减少选择数量。"))
            return ""
        try:
            for post in posts:
                if not post.download_url:
                    raise DownloadError("missing_original", f"作品 #{post.id} 没有原文件地址，不能用预览图代替。")
                DownloadRequest(post.id, post.download_url, post.file_ext)
            token = uuid4().hex
            value = _Request(token, tuple(posts), source, revision, selection_revision, Path(self.defaultDirectory), guard)
            if not self._source_current(value):
                raise DownloadError("stale_download", "作品来源已改变，请重新选择。")
            self._request = value
            self._error, self._details = "", ""
            self.changed.emit()
            return token if self._request is value and self._source_current(value) else ""
        except DownloadError as exc:
            self._fail(exc)
            return ""

    @Slot(str, result=bool)
    def openRequest(self, token: str):
        return bool(not self._closed and self._request and self._request.token == token and self._source_current(self._request))

    @Slot(str)
    def cancelRequest(self, token: str):
        if self._request and self._request.token == token:
            self._request, self._choice = None, None
            if not self._closed:
                self.changed.emit()

    @Slot(str, result=str)
    def beginDirectorySelection(self, purpose: str):
        if self._closed or self._busy or purpose not in {"request", "default"}:
            return ""
        request_token = self._request.token if self._request and self._source_current(self._request) else ""
        if purpose == "request" and not request_token:
            return ""
        token = uuid4().hex
        self._choice = (token, purpose, request_token)
        return token

    @Slot(str)
    def cancelDirectorySelection(self, token: str):
        if self._choice and self._choice[0] == token:
            self._choice = None

    @Slot(QUrl, str, result=bool)
    def acceptDirectory(self, url: QUrl, token: str):
        if self._closed or self._busy or not self._choice or self._choice[0] != token:
            return False
        _token, purpose, request_token = self._choice
        self._choice = None
        try:
            path = Path(url.toLocalFile()) if url.isLocalFile() else Path()
            if not path.is_absolute() or not path.is_dir():
                raise DownloadError("invalid_directory", "请选择有效的本地文件夹。")
            if purpose == "default":
                self._settings.set_download_directory(path)
                self._notice = "默认下载目录已保存。"
            elif self.openRequest(request_token):
                self._request = replace(self._request, directory=path)
            else:
                raise DownloadError("stale_download", "作品来源已改变，请重新选择。")
            if self._closed:
                return True
            self._error, self._details = "", ""
            self.changed.emit()
            return True
        except (DownloadError, SettingsError, OSError) as exc:
            return self._fail(exc)

    @Slot(str, str, bool, result=bool)
    def confirmRequest(self, token: str, policy: str, save_default: bool):
        value = self._request
        if self._closed or self._busy or not self.openRequest(token):
            return False
        self._busy = True
        self.changed.emit()
        try:
            if self._request is not value or not self._source_current(value) or self._closed:
                raise DownloadError("stale_download", "作品来源已改变，请重新选择。")
            parsed_policy = DownloadConflictPolicy(policy)
            if not value.directory.is_absolute() or value.directory.exists() and not value.directory.is_dir():
                raise DownloadError("invalid_directory", "下载目录已不可用，请重新选择文件夹。")
            if save_default:
                self._settings.set_download_directory(value.directory)
            # Saving settings may be supplied by an adapter that emits callbacks.
            if self._closed or self._request is not value or not self._source_current(value):
                raise DownloadError("stale_download", "作品来源已改变，尚未创建下载任务。")
            batch = self._manager.enqueue_batch(value.posts, value.directory, parsed_policy)
            if self._request is value:
                self._request, self._choice = None, None
            if not self._closed:
                self._error, self._details = "", ""
                self._notice = f"已保存 {len(batch.items)} 项下载任务，可在“下载”页面查看。"
                self.queued.emit(batch.batch_id)
            return True
        except (DownloadError, SettingsError, OSError, ValueError) as exc:
            return self._fail(exc)
        finally:
            self._busy = False
            if not self._closed:
                self.changed.emit()

    @Slot(str, str, result=bool)
    def cancelItem(self, key: str, generation: str):
        return self._item_action(key, generation, False)

    @Slot(str, str, result=bool)
    def retryItem(self, key: str, generation: str):
        return self._item_action(key, generation, True)

    def _item_action(self, key: str, generation: str, retry: bool):
        if self._closed or not any(item.item_id == key for item in self._items.items):
            return False
        try:
            if not isinstance(generation, str) or re.fullmatch(r"0|[1-9][0-9]{0,18}", generation) is None:
                raise DownloadError("stale_download", "下载操作已失效，请重新选择。")
            action = self._manager.retry if retry else self._manager.cancel
            error_revision = self._manager_error_revision
            result = action(key, expected_generation=int(generation))
            if error_revision != self._manager_error_revision:
                return result
            if not result:
                raise DownloadError("stale_download", "下载状态已经改变，请刷新后重新选择操作。")
            if self._closed:
                return True
            self._error, self._details = "", ""
            self.changed.emit()
            return True
        except DownloadError as exc:
            return self._fail(exc)

    @Slot(str, str, result=str)
    def captureBatchAction(self, key: str, action: str):
        if self._closed or not self._selected_batch or key != self._selected_key or action not in {"cancel", "resume"}:
            return ""
        token = uuid4().hex
        self._batch_action = (token, key, action, {item.item_id: item.generation for item in self._selected_batch.items})
        return token

    @Slot(str, result=bool)
    def cancelBatch(self, token: str):
        return self._run_batch_action(token, "cancel")

    @Slot(str, result=bool)
    def resumeBatch(self, token: str):
        return self._run_batch_action(token, "resume")

    def _run_batch_action(self, token: str, action: str):
        value = self._batch_action
        self._batch_action = None
        if self._closed or not value or value[:3] != (token, self._selected_key, action):
            return False
        method = self._manager.cancel_batch if action == "cancel" else self._manager.resume_batch
        error_revision = self._manager_error_revision
        result = method(value[1], expected_generations=value[3])
        if error_revision != self._manager_error_revision:
            return result
        if not result:
            return self._fail(DownloadError("stale_download", "下载状态已经改变，或当前记录无法保存，请刷新后重新操作。"))
        if self._closed:
            return True
        self._error, self._details = "", ""
        self.changed.emit()
        return True

    def _gallery_post(self, key: str, revision: int):
        if self._closed or revision != self._gallery.revision:
            return None
        return next((post for post in self._gallery.posts if str(post.id) == key), None)

    def _load_current(self, generation: int) -> bool:
        return not self._closed and self._active and generation == self._load_generation

    def _source_current(self, value: _Request) -> bool:
        if self._closed:
            return False
        if value.source == "snapshot":
            return value.guard is not None and value.guard()
        if value.source == "detail":
            return self._detail.isOpen and value.source_revision == self._detail_revision and self._detail.current_post is value.posts[0]
        if value.source == "selection":
            return self._selection.selected_posts(value.selection_revision, value.source_revision) == value.posts
        return self._gallery_post(str(value.posts[0].id), value.source_revision) is value.posts[0]

    @Slot()
    def _source_changed(self):
        if self._request and not self._source_current(self._request):
            self._request, self._choice = None, None
            self._error = "作品来源或选择已经改变，请重新选择下载作品。"
            if not self._closed:
                self.changed.emit()

    @Slot()
    def _detail_changed(self):
        self._detail_revision += 1
        self._source_changed()
        if not self._closed:
            self.changed.emit()

    @Slot(object)
    def _batch_changed(self, batch):
        if self._closed:
            return
        if not self._active:
            self._dirty = True
            return
        generation, selected_key = self._load_generation, self._selected_key
        if not self._batch_snapshot_current(batch):
            return
        summary = DownloadBatchSummary.from_batch(batch)
        rows = list(self._batches.items)
        found = next((index for index, row in enumerate(rows) if row.batch_id == batch.batch_id), None)
        if found is not None:
            rows[found] = summary
        elif all(item.generation == 0 and item.state is DownloadTaskState.WAITING for item in batch.items):
            rows.insert(0, summary)
        self._batches.replace(rows)
        if (not self._load_current(generation) or selected_key != self._selected_key
                or not self._batch_snapshot_current(batch)):
            return
        if batch.batch_id == self._selected_key:
            self._selected_batch = batch
            self._items.replace(batch.items)
        if (self._load_current(generation) and selected_key == self._selected_key
                and self._batch_snapshot_current(batch)):
            self.changed.emit()

    def _batch_snapshot_current(self, batch) -> bool:
        if batch.batch_id != self._selected_key:
            return True
        previous = {item.item_id: item for item in self._items.items}
        return not any(item.item_id in previous and (item.generation < previous[item.item_id].generation
            or item.generation == previous[item.item_id].generation and (item.bytes_written < previous[item.item_id].bytes_written
            or previous[item.item_id].state in DOWNLOAD_TERMINAL_STATES and item.state is not previous[item.item_id].state)) for item in batch.items)

    @Slot(str)
    def _manager_error(self, message: str):
        if not self._closed:
            self._manager_error_revision += 1
            self._error = "下载未能继续，请查看任务状态和详细原因。"
            self._details = message
            self.changed.emit()

    def _fail(self, error: Exception):
        if not self._closed:
            if isinstance(error, SettingsError):
                self._error = "默认下载目录保存失败，原设置保持不变。"
            else:
                self._error = ERROR_TEXT.get(getattr(error, "code", ""), str(error))
            self._details = str(error)
            self.changed.emit()
        return False

    def shutdown(self):
        self._closed = True
        self._request, self._choice, self._batch_action = None, None, None
        self._load_generation += 1
