from __future__ import annotations

import json
import socket
import threading
from collections import deque
from dataclasses import dataclass, field
from http.client import HTTPException, IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest

from danbooru_multi_search.integrations.eagle import (
    EagleConnectionError,
    EagleHttpRequest,
    EagleHttpResponse,
    EagleProtocolError,
    EagleResponseTooLargeError,
    EagleTimeoutError,
    EagleWebApiBridge,
    UrllibEagleHttpTransport,
    library_identifier_from_windows_path,
    normalize_windows_library_path,
)


def jsend(data: object) -> bytes:
    return json.dumps({"status": "success", "data": data}).encode("utf-8")


def app_info(
    version: str = "4.0.0",
    build_version: str | None = "build21",
) -> dict[str, object]:
    return {
        "version": version,
        "prereleaseVersion": None,
        "buildVersion": build_version,
        "platform": "win32",
    }


def library_info(path: str = r"D:\Eagle\Illustrations.library") -> dict[str, object]:
    return {"name": "Illustrations", "path": path}


@dataclass(frozen=True, slots=True)
class ServerResponse:
    body: bytes
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    include_content_length: bool = True


@dataclass(frozen=True, slots=True)
class RecordedHttpRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class EagleTestServer:
    def __init__(self, responses: list[ServerResponse]) -> None:
        queued_responses = deque(responses)

        class TestServer(ThreadingHTTPServer):
            daemon_threads = True

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self._handle()

            def do_POST(self) -> None:
                self._handle()

            def _handle(self) -> None:
                content_length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(content_length) if content_length else b""
                self.server.recorded_requests.append(  # type: ignore[attr-defined]
                    RecordedHttpRequest(
                        method=self.command,
                        path=self.path,
                        headers={key.lower(): value for key, value in self.headers.items()},
                        body=body,
                    )
                )
                try:
                    response = self.server.queued_responses.popleft()  # type: ignore[attr-defined]
                except IndexError:
                    response = ServerResponse(b"no queued response", status=500)
                self.send_response(response.status)
                for key, value in response.headers.items():
                    self.send_header(key, value)
                if response.include_content_length:
                    self.send_header("Content-Length", str(len(response.body)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(response.body)
                except OSError:
                    pass

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = TestServer(("127.0.0.1", 0), Handler)
        self._server.queued_responses = queued_responses
        self._server.recorded_requests = []
        host, port = self._server.server_address
        self.base_url = f"http://{host}:{port}/api/v2/"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()

    @property
    def requests(self) -> list[RecordedHttpRequest]:
        return self._server.recorded_requests

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


@pytest.fixture
def eagle_server():
    servers: list[EagleTestServer] = []

    def create(*responses: ServerResponse) -> EagleTestServer:
        server = EagleTestServer(list(responses))
        servers.append(server)
        return server

    yield create

    for server in servers:
        server.close()


class RecordingTransport:
    def __init__(self, *responses: EagleHttpResponse | Exception) -> None:
        self.responses = deque(responses)
        self.requests: list[EagleHttpRequest] = []
        self.options: list[tuple[float, int]] = []

    def send(
        self,
        request: EagleHttpRequest,
        *,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> EagleHttpResponse:
        self.requests.append(request)
        self.options.append((timeout_seconds, maximum_response_bytes))
        if not self.responses:
            raise AssertionError("Unexpected Eagle HTTP request.")
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def transport_response(
    data: object,
    *,
    status_code: int = 200,
) -> EagleHttpResponse:
    return EagleHttpResponse(
        status_code=status_code,
        headers={"Content-Type": "application/json"},
        body=jsend(data),
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:41595/api/v2/",
        "http://127.0.0.1:41595/api/v2",
        "http://[::1]:41595/api/v2/",
    ],
)
def test_base_url_allows_only_explicit_loopback_hosts(base_url):
    EagleWebApiBridge(base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://localhost:41595/api/v2/",
        "http://0.0.0.0:41595/api/v2/",
        "http://192.168.1.2:41595/api/v2/",
        "http://example.com/api/v2/",
        "http://user@localhost:41595/api/v2/",
        "http://localhost:41595/api/v2/?token=x",
        "http://localhost:41595/api/v2/#fragment",
        "http://localhost:41595/api/v2/\r\nX-Test: bad",
        "http://localhost:41595/",
        "http://localhost:41595/api/v1/",
        "http://localhost:41595/prefix/api/v2/",
        "http://localhost:41595/api/v2//",
        "http://localhost:41595/api//v2/",
        "http://localhost:41595/api/./v2/",
        "http://localhost:41595/api/other/../v2/",
        "http://localhost:41595/api/v2/.",
        "http://localhost:41595/api/v2/../",
        "http://localhost:41595/api/%2e/v2/",
        "http://localhost:41595/api/v2/%2E%2E/",
        "http://localhost:41595/api%2fv2/",
        "http://localhost:41595/api/v2%2f",
        r"http://localhost:41595/api\v2/",
    ],
)
def test_base_url_rejects_nonlocal_or_ambiguous_urls(base_url):
    with pytest.raises(ValueError):
        EagleWebApiBridge(base_url)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": "3"},
        {"maximum_response_bytes": 0},
        {"maximum_response_bytes": True},
        {"user_agent": ""},
        {"user_agent": "valid\r\nInjected: yes"},
    ],
)
def test_transport_configuration_is_strict(kwargs):
    with pytest.raises(ValueError):
        EagleWebApiBridge(**kwargs)


