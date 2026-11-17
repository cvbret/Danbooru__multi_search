"""Persistent download coordination over the application's shared media queue."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QNetworkAccessManager

from danbooru_multi_search.core.downloads import (
    DEFAULT_MAX_DOWNLOAD_BYTES, MAX_DOWNLOAD_BATCH_ITEMS, DOWNLOAD_TERMINAL_STATES,
    DownloadBatchSnapshot, DownloadConflictPolicy, DownloadedFile, DownloadError,
    DownloadItemSnapshot, DownloadRequest, DownloadTaskState,
)
from danbooru_multi_search.danbooru.download import DownloadAttemptHooks, build_media_request, start_media_download_attempt
from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.storage.download_files import commit_file, discard_owned_partial, partial_path, resolve_destination, target_key
from danbooru_multi_search.storage.downloads import DownloadRepository
from danbooru_multi_search.tasks.model import TaskAttemptResult, TaskFinalResult, TaskKind, TaskRecord, TaskState
from danbooru_multi_search.tasks.queue import BoundedTaskQueue, TaskQueueFullError, TaskQueueShutdownError
from danbooru_multi_search.tasks.qt_scheduling import QtScheduler
from danbooru_multi_search.tasks.scheduling import ScheduledCall, Scheduler


@dataclass(slots=True)
class _Entry:
    item: DownloadItemSnapshot
    target: str
    queue_id: str | None = None


class PersistentDownloadManager(QObject):
    """Persist first, feed a bounded window, and never auto-resume on startup."""

    itemChanged = Signal(object)
    batchChanged = Signal(object)
    errorOccurred = Signal(str)

    def __init__(self, repository: DownloadRepository, task_queue: BoundedTaskQueue,
                 network_manager: QNetworkAccessManager, *, scheduler: Scheduler | None = None,
                 timeout_ms: int = 15000, maximum_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        if type(timeout_ms) is not int or timeout_ms <= 0 or type(maximum_bytes) is not int or maximum_bytes <= 0:
            raise ValueError("download timeout and size limits must be positive integers")
        self._repository = repository
        self._task_queue = task_queue
        self._network_manager = network_manager
        self._scheduler = scheduler or QtScheduler(self)
        self._timeout_ms = timeout_ms
        self._maximum_bytes = maximum_bytes
        self._pending: list[str] = []
        self._entries: dict[str, _Entry] = {}
        self._cancelling_batches: set[str] = set()
        self._poll: ScheduledCall | None = None
        self._pumping = False
        self._pump_again = False
        self._closed = False
        self._closing = False
        self._shutdown_ids: tuple[str, ...] = ()
        self._halted = False
        self._halting = False
        self._error = ""
        try:
            repository.recover_unfinished()
        except DownloadError as exc:
            self._halted = True
            self._error = str(exc)

    @property
    def error(self) -> str:
        return self._error

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def halted(self) -> bool:
        return self._halted

    def enqueue_download(self, post: DanbooruPost, directory: Path,
                         policy: DownloadConflictPolicy = DownloadConflictPolicy.SKIP) -> DownloadBatchSnapshot:
        return self.enqueue_batch((post,), directory, policy)

    def enqueue_batch(self, posts: Sequence[DanbooruPost], directory: Path,
                      policy: DownloadConflictPolicy = DownloadConflictPolicy.SKIP) -> DownloadBatchSnapshot:
        if self._closed or self._closing or self._halted:
            raise DownloadError("manager_unavailable", self._error or "下载管理器已经关闭。")
        if not isinstance(posts, Sequence) or isinstance(posts, (str, bytes)) or not 1 <= len(posts) <= MAX_DOWNLOAD_BATCH_ITEMS:
            raise DownloadError("invalid_batch", "下载批量必须明确包含 1 至 1,000 项。")
        requests = []
        for post in posts:
            if not isinstance(post, DanbooruPost):
                raise DownloadError("invalid_request", "批量下载包含无效作品。")
            requests.append(DownloadRequest(post.id, post.download_url, post.file_ext))
        try:
            batch = self._repository.create_batch(tuple(requests), Path(directory).expanduser().resolve(), policy)
        except DownloadError as exc:
            self._handle_error(exc)
            raise
        self._pending.extend(item.item_id for item in batch.items)
        self.batchChanged.emit(batch)
        self._pump()
        return batch

    def snapshot(self, batch_id: str) -> DownloadBatchSnapshot:
        return self._repository.get_batch(batch_id)

    def schedule_persisted_batch(self, batch_id: str) -> None:
        """Feed an already committed finite batch once, using the normal file pipeline."""
        if self._closed or self._closing or self._halted:
            raise DownloadError("manager_unavailable", self._error or "下载管理器已经关闭。")
        batch = self._repository.get_batch(batch_id)
        if not 1 <= len(batch.items) <= MAX_DOWNLOAD_BATCH_ITEMS:
            raise DownloadError("invalid_batch", "已保存下载批次的数量无效。")
        known = set(self._pending) | self._entries.keys()
        self._pending.extend(item.item_id for item in batch.items
                             if item.state is DownloadTaskState.WAITING and item.item_id not in known)
        self.batchChanged.emit(batch)
        self._pump()

    def cancel(self, item_id: str, *, expected_generation: int | None = None) -> bool:
        if self._closed or self._closing or self._halting:
            return False
        try:
            item = self._repository.get_item(item_id)
            if item.state in DOWNLOAD_TERMINAL_STATES:
                return False
            item = self._repository.cancel(item_id, item.generation if expected_generation is None else expected_generation)
            self._pending = [value for value in self._pending if value != item_id]
            entry = self._entries.get(item_id)
            if entry:
                entry.item = item
                if entry.queue_id:
                    self._task_queue.cancel(entry.queue_id, "User cancelled the persistent download.")
            else:
                discard_owned_partial(item)
                self._publish(item)
            self._pump()
            return True
        except DownloadError as exc:
            self._handle_error(exc)
            return False

    def cancel_batch(self, batch_id: str, *, expected_generations: dict[str, int] | None = None) -> bool:
        if self._closed or self._closing or batch_id in self._cancelling_batches:
            return False
        self._cancelling_batches.add(batch_id)
        try:
            batch = self._repository.cancel_batch(batch_id, expected_generations=expected_generations)
            identifiers = {item.item_id for item in batch.items}
            self._pending = [item_id for item_id in self._pending if item_id not in identifiers]
            # Update every runtime guard before cancelling the first queue item.
            for item in batch.items:
                if item.item_id in self._entries:
                    self._entries[item.item_id].item = item
            active_ids = set(self._entries).intersection(identifiers)
            for item in batch.items:
                entry = self._entries.get(item.item_id)
                if entry and entry.queue_id:
                    self._task_queue.cancel(entry.queue_id, "User cancelled the entire download batch.")
            cleaned = True
            for item in batch.items:
                if item.item_id not in active_ids and item.state is DownloadTaskState.CANCELLED:
                    try:
                        discard_owned_partial(item)
                        self._publish(item)
                    except DownloadError as exc:
                        cleaned = False
                        self._repository.note_cleanup_error(item.item_id, item.generation, str(exc))
                        self._handle_error(exc)
            return cleaned
        except DownloadError as exc:
            self._handle_error(exc)
            return False
        finally:
            self._cancelling_batches.discard(batch_id)
            self._pump()

    def retry(self, item_id: str, *, expected_generation: int | None = None) -> bool:
        if item_id in self._entries:
            return False
        try:
            expected = None if expected_generation is None else {item_id: expected_generation}
            generations = self._allow_manual_resume(expected)
            if generations is None:
                return False
            item = self._repository.get_item(item_id)
            generation = generations.get(item_id, item.generation)
            if item.generation != generation:
                raise DownloadError("stale_download", "下载操作已经失效，请重新选择。")
            if item.batch_id in self._cancelling_batches:
                return False
            discard_owned_partial(item)
            item = self._repository.prepare_retry(item_id, generation)
            self._pending.append(item_id)
            self._publish(item)
            self._pump()
            return True
        except DownloadError as exc:
            self._handle_error(exc)
            return False

    def resume_batch(self, batch_id: str, *, expected_generations: dict[str, int] | None = None) -> bool:
        if batch_id in self._cancelling_batches:
            return False
        try:
            generations = self._allow_manual_resume(expected_generations)
            if generations is None:
                return False
            expected = generations if expected_generations is not None else None
            batch = self.snapshot(batch_id)
            if expected is not None and {item.item_id: item.generation for item in batch.items} != expected:
                raise DownloadError("stale_download", "下载批次已经改变，请重新选择。")
            interrupted = tuple(item for item in batch.items if item.state is DownloadTaskState.AWAITING_RESUME)
            for item in interrupted:
                discard_owned_partial(item)
            waiting = self._repository.prepare_batch_resume(batch_id, interrupted, expected_generations=expected)
            self._pending.extend(item.item_id for item in waiting)
            for item in waiting:
                self._publish(item)
            self._pump()
            return bool(waiting)
        except DownloadError as exc:
            self._handle_error(exc)
            return False

    def _allow_manual_resume(self, expected: dict[str, int] | None = None) -> dict[str, int] | None:
        if self._closed or self._closing or self._halting:
            return None
        if self._halted:
            generations = self._repository.recover_unfinished(expected_generations=expected)
            self._halted = False
            self._error = ""
            return generations
        return dict(expected or {})

    def _pump(self) -> None:
        if self._closed or self._closing or self._halted:
            return
        if self._pumping:
            self._pump_again = True
            return
        self._pumping = True
        try:
            while True:
                self._pump_again = False
                for item_id in tuple(self._pending):
                    if self._closed or self._closing or self._halted or len(self._entries) >= self._task_queue.config.media_concurrency:
                        break
                    try:
                        item = self._repository.get_item(item_id)
                        if item.batch_id in self._cancelling_batches:
                            continue
                        if item.state is not DownloadTaskState.WAITING:
                            self._pending.remove(item_id)
                            continue
                        destination = resolve_destination(item, {entry.target for entry in self._entries.values()})
                        if destination.blocked:
                            continue
                        item = self._repository.set_destination(item_id, item.generation, destination.path.name)
                        if destination.skipped:
                            item = self._repository.finish(item_id, item.generation, DownloadTaskState.SKIPPED, message="已有同名文件，已跳过。")
                            self._pending.remove(item_id)
                            self._publish(item)
                            continue
                        entry = _Entry(item, target_key(destination.path))
                        self._pending.remove(item_id)
                        self._entries[item_id] = entry
                        try:
                            self._task_queue.submit(TaskKind.MEDIA,
                                lambda number, callback, current=entry: self._start(current, callback),
                                lambda result, current=entry: self._completed(current, result),
                                state_listener=lambda record, current=entry: self._queue_state(current, record))
                        except TaskQueueFullError:
                            self._release(entry)
                            self._pending.insert(0, item_id)
                            self._schedule_poll()
                            break
                        except TaskQueueShutdownError as exc:
                            self._release(entry)
                            self._repository.pause(item_id, item.generation)
                            self._handle_error(DownloadError("queue_closed", str(exc)))
                    except DownloadError as exc:
                        if exc.code in {"storage_error", "invalid_record"}:
                            self._storage_failure(exc)
                            break
                        if item_id in self._pending:
                            self._pending.remove(item_id)
                        current = self._repository.get_item(item_id)
                        failed = self._repository.finish(item_id, current.generation, DownloadTaskState.FAILED, error_code=exc.code, message=str(exc), clear_partial=False)
                        self._publish(failed)
                if not self._pump_again or self._closed or self._halted:
                    break
            if self._pending and not self._entries:
                self._schedule_poll()
        except DownloadError as exc:
            self._storage_failure(exc)
        finally:
            self._pumping = False

    def _schedule_poll(self) -> None:
        if self._closed or self._closing or self._halted or self._poll is not None:
            return
        def poll() -> None:
            self._poll = None
            self._pump()
        self._poll = self._scheduler.call_later(0.25, poll)

    def _queue_state(self, entry: _Entry, record: TaskRecord) -> None:
        entry.queue_id = record.identifier
        if not self._current(entry):
            if not record.terminal:
                self._task_queue.cancel(record.identifier)
            return
        if record.state is TaskState.RETRY_WAIT:
            try:
                entry.item = self._repository.set_retry_wait(entry.item.item_id, entry.item.generation, record.message or "等待自动重试。")
                self._publish(entry.item)
            except DownloadError as exc:
                self._storage_failure(exc)

    def _start(self, entry: _Entry, completion):
        if not self._current(entry):
            completion(TaskAttemptResult.failure("下载操作已失效。", error_code="cancelled"))
            return None
        try:
            discard_owned_partial(entry.item)
            entry.item = self._repository.begin_attempt(entry.item.item_id, entry.item.generation)
            generation = entry.item.generation
            self._publish(entry.item)
            if not self._current(entry, generation):
                completion(TaskAttemptResult.failure("下载操作已失效。", error_code="cancelled"))
                return None
            def register(path: Path) -> None:
                entry.item = self._repository.register_partial(entry.item.item_id, generation, path.name)
            def progress(written: int, total: int | None) -> None:
                if not self._current(entry, generation):
                    raise DownloadError("stale_download", "下载回调已经失效。")
                try:
                    entry.item = self._repository.update_progress(entry.item.item_id, generation, written, total)
                    self._publish(entry.item)
                except DownloadError as exc:
                    self._storage_failure(exc)
                    raise
            def commit(partial: Path, destination: Path) -> None:
                if not self._current(entry, generation):
                    raise OSError("Download was cancelled before commit.")
                commit_file(partial, destination, entry.item.policy)
            return start_media_download_attempt(self._network_manager,
                build_media_request(entry.item.request.source_url, timeout_ms=self._timeout_ms),
                entry.item.destination, entry.item.request.source_url, self._maximum_bytes,
                completion, self, partial=partial_path(entry.item), hooks=DownloadAttemptHooks(
                    progress=progress, partial_ready=register, can_continue=lambda: self._current(entry, generation),
                    commit=commit, cleanup_error=lambda message: self._cleanup_error(entry, message)))
        except DownloadError as exc:
            if exc.code in {"storage_error", "invalid_record"}:
                self._storage_failure(exc)
            completion(TaskAttemptResult.failure(str(exc), error_code=exc.code))
        except (OSError, ValueError) as exc:
            completion(TaskAttemptResult.failure(str(exc), error_code="file_error"))
        return None

    def _completed(self, entry: _Entry, result: TaskFinalResult) -> None:
        if self._entries.get(entry.item.item_id) is not entry:
            return
        if self._closed or self._closing or self._halted:
            self._release(entry)
            return
        try:
            item = entry.item
            if item.state is not DownloadTaskState.CANCELLED:
                if result.state is TaskState.SUCCEEDED and isinstance(result.value, DownloadedFile):
                    item = self._repository.update_progress(item.item_id, item.generation, result.value.bytes_written, result.value.bytes_written)
                    item = self._repository.finish(item.item_id, item.generation, DownloadTaskState.COMPLETED)
                else:
                    state = DownloadTaskState.CANCELLED if result.state is TaskState.CANCELLED else DownloadTaskState.FAILED
                    item = self._repository.finish(item.item_id, item.generation, state,
                        error_code=result.error_code or "download_error", message=result.error or "下载失败。", clear_partial=False)
            self._release(entry)
            self._publish(item)
            self._pump()
        except DownloadError as exc:
            # A file may already be committed. Keep it and stop all subsequent
            # requests instead of reporting a success that was never persisted.
            self._storage_failure(exc)
            self._release(entry)

    def _current(self, entry: _Entry, generation: int | None = None) -> bool:
        return (not self._closed and not self._closing and not self._halted and self._entries.get(entry.item.item_id) is entry
                and entry.item.state not in DOWNLOAD_TERMINAL_STATES
                and (generation is None or entry.item.generation == generation))

    def _release(self, entry: _Entry) -> None:
        if self._entries.get(entry.item.item_id) is entry:
            self._entries.pop(entry.item.item_id)

    def _publish(self, item: DownloadItemSnapshot) -> None:
        if self._closed or self._closing or self._halted:
            return
        self.itemChanged.emit(item)
        if not self._closed and not self._closing and not self._halted:
            self.batchChanged.emit(self._repository.get_batch(item.batch_id))

    def _cleanup_error(self, entry: _Entry, message: str) -> None:
        self._error = f"下载临时文件清理失败：{message}"
        try:
            entry.item = self._repository.note_cleanup_error(entry.item.item_id, entry.item.generation, self._error)
        except DownloadError as exc:
            self._storage_failure(exc)
        if not self._closed and not self._closing:
            self.errorOccurred.emit(self._error)

    def _handle_error(self, error: DownloadError) -> None:
        if error.code in {"storage_error", "invalid_record"}:
            self._storage_failure(error)
        else:
            self._error = str(error)
            if not self._closed and not self._closing:
                self.errorOccurred.emit(self._error)

    def _storage_failure(self, error: DownloadError) -> None:
        if self._halting:
            return
        self._halting = True
        self._halted = True
        self._error = str(error)
        try:
            if self._poll:
                self._poll.cancel()
                self._poll = None
            self._pending.clear()
            for entry in tuple(self._entries.values()):
                if entry.queue_id:
                    self._task_queue.cancel(entry.queue_id, "下载记录保存失败，已停止后续请求。")
            if not self._closed and not self._closing:
                self.errorOccurred.emit(self._error)
        finally:
            self._halting = False

    def begin_shutdown(self) -> None:
        """Stop requests immediately, before waiting for another database writer."""
        if self._closed or self._closing:
            return
        self._closing = True
        self._shutdown_ids = tuple(dict.fromkeys((*self._pending, *self._entries)))
        self._pending.clear()
        if self._poll:
            self._poll.cancel()
            self._poll = None
        for entry in tuple(self._entries.values()):
            if entry.queue_id:
                self._task_queue.cancel(entry.queue_id, "Application is shutting down.")
        self._entries.clear()

    def shutdown(self) -> None:
        if self._closed:
            return
        if not self._closing:
            self.begin_shutdown()
        for item_id in self._shutdown_ids:
            try:
                item = self._repository.get_item(item_id)
                if item.state not in DOWNLOAD_TERMINAL_STATES:
                    self._repository.pause(item_id, item.generation)
            except DownloadError as exc:
                self._error = str(exc)
        self._shutdown_ids = ()
        self._closed = True
