"""Worker-owned synchronous Qt transport with a deadline covering headers too.

This leaf module is deliberately absent from integrations.eagle.__init__.
The CLI retains its Qt-free urllib transport.
"""

from collections.abc import Callable
import math
import time

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkProxy, QNetworkReply, QNetworkRequest

from danbooru_multi_search.integrations.eagle.transport import (
    EagleConnectionError, EagleHttpRequest, EagleHttpResponse, EagleProtocolError,
    EagleResponseTooLargeError, EagleTimeoutError,
)


class QtEagleHttpTransport:
    """Construct, use and close in the same dedicated worker thread."""

    def __init__(self, cancelled: Callable[[], bool], deadline: float) -> None:
        self._cancelled = cancelled
        self._deadline = deadline
        self._manager = QNetworkAccessManager()
        self._manager.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        self.sent_post = False

    def send(
        self, request: EagleHttpRequest, *, timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> EagleHttpResponse:
        if self._cancelled():
            raise EagleConnectionError("Eagle request cancelled before sending.")
        remaining = min(timeout_seconds, self._deadline - time.monotonic())
        if not math.isfinite(remaining) or remaining <= 0:
            raise EagleTimeoutError("Eagle operation exceeded its total time limit.")
        url = QUrl(request.url)
        if url.scheme() != 'http' or url.host().lower() not in {'localhost', '127.0.0.1', '::1'}:
            raise EagleProtocolError("Only the local Eagle HTTP API is supported.")
        if request.method not in {'GET', 'POST'}:
            raise EagleProtocolError("Unsupported Eagle HTTP method.")
        qt_request = QNetworkRequest(url)
        qt_request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
        )
        for name, value in request.headers.items():
            qt_request.setRawHeader(name.encode('ascii'), value.encode('utf-8'))
        loop = QEventLoop(self._manager)
        deadline_timer = QTimer(loop)
        poll = QTimer(loop)
        deadline_timer.setSingleShot(True)
        buffer = bytearray()
        reply = None
        settled = False
        failure = None

        def settle(error: Exception | None = None) -> None:
            nonlocal settled, failure
            if settled:
                return
            settled = True
            failure = error
            deadline_timer.stop()
            poll.stop()
            if error is not None and reply is not None and not reply.isFinished():
                reply.abort()
            loop.quit()

        def read() -> None:
            if settled:
                return
            length = bytes(reply.rawHeader('Content-Length')).strip()
            if length.isdigit() and int(length) > maximum_response_bytes:
                settle(EagleResponseTooLargeError("Eagle response exceeded its size limit."))
                return
            buffer.extend(bytes(reply.read(maximum_response_bytes + 1 - len(buffer))))
            if len(buffer) > maximum_response_bytes:
                settle(EagleResponseTooLargeError("Eagle response exceeded its size limit."))

        def finished() -> None:
            if settled:
                return
            read()
            if settled:
                return
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if (status is None or 200 <= int(status) < 300) and reply.error() != QNetworkReply.NetworkError.NoError:
                settle(EagleConnectionError(reply.errorString()))
            elif (length := bytes(reply.rawHeader('Content-Length')).strip()).isdigit() and int(length) != len(buffer):
                settle(EagleProtocolError("Eagle response ended before its declared body was complete."))
            else:
                settle()

        def cancellation() -> None:
            if self._cancelled():
                settle(EagleConnectionError("Eagle operation was cancelled."))

        deadline_timer.timeout.connect(lambda: settle(EagleTimeoutError("Eagle request exceeded its total time limit.")))
        poll.timeout.connect(cancellation)
        deadline_timer.start(max(1, math.ceil(remaining * 1000)))
        poll.start(25)
        try:
            if self._cancelled():
                raise EagleConnectionError("Eagle request cancelled before sending.")
            if time.monotonic() >= self._deadline:
                raise EagleTimeoutError("Eagle operation exceeded its total time limit.")
            if request.method == 'POST':
                self.sent_post = True
                reply = self._manager.post(qt_request, request.body or b'')
            else:
                reply = self._manager.get(qt_request)
            reply.setReadBufferSize(maximum_response_bytes + 1)
            reply.readyRead.connect(read)
            reply.metaDataChanged.connect(read)
            reply.finished.connect(finished)
            if reply.isFinished():
                finished()
            cancellation()
            # A synchronous completion/abort must not quit before entering exec.
            if not settled:
                loop.exec()
            if not settled:
                settle(EagleConnectionError("Eagle worker stopped before the request finished."))
            if failure is not None:
                raise failure
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            headers = {}
            for raw_name in reply.rawHeaderList():
                name = bytes(raw_name).decode('latin-1')
                headers[name] = bytes(reply.rawHeader(name)).decode('latin-1')
            return EagleHttpResponse(int(status or 0), headers, bytes(buffer))
        finally:
            deadline_timer.stop()
            poll.stop()
            if reply is not None:
                reply.readyRead.disconnect(read)
                reply.metaDataChanged.disconnect(read)
                reply.finished.disconnect(finished)
                if not reply.isFinished():
                    reply.abort()
                reply.deleteLater()
                QCoreApplication.sendPostedEvents(reply, QEvent.Type.DeferredDelete)
            loop.deleteLater()
            QCoreApplication.sendPostedEvents(loop, QEvent.Type.DeferredDelete)

    def close(self) -> None:
        """Dispose network resources before the worker's event loop exits."""
        self._manager.deleteLater()
        QCoreApplication.sendPostedEvents(self._manager, QEvent.Type.DeferredDelete)
