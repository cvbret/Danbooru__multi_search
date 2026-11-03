"""Asynchronous QtNetwork controller for one minimal Danbooru request."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Property, Signal, Slot, QUrl, QUrlQuery
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
    QSslSocket,
)

from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.danbooru.parsing import (
    DanbooruParseError,
    parse_posts_response,
)


USER_AGENT = (
    "DanbooruMultiSearch/0.1.0 "
    "(https://github.com/cvbret/Danbooru__multi_search)"
)
DEFAULT_TIMEOUT_MS = 10_000


class RequestState:
    IDLE = "idle"
    LOADING = "loading"
    SUCCESS = "success"
    EMPTY = "empty"
    ERROR = "error"


class RequestErrorKind:
    INVALID_REQUEST = "invalid_request"
    HTTP = "http"
    TIMEOUT = "timeout"
    NETWORK = "network"
    INVALID_RESPONSE = "invalid_response"


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


def _select_preferred_tls_backend() -> None:
    """Use Schannel when Qt provides it, avoiding external OpenSSL DLL coupling."""

    available_backends = QSslSocket.availableBackends()
    if "schannel" in available_backends and QSslSocket.activeBackend() != "schannel":
        QSslSocket.setActiveBackend("schannel")


def build_posts_request(
    base_url: str,
    tags: str,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> QNetworkRequest:
    """Build the single-page, single-post request used by M0-002."""

    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be greater than zero")

    endpoint = QUrl(f"{base_url.rstrip('/')}/posts.json")
    if (
        not endpoint.isValid()
        or endpoint.scheme() not in {"http", "https"}
        or not endpoint.host()
    ):
        raise ValueError("Danbooru base URL must be a valid HTTP or HTTPS URL.")

    query = QUrlQuery()
    query.addQueryItem("tags", tags)
    query.addQueryItem("page", "1")
    query.addQueryItem("limit", "1")
    endpoint.setQuery(query)

    request = QNetworkRequest(endpoint)
    request.setRawHeader(b"User-Agent", USER_AGENT.encode("ascii"))
    request.setRawHeader(b"Accept", b"application/json")
    request.setAttribute(QNetworkRequest.Attribute.Http2AllowedAttribute, False)
    request.setTransferTimeout(timeout_ms)
    return request


class DanbooruSearchController(QObject):
    """Expose one asynchronous Danbooru result to QML."""

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
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        _select_preferred_tls_backend()
        self._base_url = base_url
        self._timeout_ms = timeout_ms
        self._network_manager = network_manager or QNetworkAccessManager(self)
        self._active_reply: QNetworkReply | None = None
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
        return self._state == RequestState.LOADING

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
        """Start one request and return immediately to the Qt event loop."""

        if self.busy:
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

        self._state = RequestState.LOADING
        self._status_text = "正在请求 Danbooru……"
        self._error_text = ""
        self._image_url = ""
        self._post_summary = ""
        self._emit_view_changes()

        reply = self._network_manager.get(request)
        self._active_reply = reply
        reply.finished.connect(self._handle_reply)

    def _handle_reply(self) -> None:
        reply = self._active_reply
        if reply is None:
            return

        self._active_reply = None
        payload = bytes(reply.readAll())
        network_error = reply.error()
        network_error_text = reply.errorString()
        status_value = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute
        )
        http_status = int(status_value) if status_value is not None else None
        reply.deleteLater()

        if http_status is not None and not 200 <= http_status < 300:
            self._complete(
                DanbooruRequestOutcome(
                    state=RequestState.ERROR,
                    message=f"Danbooru 返回 HTTP {http_status}。",
                    error_kind=RequestErrorKind.HTTP,
                    http_status=http_status,
                )
            )
            return

        if network_error != QNetworkReply.NetworkError.NoError:
            if network_error == QNetworkReply.NetworkError.TimeoutError:
                kind = RequestErrorKind.TIMEOUT
                message = f"Danbooru 请求在 {self._timeout_ms} 毫秒后超时。"
            else:
                kind = RequestErrorKind.NETWORK
                message = f"Danbooru 网络错误：{network_error_text}"
            self._complete(
                DanbooruRequestOutcome(
                    state=RequestState.ERROR,
                    message=message,
                    error_kind=kind,
                    http_status=http_status,
                )
            )
            return

        try:
            posts = parse_posts_response(payload)
        except DanbooruParseError as exc:
            self._complete(
                DanbooruRequestOutcome(
                    state=RequestState.ERROR,
                    message=str(exc),
                    error_kind=RequestErrorKind.INVALID_RESPONSE,
                    http_status=http_status,
                )
            )
            return

        if not posts:
            self._complete(
                DanbooruRequestOutcome(
                    state=RequestState.EMPTY,
                    message="没有找到符合标签的作品。",
                    http_status=http_status,
                )
            )
            return

        post = posts[0]
        self._complete(
            DanbooruRequestOutcome(
                state=RequestState.SUCCESS,
                message=f"已加载作品 #{post.id}。",
                post=post,
                http_status=http_status,
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
            self._image_url = post.display_url
            self._post_summary = (
                f"作品 #{post.id} · 得分 {post.score} · "
                f"收藏 {post.fav_count} · 分级 {post.rating} · "
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
