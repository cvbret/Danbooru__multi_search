"""Small injectable HTTP transport for the local Eagle Web API."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from http.client import HTTPException, IncompleteRead
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)


@dataclass(frozen=True, slots=True)
class EagleHttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None = None


@dataclass(frozen=True, slots=True)
class EagleHttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class EagleTransportError(RuntimeError):
    """Base class for failures before a complete HTTP response is available."""


class EagleConnectionError(EagleTransportError):
    pass


class EagleTimeoutError(EagleTransportError):
    pass


class EagleResponseTooLargeError(EagleTransportError):
    pass


class EagleProtocolError(EagleTransportError):
    pass


class EagleHttpTransport(Protocol):
    def send(
        self,
        request: EagleHttpRequest,
        *,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> EagleHttpResponse: ...


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibEagleHttpTransport:
    """Direct local HTTP transport with redirects and environment proxies disabled."""

    def __init__(self, opener: OpenerDirector | None = None) -> None:
        self._opener = opener or build_opener(ProxyHandler({}), _RejectRedirects())

    def send(
        self,
        request: EagleHttpRequest,
        *,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> EagleHttpResponse:
        urllib_request = Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        response = None
        try:
            try:
                response = self._opener.open(
                    urllib_request,
                    timeout=timeout_seconds,
                )
            except HTTPError as exc:
                response = exc
            return self._read_response(response, maximum_response_bytes)
        except (socket.timeout, TimeoutError) as exc:
            raise EagleTimeoutError("The Eagle request timed out.") from exc
        except URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise EagleTimeoutError("The Eagle request timed out.") from exc
            raise EagleConnectionError(
                f"Unable to connect to the Eagle Web API: {exc.reason}"
            ) from exc
        except (IncompleteRead, HTTPException) as exc:
            raise EagleProtocolError(
                f"Eagle returned an incomplete HTTP response: {exc}"
            ) from exc
        except OSError as exc:
            raise EagleConnectionError(
                f"Unable to connect to the Eagle Web API: {exc}"
            ) from exc
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def _read_response(response, maximum_response_bytes: int) -> EagleHttpResponse:
        content_length = response.headers.get("Content-Length")
        declared_length: int | None = None
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                declared_length = None
            if (
                declared_length is not None
                and declared_length > maximum_response_bytes
            ):
                raise EagleResponseTooLargeError(
                    "The Eagle response exceeded the configured size limit."
                )

        body = response.read(maximum_response_bytes + 1)
        if len(body) > maximum_response_bytes:
            raise EagleResponseTooLargeError(
                "The Eagle response exceeded the configured size limit."
            )
        if (
            declared_length is not None
            and declared_length >= 0
            and len(body) < declared_length
        ):
            raise EagleProtocolError(
                "Eagle returned an incomplete HTTP response: "
                f"expected {declared_length} bytes, received {len(body)}."
            )
        return EagleHttpResponse(
            status_code=int(response.getcode()),
            headers={key: value for key, value in response.headers.items()},
            body=body,
        )