def test_base_url_without_trailing_slash_is_normalized_exactly():
    transport = RecordingTransport(transport_response(app_info()))
    bridge = EagleWebApiBridge(
        "http://127.0.0.1:54321/api/v2",
        transport=transport,
    )

    assert bridge.test_connection().succeeded is True
    assert transport.requests[0].url == (
        "http://127.0.0.1:54321/api/v2/app/info"
    )


def test_connection_uses_expected_method_path_headers_and_limits(eagle_server):
    server = eagle_server(ServerResponse(jsend(app_info("4.0.0", "Build 21"))))
    bridge = EagleWebApiBridge(
        server.base_url,
        timeout_seconds=1.25,
        maximum_response_bytes=8192,
        user_agent="DanbooruMultiSearch/Test EagleBridge",
    )

    result = bridge.test_connection()

    assert result.succeeded is True
    assert result.value is not None
    assert result.value.version == "4.0.0"
    assert result.value.build_version == "Build 21"
    assert result.value.build_number == 21
    assert result.value.platform == "win32"
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request.method == "GET"
    assert request.path == "/api/v2/app/info"
    assert request.body == b""
    assert request.headers["accept"] == "application/json"
    assert request.headers["user-agent"] == "DanbooruMultiSearch/Test EagleBridge"
    assert "content-type" not in request.headers


def test_connection_accepts_current_eagle_numeric_build_version():
    transport = RecordingTransport(
        transport_response(
            {
                "version": "4.0.0",
                "prereleaseVersion": None,
                "buildVersion": "20260401",
                "platform": "win32",
            }
        )
    )

    result = EagleWebApiBridge(transport=transport).test_connection()

    assert result.succeeded is True
    assert result.value is not None
    assert result.value.build_version == "20260401"
    assert result.value.build_number == 20_260_401


