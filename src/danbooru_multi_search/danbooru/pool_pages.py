"""One bounded pool-page metadata operation on the shared task queue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager

from danbooru_multi_search.core.pool_pages import POOL_WINDOW_SIZE, PoolPageBatch, PoolPageSlot
from danbooru_multi_search.core.pools import PoolError, PoolPageReference
from danbooru_multi_search.danbooru.client import build_posts_request
from danbooru_multi_search.danbooru.pool_post_parsing import parse_pool_posts_response
from danbooru_multi_search.danbooru.pool_parsing import MAX_POOL_RESPONSE_BYTES
from danbooru_multi_search.danbooru.pools import _QtPoolRequestAttempt
from danbooru_multi_search.tasks.model import TaskAttemptResult, TaskFinalResult, TaskKind, TaskRecord
from danbooru_multi_search.tasks.queue import BoundedTaskQueue, TaskQueueFullError, TaskQueueShutdownError


@dataclass(frozen=True, slots=True)
class PoolPageRequest:
    request_id: str
    references: tuple[PoolPageReference, ...]


class PoolPageRequestService(QObject):
    """Read only explicitly requested positions; never fetch adjacent windows.

    accepted(request) is published after queue registration and before HTTP.
    completed(request, batch, error, error_code) publishes one terminal result.
    Both signals permit synchronous replacement, cancellation and shutdown.
    """

    accepted = Signal(object)
    completed = Signal(object, object, str, str)

    def __init__(
        self, base_url: str, task_queue: BoundedTaskQueue,
        network_manager: QNetworkAccessManager, parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        endpoint = QUrl(base_url)
        endpoint.setQuery(None)
        endpoint.setFragment(None)
        self._base_url = endpoint.toString()
        self._queue = task_queue
        self._manager = network_manager
        self._request: PoolPageRequest | None = None
        self._task_id: str | None = None
        self._closed = False

    def fetch_pages(self, references: tuple[PoolPageReference, ...]) -> PoolPageRequest:
        """Fetch at most 40 positions, deduplicating only the HTTP ID filter."""
        if (
            not isinstance(references, tuple) or not 1 <= len(references) <= POOL_WINDOW_SIZE
            or not all(isinstance(ref, PoolPageReference) for ref in references)
            or len({ref.pool_id for ref in references}) != 1
            or len({ref.page_number for ref in references}) != len(references)
        ):
            raise PoolError("invalid_page", "请求页面位置无效或超过 40 项。")
        ids = tuple(dict.fromkeys(ref.post_id for ref in references))
        # Official post_query_builder_test.rb covers id:comma,list and status:any.
        request = build_posts_request(self._base_url, "id:" + ",".join(map(str, ids)) + " status:any", limit=len(ids))
        if self._closed:
            raise RuntimeError("作品池页面服务已经关闭。")
        previous_task = self._task_id
        operation = PoolPageRequest(uuid4().hex, references)
        self._request = operation
        self._task_id = None
        if previous_task is not None:
            self._queue.cancel(previous_task)
        if self._closed or self._request is not operation:
            return operation
        submitted = False
        deferred: list[TaskFinalResult] = []

        def current() -> bool:
            return not self._closed and self._request is operation

        def parse(payload: bytes) -> PoolPageBatch:
            values = {value.post_id: value for value in parse_pool_posts_response(payload, ids)}
            return PoolPageBatch(tuple(PoolPageSlot(ref, values[ref.post_id]) for ref in references))

        def starter(attempt: int, completion: Callable[[TaskAttemptResult], None]):
            if not current():
                return None
            return _QtPoolRequestAttempt(self._manager, request, completion, self,
                                         parser=parse, maximum_bytes=MAX_POOL_RESPONSE_BYTES)

        def state_changed(record: TaskRecord) -> None:
            if not current():
                if not record.terminal:
                    self._queue.cancel(record.identifier)
                return
            if self._task_id is None:
                self._task_id = record.identifier
                self.accepted.emit(operation)
                if not current() and not record.terminal:
                    self._queue.cancel(record.identifier)

        def finish(result: TaskFinalResult) -> None:
            if not current() or result.task_id != self._task_id:
                return
            self._request = None
            self._task_id = None
            if result.error_code:
                self.completed.emit(operation, None, result.error or "页面请求失败。", result.error_code)
            elif isinstance(result.value, PoolPageBatch):
                self.completed.emit(operation, result.value, "", "")
            else:
                self.completed.emit(operation, None, "页面请求结果无效。", "invalid_response")

        def completed(result: TaskFinalResult) -> None:
            if submitted:
                finish(result)
            else:
                deferred.append(result)

        try:
            record = self._queue.submit(TaskKind.METADATA, starter, completed, state_listener=state_changed)
        except (TaskQueueFullError, TaskQueueShutdownError) as exc:
            if current():
                self._request = None
                self.completed.emit(operation, None, f"页面请求无法加入队列：{exc}", "queue_rejected")
            return operation
        if current():
            self._task_id = record.identifier
        submitted = True
        for result in deferred:
            finish(result)
        return operation

    def cancel_current(self, expected_request_id: str | None = None) -> bool:
        """Invalidate before queue callbacks; only cancel this service's task."""
        operation = self._request
        if operation is None or (expected_request_id is not None and expected_request_id != operation.request_id):
            return False
        task_id = self._task_id
        self._request = None
        self._task_id = None
        if task_id is not None:
            self._queue.cancel(task_id)
        return True

    def shutdown(self) -> None:
        self._closed = True
        self.cancel_current()
