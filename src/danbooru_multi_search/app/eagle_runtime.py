"""Qt scheduling for Eagle use cases; factories own backend resources."""

from dataclasses import replace
from threading import Event
import time

from PySide6.QtCore import QObject, Property, QThread, Qt, Signal, Slot

from danbooru_multi_search.app.eagle_service import (
    EagleBackend,
    EagleBackendFactory,
    EagleRequest,
    EagleResult,
    execute_eagle_request,
)
from danbooru_multi_search.core.results import ServiceAvailability
from danbooru_multi_search.tasks.model import (
    TaskAttemptResult,
    TaskFinalResult,
    TaskKind,
    TaskRecord,
)
from danbooru_multi_search.tasks.queue import (
    BoundedTaskQueue,
    TaskQueueFullError,
    TaskQueueShutdownError,
)


class _EagleWorker(QObject):
    completed = Signal(object)

    def __init__(self, factory: EagleBackendFactory, closing: Event) -> None:
        super().__init__()
        self._factory = factory
        self._closing = closing

    @Slot(object, object)
    def execute(self, request: EagleRequest, cancelled: Event) -> None:
        backend: EagleBackend | None = None
        result: EagleResult | None = None
        def stopped() -> bool:
            return cancelled.is_set() or self._closing.is_set()

        try:
            with self._factory(stopped, time.monotonic() + 15) as backend:
                result = execute_eagle_request(request, backend, stopped)
        except Exception as exc:
            started = backend.post_sent() if backend else False
            result = EagleResult(
                request.request_id,
                error_code="eagle_worker_error",
                diagnostic=str(exc),
                post_started=started,
                uncertain=started,
                created_identifier=result.created_identifier if result else None,
            )
        self.completed.emit(result)
        if self._closing.is_set():
            QThread.currentThread().quit()


class _Cancellation:
    def __init__(self, event: Event) -> None:
        self._event = event

    def abort(self) -> None:
        self._event.set()


class EagleRuntime(QObject):
    """Keep one bounded worker operation registered with the shared queue.

    Cancellation frees only this queue entry. Busy remains true until its worker
    has actually returned; a new operation cannot race the old backend.
    """
    changed = Signal()
    completed = Signal(object)
    _dispatch = Signal(object, object)

    def __init__(
        self, factory: EagleBackendFactory, availability: ServiceAvailability,
        queue: BoundedTaskQueue, parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._factory = factory
        self.availability = availability
        self._queue = queue
        self._request: EagleRequest | None = None
        self._cancel: Event | None = None
        self._task_id: str | None = None
        self._completion = None
        self._worker_busy = False
        self._thread: QThread | None = None
        self._worker: _EagleWorker | None = None
        self._closing_event = Event()
        self._closing = False
        self._closed = False
        self._stopped = True

    @Property(bool, notify=changed)
    def busy(self) -> bool:
        return self._request is not None

    @property
    def cancelling(self) -> bool:
        return self._cancel is not None and self._cancel.is_set()

    @property
    def active_request(self) -> EagleRequest | None:
        return self._request

    @property
    def thread_started(self) -> bool:
        return self._thread is not None

    def _ensure_worker(self) -> None:
        if self._thread is not None:
            return
        self._thread = QThread(self)
        self._worker = _EagleWorker(self._factory, self._closing_event)
        self._worker.moveToThread(self._thread)
        self._dispatch.connect(self._worker.execute, Qt.ConnectionType.QueuedConnection)
        self._worker.completed.connect(self._worker_done, Qt.ConnectionType.QueuedConnection)
        self._thread.finished.connect(self._worker.deleteLater)
        self._stopped = False
        self._thread.start()

    def start(self, request: EagleRequest) -> bool:
        if self._closing or not self.availability.available or self.busy:
            return False
        cancel = Event()
        self._request = request
        self._cancel = cancel
        self.changed.emit()
        if self._request is not request:
            return False
        if self._closing or cancel.is_set():
            self._request = None
            return False

        def state(record: TaskRecord) -> None:
            if self._request is not request or self._closing or cancel.is_set():
                if not record.terminal:
                    self._queue.cancel(record.identifier)
                return
            if self._task_id is None:
                self._task_id = record.identifier

        def starter(attempt: int, completion):
            if self._request is not request or self._closing or cancel.is_set():
                completion(TaskAttemptResult.failure("Cancelled.", error_code="eagle_cancelled"))
                return None
            self._ensure_worker()
            self._worker_busy = True
            self._completion = completion
            self._dispatch.emit(request, cancel)
            return _Cancellation(cancel)

        try:
            self._queue.submit(TaskKind.METADATA, starter, self._queue_done, state_listener=state)
            return self._request is request
        except (TaskQueueFullError, TaskQueueShutdownError) as exc:
            if self._request is request:
                self._publish(EagleResult(request.request_id, error_code="eagle_queue_rejected", diagnostic=str(exc)))
            return False

    def _queue_done(self, result: TaskFinalResult) -> None:
        if result.task_id != self._task_id:
            return
        self._task_id = None
        if not self._worker_busy and self._request is not None:
            value = result.value if isinstance(result.value, EagleResult) else EagleResult(
                self._request.request_id, error_code="eagle_cancelled",
            )
            self._publish(value)

    @Slot(object)
    def _worker_done(self, result: EagleResult) -> None:
        request = self._request
        if request is None or request.request_id != result.request_id:
            return
        self._worker_busy = False
        completion = self._completion
        self._completion = None
        if completion:
            # Domain errors remain values; the queue must never repeat a POST.
            completion(TaskAttemptResult.success(result))
        if self._request is request:
            self._publish(result)

    def _publish(self, result: EagleResult) -> None:
        request = self._request
        if request is None or request.request_id != result.request_id or self._closing:
            return
        if self.cancelling:
            result = replace(
                result, snapshot=None, error_code="eagle_cancelled",
                uncertain=result.uncertain or result.post_started and result.snapshot is None,
            )
        self._request = None
        self._cancel = None
        self.completed.emit(result)
        if not self._closing and self._request is None:
            self.changed.emit()

    def cancel(self) -> bool:
        request = self._request
        if request is None or self._cancel is None:
            return False
        self._cancel.set()
        if self._task_id:
            self._queue.cancel(self._task_id)
        elif not self._worker_busy:
            self._publish(EagleResult(request.request_id, error_code="eagle_cancelled"))
        if self._request is request and not self._closing:
            self.changed.emit()
        return True

    def begin_shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._closing_event.set()
        self.cancel()

    def shutdown(self) -> None:
        if self._closed:
            return
        self.begin_shutdown()
        if self._thread:
            self._thread.quit()
            if not self._thread.wait(6000):
                raise RuntimeError("Eagle worker exceeded its shutdown interval.")
        self._stopped = True
        self._request = None
        self._completion = None
        self._cancel = None
        self._worker_busy = False
        self._closed = True

    @property
    def shutdown_completed(self) -> bool:
        # A Python wrapper can outlive engine deletion; do not query dead QThread.
        return self._closed and self._stopped