@pytest.mark.parametrize(
    ("version", "build_version", "succeeded", "expected_code"),
    [
        ("3.9.9", "build100", False, "unsupported_eagle_version"),
        ("4.0.0", "build20", False, "unsupported_eagle_version"),
        ("4.0.0", "0", False, "unsupported_eagle_version"),
        ("4.0.0", "20", False, "unsupported_eagle_version"),
        ("4.0.0", "21", True, None),
        ("4.0.0", "00021", True, None),
        ("4.0.0", "20260401", True, None),
        ("4.0", "build21", True, None),
        ("4.0.9", "Build 100", True, None),
        ("4.1.0", "build1", True, None),
        ("5", None, True, None),
        ("10.0", None, True, None),
        ("4.0", None, False, "unsupported_eagle_version"),
        ("4.x", "build100", False, "eagle_invalid_app_info"),
        ("5.0", "release100", False, "eagle_invalid_app_info"),
    ],
)
def test_version_boundaries(version, build_version, succeeded, expected_code):
    transport = RecordingTransport(
        transport_response(app_info(version, build_version))
    )
    bridge = EagleWebApiBridge(
        transport=transport,
        timeout_seconds=2.5,
        maximum_response_bytes=12345,
    )

    result = bridge.test_connection()

    assert result.succeeded is succeeded
    assert result.code == expected_code
    assert transport.options == [(2.5, 12345)]
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "build_version",
    [
        "",
        "   ",
        "-1",
        "+21",
        "release100",
        "21.0",
        "build-21",
        "0x15",
    ],
)
def test_invalid_build_version_formats_are_rejected(build_version):
    transport = RecordingTransport(
        transport_response(app_info("5.0.0", build_version))
    )

    result = EagleWebApiBridge(transport=transport).test_connection()

    assert result.succeeded is False
    assert result.code == "eagle_invalid_app_info"


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"prereleaseVersion": None, "buildVersion": "build21", "platform": "win32"},
        {"version": 4, "prereleaseVersion": None, "buildVersion": "build21", "platform": "win32"},
        {"version": "4.0", "buildVersion": "build21", "platform": "win32"},
        {"version": "4.0", "prereleaseVersion": 1, "buildVersion": "build21", "platform": "win32"},
        {"version": "4.0", "prereleaseVersion": None, "platform": "win32"},
        {"version": "4.0", "prereleaseVersion": None, "buildVersion": "build21"},
    ],
)
def test_application_info_fields_are_strict(data):
    result = EagleWebApiBridge(
        transport=RecordingTransport(transport_response(data))
    ).test_connection()

    assert result.code == "eagle_invalid_app_info"


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (
            EagleHttpResponse(503, {}, b'{"status":"error","message":"busy"}'),
            "eagle_http_error",
        ),
        (EagleHttpResponse(200, {}, b"not-json"), "eagle_invalid_json"),
        (
            EagleHttpResponse(200, {}, b'{"status":"error","message":"busy"}'),
            "eagle_api_error",
        ),
        (
            EagleHttpResponse(200, {}, b'{"status":"unknown","data":{}}'),
            "eagle_invalid_jsend",
        ),
        (
            EagleHttpResponse(200, {}, b'{"status":"success"}'),
            "eagle_invalid_jsend",
        ),
        (EagleTimeoutError("timeout"), "eagle_timeout"),
        (EagleConnectionError("refused"), "eagle_connection_error"),
        (
            EagleResponseTooLargeError("too large"),
            "eagle_response_too_large",
        ),
        (HTTPException("broken protocol"), "eagle_connection_error"),
    ],
)
def test_response_and_transport_errors_have_stable_codes(response, expected_code):
    bridge = EagleWebApiBridge(transport=RecordingTransport(response))

    result = bridge.test_connection()

    assert result.succeeded is False
    assert result.code == expected_code
    assert result.message


@pytest.mark.parametrize("include_content_length", [True, False])
def test_default_transport_enforces_declared_and_actual_response_size(
    eagle_server,
    include_content_length,
):
    server = eagle_server(
        ServerResponse(
            b"x" * 100,
            include_content_length=include_content_length,
        )
    )
    bridge = EagleWebApiBridge(
        server.base_url,
        maximum_response_bytes=16,
    )

    result = bridge.test_connection()

    assert result.code == "eagle_response_too_large"
    assert len(server.requests) == 1


def test_redirect_is_not_followed(eagle_server):
    server = eagle_server(
        ServerResponse(
            b"redirect",
            status=302,
            headers={"Location": "http://example.com/remote"},
        )
    )
    bridge = EagleWebApiBridge(server.base_url)

    result = bridge.test_connection()

    assert result.code == "eagle_http_error"
    assert "302" in (result.message or "")
    assert len(server.requests) == 1


class FakeResponse:
    def __init__(self, body: bytes, content_length: str | None = None) -> None:
        self._body = body
        self.closed = False
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def read(self, limit: int) -> bytes:
        return self._body[:limit]

    def getcode(self) -> int:
        return 200

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    def open(self, request, timeout):
        return self.response


class RaisingOpener:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def open(self, request, timeout):
        raise self.error


def test_default_transport_closes_responses_on_success_and_size_failure():
    successful_response = FakeResponse(b"{}")
    successful_transport = UrllibEagleHttpTransport(FakeOpener(successful_response))
    request = EagleHttpRequest("GET", "http://localhost/", {}, None)

    successful_transport.send(
        request,
        timeout_seconds=1,
        maximum_response_bytes=10,
    )

    assert successful_response.closed is True

    oversized_response = FakeResponse(b"x" * 20)
    oversized_transport = UrllibEagleHttpTransport(FakeOpener(oversized_response))
    with pytest.raises(EagleResponseTooLargeError):
        oversized_transport.send(
            request,
            timeout_seconds=1,
            maximum_response_bytes=10,
        )
    assert oversized_response.closed is True

    declared_oversized_response = FakeResponse(b"", content_length="20")
    declared_oversized_transport = UrllibEagleHttpTransport(
        FakeOpener(declared_oversized_response)
    )
    with pytest.raises(EagleResponseTooLargeError):
        declared_oversized_transport.send(
            request,
            timeout_seconds=1,
            maximum_response_bytes=10,
        )
    assert declared_oversized_response.closed is True


