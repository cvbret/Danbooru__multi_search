from __future__ import annotations

import threading
import time
import socket
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

import pytest


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    path: str
    headers: dict[str, str]


class LocalHttpServer:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        delay: float = 0,
        disconnect: bool = False,
        include_content_length: bool = True,
    ) -> None:
        class TestServer(ThreadingHTTPServer):
            daemon_threads = True

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                server = self.server
                server.recorded_requests.append(  # type: ignore[attr-defined]
                    RecordedRequest(
                        path=self.path,
                        headers={key: value for key, value in self.headers.items()},
                    )
                )
                if server.response_delay:  # type: ignore[attr-defined]
                    time.sleep(server.response_delay)  # type: ignore[attr-defined]
                if server.disconnect_without_response:  # type: ignore[attr-defined]
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                try:
                    self.send_response(server.response_status)  # type: ignore[attr-defined]
                    self.send_header("Content-Type", "application/json")
                    if server.include_content_length:  # type: ignore[attr-defined]
                        self.send_header(
                            "Content-Length",
                            str(len(server.response_body)),  # type: ignore[attr-defined]
                        )
                    self.end_headers()
                    self.wfile.write(server.response_body)  # type: ignore[attr-defined]
                except OSError:
                    pass

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = TestServer(("127.0.0.1", 0), Handler)
        self._server.response_body = body
        self._server.response_status = status
        self._server.response_delay = delay
        self._server.disconnect_without_response = disconnect
        self._server.include_content_length = include_content_length
        self._server.recorded_requests = []
        host, port = self._server.server_address
        self.base_url = f"http://{host}:{port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()

    @property
    def requests(self) -> list[RecordedRequest]:
        return self._server.recorded_requests

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


@pytest.fixture
def local_http_server() -> Callable[..., LocalHttpServer]:
    servers: list[LocalHttpServer] = []

    def create(
        body: bytes,
        *,
        status: int = 200,
        delay: float = 0,
        disconnect: bool = False,
        include_content_length: bool = True,
    ) -> LocalHttpServer:
        server = LocalHttpServer(
            body,
            status=status,
            delay=delay,
            disconnect=disconnect,
            include_content_length=include_content_length,
        )
        servers.append(server)
        return server

    yield create

    for server in servers:
        server.close()
