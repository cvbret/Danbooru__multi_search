"""Queued QtNetwork controller for one minimal Danbooru request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QObject, Property, Signal, Slot, QUrl, QUrlQuery
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
    QSslSocket,
)

from danbooru_multi_search.danbooru.http_diagnostics import read_reply_diagnostics
from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.danbooru.parsing import (
    DanbooruParseError,
    ParsedPostsPage,
    parse_search_posts_response,
)
from danbooru_multi_search.tasks import (
    BoundedTaskQueue,
    QtScheduler,
    SystemClock,
    TaskAttemptResult,
    TaskFinalResult,
    TaskKind,
    TaskQueueConfig,
    TaskQueueFullError,
    TaskQueueShutdownError,
    TaskRecord,
    TaskState,
)


USER_AGENT = (
    "DanbooruMultiSearch/0.1.0 "
    "(https://github.com/cvbret/Danbooru__multi_search)"
)
DEFAULT_TIMEOUT_MS = 10_000
PostsResponseParser = Callable[[bytes], tuple[DanbooruPost, ...] | ParsedPostsPage]


class RequestState:
    IDLE = "idle"
    WAITING = "waiting"
    LOADING = "loading"
    RETRY_WAIT = "retry_wait"
    SUCCESS = "success"
    EMPTY = "empty"
    ERROR = "error"
    CANCELLED = "cancelled"


class RequestErrorKind:
    INVALID_REQUEST = "invalid_request"
    HTTP = "http"
    TIMEOUT = "timeout"
    NETWORK = "network"
    INVALID_RESPONSE = "invalid_response"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class DanbooruRequestOutcome:
    state: str
    message: str
    post: DanbooruPost | None = None
    error_kind: str | None = None
    http_status: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.state == RequestState.SUCCESS


def select_preferred_tls_backend() -> None:
    """Use Schannel when Qt provides it, avoiding external OpenSSL DLL coupling."""

    available_backends = QSslSocket.availableBackends()
    if "schannel" in available_backends and QSslSocket.activeBackend() != "schannel":
        QSslSocket.setActiveBackend("schannel")


def build_posts_request(
    base_url: str,
    tags: str,
    *,
    page: int = 1,
    limit: int = 1,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> QNetworkRequest:
    """Build one validated ``posts.json`` page request."""

    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be greater than zero")
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ValueError("page must be a positive integer")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= 200
    ):
        raise ValueError("limit must be between 1 and 200")

    endpoint = QUrl(f"{base_url.rstrip('/')}/posts.json")
    if (
        not endpoint.isValid()
        or endpoint.scheme() not in {"http", "https"}
        or not endpoint.host()
    ):
        raise ValueError("Danbooru base URL must be a valid HTTP or HTTPS URL.")

    query = QUrlQuery()
    query.addQueryItem("tags", tags)
    query.addQueryItem("page", str(page))
    query.addQueryItem("limit", str(limit))
    endpoint.setQuery(query)

    request = QNetworkRequest(endpoint)
    request.setRawHeader(b"User-Agent", USER_AGENT.encode("ascii"))
    request.setRawHeader(b"Accept", b"application/json")
    request.setAttribute(QNetworkRequest.Attribute.Http2AllowedAttribute, False)
    request.setTransferTimeout(timeout_ms)
    return request


class QtPostsRequestAttempt(QObject):
    """Own one QNetworkReply and convert it to a queue attempt result."""

    def __init__(
        self,
        manager: QNetworkAccessManager,
        request: QNetworkRequest,
        completion: Callable[[TaskAttemptResult], None],
        parent: QObject,
        *,
        parser: PostsResponseParser = parse_search_posts_response,
    ) -> None:
        super().__init__(parent)
        self._completion: Callable[[TaskAttemptResult], None] | None = completion
        self._parser = parser
        self._reply: QNetworkReply | None = manager.get(request)
        self._reply.finished.connect(self._finished)

    def abort(self) -> None:
        self._completion = None
        if self._reply is not None:
            self._reply.abort()

    @Slot()
    def _finished(self) -> None:
        reply = self._reply
        if reply is None:
            return
        self._reply = None

        payload = bytes(reply.readAll())
        diagnostics = read_reply_diagnostics(reply)
        http_status = diagnostics.http_status
        reply.deleteLater()

        if diagnostics.failure_kind == "http":
            result = TaskAttemptResult.failure(
                f"Danbooru 返回 HTTP {http_status}。",
                error_code=RequestErrorKind.HTTP,
                http_status=http_status,
                retry_after=diagnostics.retry_after,
            )
        elif diagnostics.failure_kind is not None:
            if diagnostics.failure_kind == "timeout":
                error_code = RequestErrorKind.TIMEOUT
                message = "Danbooru 请求超时。"
            else:
                error_code = RequestErrorKind.NETWORK
                message = f"Danbooru 网络错误：{diagnostics.network_error_text}"
            result = TaskAttemptResult.failure(
                message,
                error_code=error_code,
                http_status=http_status,
            )
        else:
            try:
                posts = self._parser(payload)
            except DanbooruParseError as exc:
                result = TaskAttemptResult.failure(
                    str(exc),
                    error_code=RequestErrorKind.INVALID_RESPONSE,
                    http_status=http_status,
                )
            else:
                result = TaskAttemptResult.success(
                    posts,
                    http_status=http_status,
                )

        completion = self._completion
        self._completion = None
        if completion is not None:
            completion(result)
        self.deleteLater()


class DanbooruSearchController(QObject):
    """Expose one cancellable, rate-limited Danbooru result to QML."""

    stateChanged = Signal()
    statusTextChanged = Signal()
    errorTextChanged = Signal()
    imageUrlChanged = Signal()
    postSummaryChanged = Signal()
    requestCompleted = Signal(object)

    def __init__(
        self,
        base_url: str,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        network_manager: QNetworkAccessManager | None = None,
        task_queue: BoundedTaskQueue | None = None,
        queue_config: TaskQueueConfig | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        select_preferred_tls_backend()
        self._base_url = base_url
        self._timeout_ms = timeout_ms
        self._network_manager = network_manager or QNetworkAccessManager(self)
        self._owns_queue = task_queue is None
        if task_queue is None:
            scheduler = QtScheduler(self)
            task_queue = BoundedTaskQueue(
                queue_config or TaskQueueConfig(),
                clock=SystemClock(),
                scheduler=scheduler,
            )
        self._task_queue = task_queue
        self._active_task_id: str | None = None
        self._request_generation = 0
        self._shutdown = False
        self._state = RequestState.IDLE
        self._status_text = "请输入标签并请求一条作品。"
        self._error_text = ""
        self._image_url = ""
        self._post_summary = ""

    @Property(str, notify=stateChanged)
    def state(self) -> str:
        return self._state

    @Property(bool, notify=stateChanged)
    def busy(self) -> bool:
        return self._state in {
            RequestState.WAITING,
            RequestState.LOADING,
            RequestState.RETRY_WAIT,
        }

    @Property(str, notify=statusTextChanged)
    def statusText(self) -> str:
        return self._status_text

    @Property(str, notify=errorTextChanged)
    def errorText(self) -> str:
        return self._error_text

    @Property(str, notify=imageUrlChanged)
    def imageUrl(self) -> str:
        return self._image_url

    @Property(str, notify=postSummaryChanged)
    def postSummary(self) -> str:
        return self._post_summary

    @Slot(str)
    def search(self, tags: str) -> None:
        if self.busy:
            return
        if self._shutdown:
            self._complete(
                DanbooruRequestOutcome(
                    state=RequestState.ERROR,
                    message="应用正在关闭，不能开始新请求。",
                    error_kind=RequestErrorKind.INVALID_REQUEST,
                )
            )
            return

        normalized_tags = tags.strip()
        if not normalized_tags:
            self._complete(
                DanbooruRequestOutcome(
                    state=RequestState.ERROR,
                    message="请输入至少一个标签。",
                    error_kind=RequestErrorKind.INVALID_REQUEST,
                )
            )
            return

        try:
            request = build_posts_request(
                self._base_url,
                normalized_tags,
                timeout_ms=self._timeout_ms,
            )
        except ValueError as exc:
            self._complete(
                DanbooruRequestOutcome(
                    state=RequestState.ERROR,
                    message=str(exc),
                    error_kind=RequestErrorKind.INVALID_REQUEST,
                )
            )
            return

        self._request_generation += 1
        generation = self._request_generation
        self._state = RequestState.WAITING
        self._status_text = "请求正在等待执行。"
        self._error_text = ""
        self._image_url = ""
        self._post_summary = ""
        self._emit_view_changes()

        def starter(
            attempt_number: int,
            completion: Callable[[TaskAttemptResult], None],
        ) -> QtPostsRequestAttempt:
            return QtPostsRequestAttempt(
                self._network_manager,
                request,
                completion,
                self,
            )

        def state_changed(record: TaskRecord) -> None:
            self._task_state_changed(generation, record)

        def completed(result: TaskFinalResult) -> None:
            self._task_completed(generation, result)

        try:
            record = self._task_queue.submit(
                TaskKind.METADATA,
                starter,
                completed,
                state_listener=state_changed,
            )
        except (TaskQueueFullError, TaskQueueShutdownError) as exc:
            self._complete(
                DanbooruRequestOutcome(
                    state=RequestState.ERROR,
                    message=str(exc),
                    error_kind=RequestErrorKind.INVALID_REQUEST,
                )
            )
            return
        if self.busy and generation == self._request_generation:
            self._active_task_id = record.identifier

    @Slot()
    def cancelCurrentRequest(self) -> None:
        task_id = self._active_task_id
        if task_id is not None:
            self._task_queue.cancel(task_id, "用户取消了请求。")

    @Slot()
    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        if self._owns_queue:
            self._task_queue.shutdown()
        elif self._active_task_id is not None:
            self._task_queue.cancel(
                self._active_task_id,
                "Application is shutting down.",
            )

    def _task_state_changed(self, generation: int, record: TaskRecord) -> None:
        if generation != self._request_generation or record.terminal:
            return
        if record.state == TaskState.WAITING:
            self._state = RequestState.WAITING
            self._status_text = "请求正在等待执行。"
        elif record.state == TaskState.RUNNING:
            self._state = RequestState.LOADING
            self._status_text = f"正在请求 Danbooru（第 {record.attempts} 次）……"
        elif record.state == TaskState.RETRY_WAIT:
            self._state = RequestState.RETRY_WAIT
            self._status_text = "Danbooru 要求稍后重试，当前正在等待。"
        self._error_text = ""
        self._emit_view_changes()

    def _task_completed(self, generation: int, result: TaskFinalResult) -> None:
        if generation != self._request_generation:
            return
        if (
            self._active_task_id is not None
            and result.task_id != self._active_task_id
        ):
            return
        self._active_task_id = None

        if result.state == TaskState.CANCELLED:
            self._complete(
                DanbooruRequestOutcome(
                    state=RequestState.CANCELLED,
                    message="请求已取消。",
                    error_kind=RequestErrorKind.CANCELLED,
                )
            )
            return

        if result.state == TaskState.FAILED:
            message = result.error or "Danbooru 请求失败。"
            if result.retry_exhausted and result.http_status is not None:
                message = (
                    f"Danbooru 返回 HTTP {result.http_status}，"
                    f"重试 {max(0, result.attempts - 1)} 次后仍然失败。"
                )
            self._complete(
                DanbooruRequestOutcome(
                    state=RequestState.ERROR,
                    message=message,
                    error_kind=result.error_code,
                    http_status=result.http_status,
                )
            )
            return

        posts = result.value
        if not isinstance(posts, tuple):
            self._complete(
                DanbooruRequestOutcome(
                    state=RequestState.ERROR,
                    message="Danbooru 请求返回了未知结果。",
                    error_kind=RequestErrorKind.INVALID_RESPONSE,
                    http_status=result.http_status,
                )
            )
            return

        if not posts:
            self._complete(
                DanbooruRequestOutcome(
                    state=RequestState.EMPTY,
                    message="没有找到符合标签的作品。",
                    http_status=result.http_status,
                )
            )
            return

        post = posts[0]
        self._complete(
            DanbooruRequestOutcome(
                state=RequestState.SUCCESS,
                message=f"已加载作品 #{post.id}。",
                post=post,
                http_status=result.http_status,
            )
        )

    def _complete(self, outcome: DanbooruRequestOutcome) -> None:
        self._state = outcome.state
        self._status_text = outcome.message
        self._error_text = outcome.message if outcome.state == RequestState.ERROR else ""

        if outcome.post is None:
            self._image_url = ""
            self._post_summary = ""
        else:
            post = outcome.post
            score_text = str(post.score) if post.score is not None else "暂无数据"
            favorite_text = (
                str(post.fav_count)
                if post.fav_count is not None
                else "暂无数据"
            )
            self._image_url = post.display_url
            self._post_summary = (
                f"作品 #{post.id} · 得分 {score_text} · "
                f"收藏 {favorite_text} · 分级 {post.rating} · "
                f"{post.width}×{post.height} · {post.file_ext}"
            )

        self._emit_view_changes()
        self.requestCompleted.emit(outcome)

    def _emit_view_changes(self) -> None:
        self.stateChanged.emit()
        self.statusTextChanged.emit()
        self.errorTextChanged.emit()
        self.imageUrlChanged.emit()
        self.postSummaryChanged.emit()