def test_default_transport_distinguishes_timeout_and_connection_failure():
    request = EagleHttpRequest("GET", "http://localhost/", {}, None)
    timeout_transport = UrllibEagleHttpTransport(
        RaisingOpener(URLError(socket.timeout("timed out")))
    )
    connection_transport = UrllibEagleHttpTransport(
        RaisingOpener(URLError(ConnectionRefusedError("refused")))
    )

    with pytest.raises(EagleTimeoutError):
        timeout_transport.send(
            request,
            timeout_seconds=1,
            maximum_response_bytes=10,
        )
    with pytest.raises(EagleConnectionError):
        connection_transport.send(
            request,
            timeout_seconds=1,
            maximum_response_bytes=10,
        )

    protocol_transport = UrllibEagleHttpTransport(
        RaisingOpener(HTTPException("bad status line"))
    )
    with pytest.raises(EagleProtocolError):
        protocol_transport.send(
            request,
            timeout_seconds=1,
            maximum_response_bytes=10,
        )


class IncompleteFakeResponse(FakeResponse):
    def read(self, limit: int) -> bytes:
        raise IncompleteRead(partial=b"partial", expected=100)


def test_default_transport_wraps_incomplete_read_and_closes_response():
    response = IncompleteFakeResponse(b"")
    transport = UrllibEagleHttpTransport(FakeOpener(response))

    with pytest.raises(EagleProtocolError):
        transport.send(
            EagleHttpRequest("GET", "http://localhost/", {}, None),
            timeout_seconds=1,
            maximum_response_bytes=1000,
        )

    assert response.closed is True


def test_truncated_local_http_response_becomes_a_stable_client_error(eagle_server):
    server = eagle_server(
        ServerResponse(
            b'{"status":"success"',
            headers={"Content-Length": "1000"},
            include_content_length=False,
        )
    )

    result = EagleWebApiBridge(server.base_url).test_connection()

    assert result.code == "eagle_connection_error"
    assert "incomplete HTTP response" in (result.message or "")
    assert len(server.requests) == 1


def test_deeply_nested_json_becomes_invalid_json_instead_of_recursion_error():
    deep_json = (
        b'{"status":"success","data":'
        + (b"[" * 2000)
        + b"0"
        + (b"]" * 2000)
        + b"}"
    )
    transport = RecordingTransport(EagleHttpResponse(200, {}, deep_json))

    result = EagleWebApiBridge(transport=transport).test_connection()

    assert result.code == "eagle_invalid_json"


def test_library_parsing_and_identifier_use_normalized_windows_path():
    paths = [
        r"D:\Eagle\Art.library",
        r"d:/eagle/./ART.library/",
        r"D:\Eagle\folder\..\Art.library",
    ]
    identifiers = {library_identifier_from_windows_path(path) for path in paths}

    assert len(identifiers) == 1
    assert normalize_windows_library_path(paths[0]) == r"d:\eagle\art.library"

    transport = RecordingTransport(transport_response(library_info(paths[0])))
    result = EagleWebApiBridge(transport=transport).get_current_library()
    assert result.succeeded is True
    assert result.value is not None
    assert result.value.identifier in identifiers
    assert result.value.name == "Illustrations"


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        (b'{"status":"error","message":"No library is currently open"}', "eagle_no_library"),
        (jsend(None), "eagle_no_library"),
        (jsend({}), "eagle_no_library"),
        (jsend({"name": "Library", "path": "relative.library"}), "eagle_invalid_library"),
        (jsend({"name": 1, "path": r"D:\Library.library"}), "eagle_invalid_library"),
    ],
)
def test_library_errors_are_explicit(body, expected_code):
    transport = RecordingTransport(EagleHttpResponse(200, {}, body))

    result = EagleWebApiBridge(transport=transport).get_current_library()

    assert result.code == expected_code


