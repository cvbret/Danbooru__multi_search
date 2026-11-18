"""Read-only HTTP reply facts shared by JSON and streaming media requests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from PySide6.QtNetwork import QNetworkReply, QNetworkRequest


@dataclass(frozen=True, slots=True)
class ReplyDiagnostics:
    http_status: int | None
    network_error: QNetworkReply.NetworkError
    network_error_text: str
    retry_after: str | None

    @property
    def failure_kind(self) -> Literal["http", "timeout", "network"] | None:
        """Preserve HTTP failure precedence over Qt timeout/network errors."""
        if self.http_status is not None and not 200 <= self.http_status < 300:
            return "http"
        if self.network_error == QNetworkReply.NetworkError.TimeoutError:
            return "timeout"
        if self.network_error != QNetworkReply.NetworkError.NoError:
            return "network"
        return None


def read_reply_diagnostics(reply: QNetworkReply) -> ReplyDiagnostics:
    """Read only metadata, leaving response bodies and reply ownership untouched."""
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    http_status = int(status) if status is not None else None
    network_error = reply.error()
    network_error_text = reply.errorString()
    retry_after = (
        bytes(reply.rawHeader("Retry-After"))
        .decode("ascii", errors="replace").strip() or None
    )
    return ReplyDiagnostics(http_status, network_error, network_error_text, retry_after)
