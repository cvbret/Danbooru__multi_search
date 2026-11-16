"""Explicit pool metadata requests using the application's shared task queue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from PySide6.QtCore import QObject, Property, QTimer, QUrl, QUrlQuery, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from danbooru_multi_search.core.pools import (
    DEFAULT_POOL_LIST_LIMIT,
    DanbooruPool,
    PoolError,
    PoolMembershipPage,
    PoolRequestIdentity,
    PoolRequestKind,
    PoolRequestOutcome,
    PoolRequestState,
    pool_identifier,
    pool_pagination,
)
from danbooru_multi_search.danbooru.client import DEFAULT_TIMEOUT_MS, USER_AGENT
from danbooru_multi_search.danbooru.pool_parsing import (
    MAX_POOL_RESPONSE_BYTES,
    PoolParseError,
    parse_pool_list_response,
    parse_pool_response,
)
from danbooru_multi_search.tasks.model import (
    TaskAttemptResult,
    TaskFinalResult,
    TaskKind,
    TaskRecord,
    TaskState,
)
from danbooru_multi_search.tasks.queue import (
    BoundedTaskQueue,
    TaskQueueFullError,
    TaskQueueShutdownError,
)

PoolValue = DanbooruPool | PoolMembershipPage
PoolResponseParser = Callable[[bytes], PoolValue]


def _request(base_url: str, path: str, timeout_ms: int) -> QNetworkRequest:
    if type(timeout_ms) is not int or timeout_ms <= 0:
        raise ValueError("timeout_ms must be a positive integer")
    endpoint = QUrl(base_url)
    if (
        not endpoint.isValid()
        or endpoint.scheme() not in {"http", "https"}
        or not endpoint.host()
    ):
        raise ValueError("Danbooru base URL must be a valid HTTP or HTTPS URL.")
    endpoint.setPath(endpoint.path().rstrip("/") + path)
    endpoint.setQuery(None)
    endpoint.setFragment("")
    request = QNetworkRequest(endpoint)
    request.setRawHeader(b"User-Agent", USER_AGENT.encode("ascii"))
    request.setRawHeader(b"Accept", b"application/json")
    request.setAttribute(QNetworkRequest.Attribute.Http2AllowedAttribute, False)
    request.setTransferTimeout(timeout_ms)
    return request


def build_pool_request(
    base_url: str, pool_id: int, *, timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> QNetworkRequest:
    """Build one single-pool metadata request; no post/media requests follow."""
    pool_identifier(pool_id)
    return _request(base_url, f"/pools/{pool_id}.json", timeout_ms)


def build_pool_memberships_request(
    base_url: str,
    post_id: int,
    *,
    page: int = 1,
    limit: int = DEFAULT_POOL_LIST_LIMIT,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> QNetworkRequest:
    """Request one membership page with the official post_ids_include_any filter."""
    pool_identifier(post_id, "作品")
    pool_pagination(page, limit)
    request = _request(base_url, "/pools.json", timeout_ms)
    query = QUrlQuery()
    query.addQueryItem("search[post_ids_include_any]", str(post_id))
    query.addQueryItem("search[is_deleted]", "false")
    query.addQueryItem("page", str(page))
    query.addQueryItem("limit", str(limit))
    endpoint = request.url()
    endpoint.setQuery(query)
    request.setUrl(endpoint)
    return request


class _QtPoolRequestAttempt(QObject):
    """Own only this reply and enforce a bounded UTF-8 JSON response."""

    def __init__(
        self,
        manager: QNetworkAccessManager,
        request: QNetworkRequest,
        completion: Callable[[TaskAttemptResult], None],
        parent: QObject,
        *,
        parser: PoolResponseParser,
        maximum_bytes: int,
    ) -> None:
        super().__init__(parent)
        self._completion: Callable[[TaskAttemptResult], None] | None = completion
        self._parser = parser
        self._maximum_bytes = maximum_bytes
        self._payload = bytearray()
        self._reply: QNetworkReply | None = manager.get(request)
        self._reply.setReadBufferSize(maximum_bytes + 1)
        self._reply.readyRead.connect(self._read)
        self._reply.finished.connect(self._finished)
        if self._reply.isFinished():
            QTimer.singleShot(0, self, self._finished)

    def abort(self) -> None:
        self._completion = None
        reply, self._reply = self._reply, None
        if reply is not None:
            reply.abort()
            reply.deleteLater()
        self.deleteLater()

    def _read(self) -> None:
        reply = self._reply
        if reply is None:
            return
        # read() also bounds the allocation when a server sends an oversized body.
        remaining = self._maximum_bytes + 1 - len(self._payload)
        self._payload.extend(bytes(reply.read(remaining)))
        if len(self._payload) > self._maximum_bytes:
            self._reply = None
            reply.abort()
            reply.deleteLater()
            self._complete(TaskAttemptResult.failure(
                "作品池响应超过允许大小。", error_code="response_too_large",
            ))

    def _finished(self) -> None:
        self._read()
        reply, self._reply = self._reply, None
        if reply is None:
            return
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        status = int(status) if status is not None else None
        error, error_text = reply.error(), reply.errorString()
        retry_after = (
            bytes(reply.rawHeader("Retry-After"))
            .decode("ascii", errors="replace").strip() or None
        )
        reply.deleteLater()
        if status is not None and not 200 <= status < 300:
            result = TaskAttemptResult.failure(
                f"作品池请求返回 HTTP {status}。",
                error_code="http", http_status=status, retry_after=retry_after,
            )
        elif error != QNetworkReply.NetworkError.NoError:
            code = (
                "timeout" if error == QNetworkReply.NetworkError.TimeoutError
                else "network"
            )
            result = TaskAttemptResult.failure(
                f"作品池请求失败：{error_text}", error_code=code, http_status=status,
            )
        else:
            try:
                value = self._parser(bytes(self._payload))
            except PoolError as exc:
                result = TaskAttemptResult.failure(
                    str(exc), error_code="invalid_response", http_status=status,
                )
            else:
                result = TaskAttemptResult.success(value, http_status=status)
        self._complete(result)

    def _complete(self, result: TaskAttemptResult) -> None:
        completion, self._completion = self._completion, None
        if completion is not None:
            completion(result)
        self.deleteLater()


@dataclass(slots=True)
class _PoolOperation:
    identity: PoolRequestIdentity
    task_id: str | None = None
    state: PoolRequestState = PoolRequestState.WAITING
    accepted: bool = False
    terminal: bool = False


class PoolRequestService(QObject):
    """One replaceable pool operation; cancellation never shuts down its shared queue.

    requestAccepted is emitted after queue registration and before any HTTP starter.
    Signal handlers may synchronously cancel, replace or close the operation.
    Immutable request IDs, not pool IDs, distinguish repeated reads of one pool.
    """

    stateChanged = Signal()
    requestAccepted = Signal(object)
    requestCompleted = Signal(object)

    def __init__(
        self,
        base_url: str,
        *,
        task_queue: BoundedTaskQueue,
        network_manager: QNetworkAccessManager | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        maximum_response_bytes: int = MAX_POOL_RESPONSE_BYTES,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        _request(base_url, "/pools.json", timeout_ms)
        if (
            type(maximum_response_bytes) is not int
            or not 1 <= maximum_response_bytes <= MAX_POOL_RESPONSE_BYTES
        ):
            raise ValueError(
                "maximum_response_bytes is outside the bounded response range"
            )
        self._base_url = base_url
        self._timeout_ms = timeout_ms
        self._maximum_bytes = maximum_response_bytes
        self._queue = task_queue
        self._manager = (
            network_manager if network_manager is not None
            else QNetworkAccessManager(self)
        )
        self._operation: _PoolOperation | None = None
        self._closed = False
        self._last_outcome: PoolRequestOutcome | None = None
        self._last_success: PoolRequestOutcome | None = None

    @Property(str, notify=stateChanged)
    def state(self) -> str:
        return (
            self._operation.state.value if self._operation
            else PoolRequestState.IDLE.value
        )

    @Property(bool, notify=stateChanged)
    def busy(self) -> bool:
        return (
            self._operation is not None
            and not self._operation.terminal
            and not self._closed
        )

    @Property(str, notify=stateChanged)
    def currentRequestId(self) -> str:
        return self._operation.identity.request_id if self._operation else ""

    @Property(str, notify=stateChanged)
    def errorText(self) -> str:
        outcome = self._last_outcome
        return (
            outcome.message
            if outcome and outcome.state is PoolRequestState.FAILED else ""
        )

    @property
    def current_request(self) -> PoolRequestIdentity | None:
        return self._operation.identity if self._operation else None

    @property
    def last_outcome(self) -> PoolRequestOutcome | None:
        return self._last_outcome

    @property
    def last_success(self) -> PoolRequestOutcome | None:
        """Retain the last confirmed data when a later read fails or is cancelled."""
        return self._last_success

    def fetch_pool(self, pool_id: int) -> PoolRequestIdentity:
        """Replace the current operation with one explicitly requested pool."""
        request = build_pool_request(
            self._base_url, pool_id, timeout_ms=self._timeout_ms,
        )
        identity = PoolRequestIdentity(uuid4().hex, PoolRequestKind.POOL, pool_id)

        def parse(payload: bytes) -> DanbooruPool:
            pool = parse_pool_response(payload)
            if pool.id != pool_id:
                raise PoolParseError("作品池响应编号与请求不一致。")
            return pool

        self._start(identity, request, parse)
        return identity

    def fetch_memberships(
        self, post_id: int, *, page: int = 1,
        limit: int = DEFAULT_POOL_LIST_LIMIT,
    ) -> PoolRequestIdentity:
        """Replace the current operation with one bounded membership page."""
        request = build_pool_memberships_request(
            self._base_url, post_id, page=page, limit=limit,
            timeout_ms=self._timeout_ms,
        )
        identity = PoolRequestIdentity(
            uuid4().hex, PoolRequestKind.MEMBERSHIPS, post_id, page, limit,
        )

        def parse(payload: bytes) -> PoolMembershipPage:
            return PoolMembershipPage(
                post_id, page, limit, parse_pool_list_response(payload, limit=limit),
            )

        self._start(identity, request, parse)
        return identity

    def _current(self, operation: _PoolOperation) -> bool:
        return (
            not self._closed
            and self._operation is operation
            and not operation.terminal
        )

    def _start(
        self, identity: PoolRequestIdentity, request: QNetworkRequest,
        parser: PoolResponseParser,
    ) -> None:
        if self._closed:
            raise RuntimeError("作品池请求服务已经关闭。")
        previous = self._operation
        operation = _PoolOperation(identity)
        self._operation = operation
        self._last_outcome = None
        if previous and previous.task_id and not previous.terminal:
            previous.terminal = True
            self._queue.cancel(previous.task_id, "作品池请求已被替换。")
        if not self._current(operation):
            return
        self.stateChanged.emit()
        if not self._current(operation):
            return

        # A scheduler may dispatch/complete synchronously inside submit().
        submitted = False
        deferred: list[TaskFinalResult] = []

        def starter(
            attempt: int, completion: Callable[[TaskAttemptResult], None],
        ) -> _QtPoolRequestAttempt | None:
            if not self._current(operation):
                return None
            return _QtPoolRequestAttempt(
                self._manager, request, completion, self,
                parser=parser, maximum_bytes=self._maximum_bytes,
            )

        def state_changed(record: TaskRecord) -> None:
            operation.task_id = record.identifier
            if not self._current(operation):
                if not record.terminal:
                    self._queue.cancel(record.identifier, "作品池请求已经失效。")
                return
            if not operation.accepted:
                operation.accepted = True
                self.requestAccepted.emit(identity)
                if not self._current(operation):
                    if not record.terminal:
                        self._queue.cancel(record.identifier, "作品池请求已经失效。")
                    return
            state = {
                TaskState.WAITING: PoolRequestState.WAITING,
                TaskState.RUNNING: PoolRequestState.RUNNING,
                TaskState.RETRY_WAIT: PoolRequestState.RETRY_WAIT,
            }.get(record.state)
            if state is not None and state is not operation.state:
                operation.state = state
                self.stateChanged.emit()

        def completed(result: TaskFinalResult) -> None:
            if not submitted:
                deferred.append(result)
            else:
                self._completed(operation, result)

        try:
            record = self._queue.submit(
                TaskKind.METADATA, starter, completed, state_listener=state_changed,
            )
        except (TaskQueueFullError, TaskQueueShutdownError) as exc:
            self._finish(operation, PoolRequestOutcome(
                identity, PoolRequestState.FAILED,
                message=f"作品池请求无法加入队列：{exc}", error_code="queue_rejected",
            ))
            return
        operation.task_id = record.identifier
        submitted = True
        for result in deferred:
            self._completed(operation, result)

    def _completed(self, operation: _PoolOperation, result: TaskFinalResult) -> None:
        if not self._current(operation) or result.task_id != operation.task_id:
            return
        if result.state is TaskState.SUCCEEDED:
            try:
                is_pool = operation.identity.kind is PoolRequestKind.POOL
                outcome = PoolRequestOutcome(
                    operation.identity, PoolRequestState.SUCCESS,
                    pool=result.value if is_pool else None,
                    memberships=None if is_pool else result.value,
                    http_status=result.http_status,
                )
            except PoolError as exc:
                outcome = PoolRequestOutcome(
                    operation.identity, PoolRequestState.FAILED, message=str(exc),
                    error_code="invalid_response", http_status=result.http_status,
                )
        else:
            state = (
                PoolRequestState.CANCELLED if result.state is TaskState.CANCELLED
                else PoolRequestState.FAILED
            )
            outcome = PoolRequestOutcome(
                operation.identity, state, message=result.error or "作品池请求失败。",
                error_code=result.error_code or "request_failed",
                http_status=result.http_status,
            )
        self._finish(operation, outcome)

    def _finish(self, operation: _PoolOperation, outcome: PoolRequestOutcome) -> None:
        if not self._current(operation):
            return
        operation.terminal = True
        operation.state = outcome.state
        self._last_outcome = outcome
        if outcome.succeeded:
            self._last_success = outcome
        self.stateChanged.emit()
        if not self._closed and self._operation is operation:
            self.requestCompleted.emit(outcome)

    def cancel_current(self, expected_request_id: str | None = None) -> bool:
        """Cancel only the current matching operation; stale clicks are ignored."""
        operation = self._operation
        if (
            operation is None or not self._current(operation)
            or (expected_request_id is not None
                and expected_request_id != operation.identity.request_id)
        ):
            return False
        task_id = operation.task_id
        # Invalidate before cancelling: queue listeners/completion are synchronous.
        operation.terminal = True
        operation.state = PoolRequestState.CANCELLED
        outcome = PoolRequestOutcome(
            operation.identity, PoolRequestState.CANCELLED,
            message="作品池请求已取消。", error_code="cancelled",
        )
        self._last_outcome = outcome
        if task_id is not None:
            self._queue.cancel(task_id, outcome.message)
        if not self._closed and self._operation is operation:
            self.stateChanged.emit()
            if not self._closed and self._operation is operation:
                self.requestCompleted.emit(outcome)
        return True

    def shutdown(self) -> None:
        """Reject future operations and suppress late signals; keep other work alive."""
        if self._closed:
            return
        self._closed = True
        operation = self._operation
        if operation is not None and not operation.terminal:
            operation.terminal = True
            operation.state = PoolRequestState.CANCELLED
            if operation.task_id is not None:
                self._queue.cancel(operation.task_id, "应用正在关闭作品池请求。")