def test_folder_pages_and_nested_children_are_flattened(eagle_server):
    first_page = {
        "data": [
            {
                "id": "root-1",
                "name": "Root One",
                "children": [
                    {"id": "child-1", "name": "Child One", "children": []}
                ],
            }
        ],
        "total": 2,
        "offset": 0,
        "limit": 1,
    }
    second_page = {
        "data": [{"id": "root-2", "name": "Root Two", "children": []}],
        "total": 2,
        "offset": 1,
        "limit": 1,
    }
    server = eagle_server(
        ServerResponse(jsend(first_page)),
        ServerResponse(jsend(second_page)),
    )
    bridge = EagleWebApiBridge(server.base_url)

    result = bridge.list_folders()

    assert result.succeeded is True
    assert result.value is not None
    assert [folder.identifier for folder in result.value] == [
        "root-1",
        "child-1",
        "root-2",
    ]
    assert result.value[1].parent_identifier == "root-1"
    assert [request.path for request in server.requests] == [
        "/api/v2/folder/get?offset=0&limit=1000",
        "/api/v2/folder/get?offset=1&limit=1000",
    ]


class InMemoryFolderBridge(EagleWebApiBridge):
    def __init__(self, page: dict[str, object]) -> None:
        super().__init__(transport=RecordingTransport())
        self.page = page

    def _request_data(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        payload=None,
    ) -> object:
        return self.page


def test_concrete_client_exposes_immutable_configuration_without_changing_transport():
    from dataclasses import FrozenInstanceError
    original_transport = RecordingTransport(transport_response(app_info()))
    bridge = EagleWebApiBridge(transport=original_transport, timeout_seconds=.75,
                              maximum_response_bytes=1024, user_agent='worker test')
    configuration = bridge.configuration
    assert configuration.base_url == 'http://localhost:41595/api/v2/'
    assert configuration.timeout_seconds == .75 and configuration.maximum_response_bytes == 1024
    assert configuration.user_agent == 'worker test'
    with pytest.raises(FrozenInstanceError):
        configuration.timeout_seconds = 10
    assert bridge.test_connection().succeeded and len(original_transport.requests) == 1


def test_create_folder_uses_one_post_and_validates_returned_folder():
    transport = RecordingTransport(transport_response({'id': 'NEW', 'name': '中文 文件夹', 'children': []}))
    result = EagleWebApiBridge(transport=transport).create_folder('中文 文件夹', parent_identifier='PARENT')
    assert result.succeeded and result.value.identifier == 'NEW' and result.value.parent_identifier == 'PARENT'
    assert len(transport.requests) == 1 and transport.requests[0].method == 'POST'
    assert transport.requests[0].url.endswith('/folder/create')
    assert json.loads(transport.requests[0].body) == {'name': '中文 文件夹', 'parent': 'PARENT'}


@pytest.mark.parametrize('kind', ['page_overflow', 'page_limit', 'node_limit', 'cancel'])
def test_gui_folder_boundaries_and_cancel_stop_loading(kind):
    node = {'id': 'P', 'name': 'parent', 'children': [{'id': 'C', 'name': 'child', 'children': []}]}
    page = {'data': [node], 'total': 2, 'offset': 0, 'limit': 1}
    if kind == 'page_overflow':
        page['data'] += [{'id': 'OTHER', 'name': 'other', 'children': []}]
    bridge = InMemoryFolderBridge(page)
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        return calls >= 2
    options = {'maximum_pages': 1}
    if kind == 'node_limit':
        options['maximum_folders'] = 1
    if kind == 'cancel':
        options['cancelled'] = cancel
    result = bridge.list_folders(**options)
    assert not result.succeeded
    assert result.code == ('eagle_invalid_folders' if kind == 'page_overflow' else
                           'eagle_cancelled' if kind == 'cancel' else 'eagle_folder_limit')


def test_1200_nested_folder_levels_use_an_explicit_stack():
    current: dict[str, object] = {
        "id": "folder-1200",
        "name": "Folder 1200",
        "children": [],
    }
    for level in reversed(range(1200)):
        current = {
            "id": f"folder-{level}",
            "name": f"Folder {level}",
            "children": [current],
        }
    page = {"data": [current], "total": 1, "offset": 0, "limit": 1}

    result = InMemoryFolderBridge(page).list_folders()

    assert result.succeeded is True
    assert result.value is not None
    assert len(result.value) == 1201
    assert result.value[0].identifier == "folder-0"
    assert result.value[-1].identifier == "folder-1200"
    assert result.value[-1].parent_identifier == "folder-1199"


