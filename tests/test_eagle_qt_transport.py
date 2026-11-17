from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread, Timer
import time

import pytest
from PySide6.QtCore import QThread

from danbooru_multi_search.integrations.eagle.qt_transport import QtEagleHttpTransport
from danbooru_multi_search.integrations.eagle.transport import (
    EagleHttpRequest, EagleResponseTooLargeError, EagleTimeoutError,
)
from tests.test_favorite_controller import APPLICATION


@pytest.fixture
def server():
    values = []
    def create(mode, *, payload=b'{"status":"success","data":{}}'):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.requests += 1
                try:
                    if mode == 'headers':
                        self.connection.sendall(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                        for _ in range(100):
                            self.connection.sendall(b'x')
                            time.sleep(.03)
                        return
                    self.send_response(200)
                    self.send_header('Content-Length', str(100 if mode == 'truncated' else len(payload)))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    if mode == 'body':
                        for value in payload:
                            self.wfile.write(bytes([value]))
                            self.wfile.flush()
                            time.sleep(.03)
                    else:
                        self.wfile.write(payload)
                except OSError:
                    pass
            do_POST = do_GET
            def log_message(self, *args):
                pass
        instance = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        instance.daemon_threads = True
        instance.requests = 0
        thread = Thread(target=lambda: instance.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        values.append((instance, thread))
        return instance, f'http://127.0.0.1:{instance.server_address[1]}/api/v2/app/info'
    yield create
    for instance, thread in values:
        instance.shutdown()
        instance.server_close()
        thread.join(2)


class RequestThread(QThread):
    def __init__(self, url, *, event=None, timeout=.15, budget=2, maximum=1024, method='GET'):
        super().__init__()
        self.url, self.cancel = url, event or Event()
        self.timeout, self.budget, self.maximum, self.method = timeout, budget, maximum, method
        self.error = self.response = None
        self.sent = False
        self.elapsed = 0

    def run(self):
        start = time.monotonic()
        transport = QtEagleHttpTransport(self.cancel.is_set, start + self.budget)
        try:
            self.response = transport.send(EagleHttpRequest(self.method, self.url, {}, b'{}' if self.method == 'POST' else None),
                timeout_seconds=self.timeout, maximum_response_bytes=self.maximum)
        except Exception as exc:
            self.error = exc
        finally:
            self.sent = transport.sent_post
            transport.close()
            self.elapsed = time.monotonic() - start


@pytest.mark.parametrize('phase', ['headers', 'body'])
def test_total_deadline_covers_slow_headers_and_slow_body(server, phase):
    _instance, url = server(phase)
    thread = RequestThread(url)
    thread.start()
    assert thread.wait(2000)
    assert isinstance(thread.error, EagleTimeoutError) and thread.response is None
    assert thread.elapsed < 1


@pytest.mark.parametrize('phase', ['headers', 'body'])
def test_cancel_during_network_wait_finishes_without_gui_completion(server, phase):
    _instance, url = server(phase)
    event = Event()
    thread = RequestThread(url, event=event, timeout=3, budget=15)
    timer = Timer(.08, event.set)
    thread.start()
    timer.start()
    assert thread.wait(2000)
    timer.join()
    assert thread.error is not None and thread.response is None and thread.elapsed < 1


def test_worker_budget_is_shared_with_each_request(server):
    _instance, url = server('headers')
    thread = RequestThread(url, timeout=3, budget=.1)
    thread.start()
    assert thread.wait(2000) and isinstance(thread.error, EagleTimeoutError) and thread.elapsed < 1


@pytest.mark.parametrize('mode,maximum', [('truncated', 1024), ('success', 3)])
def test_incomplete_or_oversize_json_is_never_a_successful_response(server, mode, maximum):
    _instance, url = server(mode)
    thread = RequestThread(url, maximum=maximum)
    thread.start()
    assert thread.wait(2000) and thread.error is not None and thread.response is None
    if mode == 'success':
        assert isinstance(thread.error, EagleResponseTooLargeError)


def test_cancel_before_post_does_not_send_or_claim_mutation(server):
    instance, url = server('success')
    event = Event()
    event.set()
    thread = RequestThread(url, event=event, method='POST')
    thread.start()
    assert thread.wait(2000)
    assert not thread.sent and instance.requests == 0 and thread.error is not None


def test_success_returns_exact_status_headers_and_bytes(server):
    payload = b'{"status":"success","data":{"name":"ok"}}'
    instance, url = server('success', payload=payload)
    thread = RequestThread(url, timeout=1)
    thread.start()
    assert thread.wait(2000) and thread.error is None
    assert thread.response.status_code == 200 and thread.response.body == payload and instance.requests == 1