def test_in_memory_folder_cycle_is_rejected_without_recursion():
    root: dict[str, object] = {
        "id": "root",
        "name": "Root",
        "children": [],
    }
    children = root["children"]
    assert isinstance(children, list)
    children.append(root)
    page = {"data": [root], "total": 1, "offset": 0, "limit": 1}

    result = InMemoryFolderBridge(page).list_folders()

    assert result.code == "eagle_invalid_folders"


@pytest.mark.parametrize(
    "page",
    [
        {
            "data": [],
            "total": 1,
            "offset": 0,
            "limit": 50,
        },
        {
            "data": [
                {
                    "id": "same",
                    "name": "Parent",
                    "children": [
                        {"id": "same", "name": "Child", "children": []}
                    ],
                }
            ],
            "total": 1,
            "offset": 0,
            "limit": 50,
        },
    ],
)
def test_folder_pagination_and_duplicate_guards(page):
    bridge = EagleWebApiBridge(transport=RecordingTransport(transport_response(page)))

    result = bridge.list_folders()

    assert result.code == "eagle_invalid_folders"


def test_import_sends_only_validated_fields_once(eagle_server, tmp_path):
    source_file = tmp_path / "插图 文件.png"
    source_file.write_bytes(b"image")
    folder_page = {
        "data": [
            {
                "id": "root",
                "name": "Root",
                "children": [
                    {"id": "target", "name": "Target", "children": []}
                ],
            }
        ],
        "total": 1,
        "offset": 0,
        "limit": 1000,
    }
    server = eagle_server(
        ServerResponse(jsend(app_info())),
        ServerResponse(jsend(library_info())),
        ServerResponse(jsend(folder_page)),
        ServerResponse(jsend({"id": "ITEM-123"})),
    )
    bridge = EagleWebApiBridge(server.base_url, user_agent="Eagle-Test/1")
    tags = ["cat", "插图"]
    metadata: dict[str, object] = {
        "name": "作品",
        "website": "https://danbooru.donmai.us/posts/1",
        "tags": tags,
        "annotation": "说明",
    }
    metadata_before = {**metadata, "tags": list(tags)}

    result = bridge.import_file(
        source_file,
        folder_identifier="target",
        metadata=metadata,
    )

    assert result.succeeded is True
    assert result.value is not None
    assert result.value.item_identifier == "ITEM-123"
    assert metadata == metadata_before
    assert tags == ["cat", "插图"]
    assert [request.method for request in server.requests] == [
        "GET",
        "GET",
        "GET",
        "POST",
    ]
    assert [request.path for request in server.requests] == [
        "/api/v2/app/info",
        "/api/v2/library/info",
        "/api/v2/folder/get?offset=0&limit=1000",
        "/api/v2/item/add",
    ]
    post_request = server.requests[-1]
    assert post_request.headers["content-type"] == "application/json"
    assert post_request.headers["user-agent"] == "Eagle-Test/1"
    assert json.loads(post_request.body) == {
        "path": str(source_file.resolve()),
        "name": "作品",
        "website": "https://danbooru.donmai.us/posts/1",
        "tags": ["cat", "插图"],
        "annotation": "说明",
        "folders": ["target"],
    }


def test_invalid_folder_stops_before_item_add(eagle_server, tmp_path):
    source_file = tmp_path / "image.png"
    source_file.write_bytes(b"image")
    folder_page = {
        "data": [{"id": "other", "name": "Other", "children": []}],
        "total": 1,
        "offset": 0,
        "limit": 1000,
    }
    server = eagle_server(
        ServerResponse(jsend(app_info())),
        ServerResponse(jsend(library_info())),
        ServerResponse(jsend(folder_page)),
    )

    result = EagleWebApiBridge(server.base_url).import_file(
        source_file,
        folder_identifier="missing",
    )

    assert result.code == "eagle_folder_not_found"
    assert [request.path for request in server.requests] == [
        "/api/v2/app/info",
        "/api/v2/library/info",
        "/api/v2/folder/get?offset=0&limit=1000",
    ]


def test_import_without_metadata_or_folder_sends_only_absolute_path(
    eagle_server,
    tmp_path,
):
    source_file = tmp_path / "image.png"
    source_file.write_bytes(b"image")
    server = eagle_server(
        ServerResponse(jsend(app_info())),
        ServerResponse(jsend(library_info())),
        ServerResponse(jsend({"id": "ITEM-ONLY-PATH"})),
    )

    result = EagleWebApiBridge(server.base_url).import_file(source_file)

    assert result.succeeded is True
    assert [request.path for request in server.requests] == [
        "/api/v2/app/info",
        "/api/v2/library/info",
        "/api/v2/item/add",
    ]
    assert json.loads(server.requests[-1].body) == {
        "path": str(source_file.resolve())
    }


@pytest.mark.parametrize(
    ("responses", "expected_code", "expected_request_count"),
    [
        (
            [ServerResponse(jsend(app_info("4.0.0", "build20")))],
            "unsupported_eagle_version",
            1,
        ),
        (
            [
                ServerResponse(jsend(app_info())),
                ServerResponse(jsend(None)),
            ],
            "eagle_no_library",
            2,
        ),
    ],
)
def test_import_stops_when_version_or_library_precheck_fails(
    eagle_server,
    tmp_path,
    responses,
    expected_code,
    expected_request_count,
):
    source_file = tmp_path / "image.png"
    source_file.write_bytes(b"image")
    server = eagle_server(*responses)

    result = EagleWebApiBridge(server.base_url).import_file(source_file)

    assert result.code == expected_code
    assert len(server.requests) == expected_request_count
    assert all(request.path != "/api/v2/item/add" for request in server.requests)


@pytest.mark.parametrize(
    ("import_response", "expected_code"),
    [
        (jsend({}), "eagle_missing_item_identifier"),
        (jsend({"id": ""}), "eagle_missing_item_identifier"),
        (b'{"status":"error","message":"cannot import"}', "eagle_api_error"),
    ],
)
def test_import_requires_success_with_nonempty_identifier_and_never_retries(
    eagle_server,
    tmp_path,
    import_response,
    expected_code,
):
    source_file = tmp_path / "image.png"
    source_file.write_bytes(b"image")
    server = eagle_server(
        ServerResponse(jsend(app_info())),
        ServerResponse(jsend(library_info())),
        ServerResponse(import_response),
    )

    result = EagleWebApiBridge(server.base_url).import_file(source_file)

    assert result.code == expected_code
    assert [request.path for request in server.requests].count("/api/v2/item/add") == 1


@pytest.mark.parametrize("kind", ["missing", "directory", "empty", "network"])
def test_invalid_files_do_not_make_requests(tmp_path, kind):
    transport = RecordingTransport()
    bridge = EagleWebApiBridge(transport=transport)
    if kind == "missing":
        path = tmp_path / "missing.png"
        expected_code = "eagle_file_not_found"
    elif kind == "directory":
        path = tmp_path
        expected_code = "eagle_not_a_file"
    elif kind == "empty":
        path = tmp_path / "empty.png"
        path.write_bytes(b"")
        expected_code = "eagle_empty_file"
    else:
        path = Path(r"\\server\share\image.png")
        expected_code = "eagle_network_file_unsupported"

    result = bridge.import_file(path)

    assert result.code == expected_code
    assert transport.requests == []


@pytest.mark.parametrize(
    "metadata",
    [
        {"source": "x"},
        {"path": "x"},
        {"url": "x"},
        {"base64": "x"},
        {"bookmarkURL": "x"},
        {"items": []},
        {"id": "injected"},
        {"name": 1},
        {"website": True},
        {"annotation": []},
        {"tags": "cat"},
        {"tags": ["cat", 1]},
        {"tags": ["cat", "   "]},
    ],
)
def test_invalid_metadata_cannot_inject_fields_or_make_requests(tmp_path, metadata):
    source_file = tmp_path / "image.png"
    source_file.write_bytes(b"image")
    transport = RecordingTransport()
    bridge = EagleWebApiBridge(transport=transport)

    result = bridge.import_file(source_file, metadata=metadata)

    assert result.code == "eagle_invalid_metadata"
    assert transport.requests == []


@pytest.mark.parametrize("folder_identifier", ["", "   ", 1, [], True])
def test_invalid_folder_identifier_does_not_make_requests(
    tmp_path,
    folder_identifier,
):
    source_file = tmp_path / "image.png"
    source_file.write_bytes(b"image")
    transport = RecordingTransport()

    result = EagleWebApiBridge(transport=transport).import_file(
        source_file,
        folder_identifier=folder_identifier,  # type: ignore[arg-type]
    )

    assert result.code == "eagle_invalid_folder_identifier"
    assert transport.requests == []
