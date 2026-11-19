from __future__ import annotations

import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QTimer, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)
from PySide6.QtQuick import QQuickItem

from danbooru_multi_search.ui.image_cache import (
    DEFAULT_IMAGE_CACHE_BYTES,
    MAXIMUM_IMAGE_CACHE_BYTES,
    MINIMUM_IMAGE_CACHE_BYTES,
    CacheActionResult,
    ImageCacheController,
    ImageNetworkAccessManager,
    ImageNetworkAccessManagerFactory,
    cache_key,
    is_complete_image_response,
    normalized_cache_url,
)
from danbooru_multi_search.danbooru import DanbooruPost
from danbooru_multi_search.danbooru.client import DEFAULT_TIMEOUT_MS, USER_AGENT
from danbooru_multi_search.ui.launcher import create_qml_engine


APPLICATION = QGuiApplication.instance() or QGuiApplication([])
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
)


class ImageHttpServer:
    def __init__(
        self,
        *,
        status: int = 200,
        content_type: str = "image/png",
        cache_control: str = "public, max-age=3600",
        body: bytes = PNG_BYTES,
        delay: float = 0,
        redirect: bool = False,
    ) -> None:
        class TestServer(ThreadingHTTPServer):
            daemon_threads = True

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.server.paths.append(self.path)  # type: ignore[attr-defined]
                if self.server.delay:  # type: ignore[attr-defined]
                    time.sleep(self.server.delay)  # type: ignore[attr-defined]
                if (
                    self.server.redirect  # type: ignore[attr-defined]
                    and self.path.startswith("/redirect")
                ):
                    self.send_response(302)
                    self.send_header("Location", "/image.png")
                    self.send_header(
                        "Cache-Control",
                        self.server.cache_control,  # type: ignore[attr-defined]
                    )
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(self.server.status)  # type: ignore[attr-defined]
                self.send_header(
                    "Content-Type",
                    self.server.content_type,  # type: ignore[attr-defined]
                )
                self.send_header(
                    "Cache-Control",
                    self.server.cache_control,  # type: ignore[attr-defined]
                )
                self.send_header(
                    "Content-Length",
                    str(len(self.server.body)),  # type: ignore[attr-defined]
                )
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(self.server.body)  # type: ignore[attr-defined]
                except OSError:
                    pass

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = TestServer(("127.0.0.1", 0), Handler)
        self._server.status = status
        self._server.content_type = content_type
        self._server.cache_control = cache_control
        self._server.body = body
        self._server.delay = delay
        self._server.redirect = redirect
        self._server.paths = []
        host, port = self._server.server_address
        self.final_url = f"http://{host}:{port}/image.png"
        self.url = (
            f"http://{host}:{port}/redirect"
            if redirect
            else self.final_url
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()

    @property
    def paths(self) -> list[str]:
        return self._server.paths

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


@pytest.fixture
def image_server():
    servers: list[ImageHttpServer] = []

    def create(**kwargs) -> ImageHttpServer:
        server = ImageHttpServer(**kwargs)
        servers.append(server)
        return server

    yield create

    for server in servers:
        server.close()


def _fetch(manager: QNetworkAccessManager, url: str) -> tuple[QNetworkReply, bytes]:
    reply = manager.get(QNetworkRequest(QUrl(url)))
    loop = QEventLoop()
    timeout = QTimer()
    timeout.setSingleShot(True)
    reply.finished.connect(loop.quit)
    timeout.timeout.connect(loop.quit)
    if not reply.isFinished():
        timeout.start(3_000)
        loop.exec()
    timeout.stop()
    assert reply.isFinished(), "image request did not finish within the test timeout"
    return reply, bytes(reply.readAll())


def _wait_until(predicate, timeout_ms: int = 3_000) -> bool:
    elapsed = 0
    while elapsed < timeout_ms:
        APPLICATION.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
        elapsed += 10
    return predicate()


def _dispose(*objects: QObject) -> None:
    for value in objects:
        value.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    APPLICATION.processEvents()


class FakeDiskCache:
    def __init__(self, _parent: QObject) -> None:
        self.directory = ""
        self.maximum = 0
        self.entries: set[bytes] = set()
        self.clear_count = 0
        self.size = 0

    def setCacheDirectory(self, value: str) -> None:
        self.directory = value

    def setMaximumCacheSize(self, value: int) -> None:
        self.maximum = value

    def maximumCacheSize(self) -> int:
        return self.maximum

    def cacheSize(self) -> int:
        return self.size

    def remove(self, url: QUrl) -> bool:
        key = cache_key(url)
        existed = key in self.entries
        self.entries.discard(key)
        return existed

    def clear(self) -> None:
        self.clear_count += 1
        self.entries.clear()
        self.size = 0


def _controller(tmp_path: Path) -> tuple[ImageCacheController, FakeDiskCache]:
    created: list[FakeDiskCache] = []

    def build(parent: QObject):
        cache = FakeDiskCache(parent)
        created.append(cache)
        return cache

    controller = ImageCacheController(
        tmp_path / "images",
        disk_cache_factory=build,
    )
    cache = controller._ensure_cache(QObject())
    assert cache is created[0]
    return controller, created[0]


def test_normalized_cache_key_preserves_query_and_removes_fragment():
    first = normalized_cache_url(
        "HTTPS://Example.Invalid/a/../image.png?size=large#first"
    )
    second = normalized_cache_url(
        "https://example.invalid/image.png?size=large#second"
    )

    assert first.toString() == "https://example.invalid/image.png?size=large"
    assert cache_key(first) == cache_key(second)
    assert cache_key(first) != cache_key(
        "https://example.invalid/image.png?size=preview"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"operation": QNetworkAccessManager.Operation.PostOperation},
        {"url": "file:///tmp/image.png"},
        {"network_error": QNetworkReply.NetworkError.TimeoutError},
        {"http_status": 206},
        {"http_status": 404},
        {"content_type": "text/html"},
        {"content_type": ""},
        {"received_bytes": 0},
        {"cache_control": b"public, NO-STORE"},
        {"cache_save_allowed": False},
    ],
)
def test_only_complete_http_200_images_are_cacheable(changes):
    values = {
        "operation": QNetworkAccessManager.Operation.GetOperation,
        "url": "https://example.invalid/image.png",
        "network_error": QNetworkReply.NetworkError.NoError,
        "http_status": 200,
        "content_type": "image/png; charset=binary",
        "received_bytes": 12,
        "cache_control": b"public, max-age=3600",
        "cache_save_allowed": True,
    }
    values.update(changes)

    assert is_complete_image_response(**values) is False


def test_complete_image_response_is_cacheable():
    assert is_complete_image_response(
        operation=QNetworkAccessManager.Operation.GetOperation,
        url="https://example.invalid/image.png?x=1",
        network_error=QNetworkReply.NetworkError.NoError,
        http_status=200,
        content_type=b"IMAGE/PNG",
        received_bytes=1,
        cache_control=b"public, max-age=60",
    )


def test_decode_failure_removes_original_and_final_once(tmp_path):
    controller, cache = _controller(tmp_path)
    original = "https://example.invalid/redirect#fragment"
    final = "https://cdn.invalid/image.png?x=1"
    cache.entries.update({cache_key(original), cache_key(final)})

    assert controller.report_decode_failure(original, final) is True
    assert cache.entries == set()
    assert controller.take_always_network(original) is True
    assert controller.take_always_network(original) is False
    assert controller.report_decode_failure(original, final) is False

    controller.report_decode_success(original)
    assert controller.report_decode_failure(original, final) is True


def test_manual_retry_succeeds_when_entries_are_absent(tmp_path):
    controller, _cache = _controller(tmp_path)

    result = controller.prepare_manual_retry(
        "https://example.invalid/image.png",
        "https://cdn.invalid/final.png",
    )

    assert result == CacheActionResult(True)
    assert controller.take_always_network(
        "https://example.invalid/image.png#ignored"
    ) is True


@pytest.mark.parametrize(
    "url",
    [
        "file:///temporary/image.png",
        "qrc:/images/fallback.png",
        "data:image/png;base64,eA==",
    ],
)
def test_local_urls_do_not_enter_network_reload_policy(tmp_path, url):
    controller, _cache = _controller(tmp_path)

    assert controller.report_decode_failure(url) is False
    assert controller.prepare_manual_retry(url).ok
    assert controller.take_always_network(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "file:///temporary/image.png",
        "qrc:/images/fallback.png",
        "data:image/png;base64,eA==",
    ],
)
def test_clear_generation_does_not_stop_local_decoder_callbacks(tmp_path, url):
    controller, _cache = _controller(tmp_path)
    old_generation = controller.generation

    assert controller.decode_failure_action(
        url,
        request_generation=old_generation,
    ) == "failed"
    assert controller.decode_success_action(
        url,
        request_generation=old_generation,
    ) == "accepted"

    assert controller.clearCache()
    assert controller.decode_failure_action(
        url,
        request_generation=old_generation,
    ) == "failed"
    assert controller.decode_success_action(
        url,
        request_generation=old_generation,
    ) == "accepted"


def test_clear_aborts_managers_before_clearing_and_invalidates_generation(
    tmp_path,
):
    controller, cache = _controller(tmp_path)
    events: list[str] = []

    class Manager:
        def active_remote_cache_keys(self) -> tuple[bytes, ...]:
            return (cache_key("https://example.invalid/active.png"),)

        def abort_remote_active(self) -> None:
            events.append("abort")

    class OrderedCache(FakeDiskCache):
        pass

    manager = Manager()
    controller._managers.add(manager)
    cache.entries.add(cache_key("https://example.invalid/image.png"))
    old_generation = controller.generation
    original_clear = cache.clear

    def clear() -> None:
        events.append("clear")
        original_clear()

    cache.clear = clear

    assert controller.clearCache() is True
    assert events == ["abort", "clear"]
    assert controller.generation == old_generation + 1
    assert not controller.response_generation_is_current(old_generation)
    assert cache.entries == set()
    assert not controller.report_decode_failure(
        "https://example.invalid/active.png"
    )
    assert controller.decode_failure_action(
        "https://example.invalid/active.png"
    ) == "stopped"
    assert controller.decode_success_action(
        "https://example.invalid/active.png"
    ) == "stopped"
    assert controller.prepare_manual_retry(
        "https://example.invalid/active.png"
    ).ok
    assert controller.take_always_network(
        "https://example.invalid/active.png"
    )


def test_shutdown_is_idempotent_and_blocks_new_reload(tmp_path):
    controller, _cache = _controller(tmp_path)

    controller.shutdown()
    controller.shutdown()

    assert controller.closed is True
    assert controller.prepare_manual_retry(
        "https://example.invalid/image.png"
    ).ok is True
    assert controller.take_always_network(
        "https://example.invalid/image.png"
    ) is False
    assert controller.decode_failure_action(
        "https://example.invalid/image.png"
    ) == "ignored"
    assert controller.decode_success_action(
        "https://example.invalid/image.png"
    ) == "ignored"


def test_late_decode_from_old_clear_generation_cannot_remove_new_cache_entry(
    tmp_path,
):
    controller, cache = _controller(tmp_path)
    url = "https://example.invalid/shared.png"
    old_generation = controller.generation
    assert controller.clearCache()
    assert controller.prepare_manual_retry(url).ok
    cache.entries.add(cache_key(url))

    assert controller.decode_failure_action(
        url,
        request_generation=old_generation,
    ) == "stopped"
    assert controller.decode_success_action(
        url,
        request_generation=old_generation,
    ) == "stopped"
    assert cache_key(url) in cache.entries


def test_late_reply_from_old_generation_cannot_change_new_cache_state(
    tmp_path,
    image_server,
):
    server = image_server(delay=0.2)
    controller, cache = _controller(tmp_path)
    parent = QObject()
    manager = ImageNetworkAccessManager(controller, parent=parent)
    finished_signals: list[None] = []
    controller.requestFinished.connect(lambda: finished_signals.append(None))
    reply = manager.get(QNetworkRequest(QUrl(server.url)))
    assert _wait_until(lambda: len(server.paths) == 1)

    assert controller.clearCache()
    replacement_final = "https://new.invalid/replacement.png"
    controller.remember_final_url(QUrl(server.url), QUrl(replacement_final))
    cache.entries.add(cache_key(server.url))

    assert _wait_until(reply.isFinished)
    assert cache_key(server.url) in cache.entries
    assert controller.finalUrlFor(server.url) == replacement_final
    assert finished_signals == []
    assert controller._usage_refresh_pending is False
    _dispose(reply, manager, parent, controller)


def test_clear_keeps_active_file_qrc_and_data_replies_running(tmp_path):
    local_file = tmp_path / "local.png"
    local_file.write_bytes(PNG_BYTES)
    urls = (
        QUrl.fromLocalFile(str(local_file)),
        QUrl("qrc:/missing/cache-clear-test.png"),
        QUrl("data:image/png;base64,iVBORw0KGgo="),
    )
    controller = ImageCacheController(tmp_path / "images")
    parent = QObject()
    manager = controller.create_manager(parent)
    old_generation = controller.generation
    replies = tuple(manager.get(QNetworkRequest(url)) for url in urls)
    assert tuple(
        state.original_url.scheme() for state in manager._active.values()
    ) == ("file", "qrc", "data")

    assert controller.clearCache()
    assert controller._automatic_reload_suppressed == set()
    assert _wait_until(lambda: all(reply.isFinished() for reply in replies))
    assert all(
        reply.error() != QNetworkReply.NetworkError.OperationCanceledError
        for reply in replies
    )
    for url in urls:
        assert controller.decode_failure_action(
            url,
            request_generation=old_generation,
        ) == "failed"
        assert controller.decode_success_action(
            url,
            request_generation=old_generation,
        ) == "accepted"

    _dispose(*replies, manager, parent, controller)


def test_cache_limit_validation_and_usage_refresh(tmp_path):
    with pytest.raises(ValueError):
        ImageCacheController(tmp_path / "small", MINIMUM_IMAGE_CACHE_BYTES - 1)
    with pytest.raises(ValueError):
        ImageCacheController(tmp_path / "large", MAXIMUM_IMAGE_CACHE_BYTES + 1)

    controller, cache = _controller(tmp_path)
    assert controller.maximumBytes == DEFAULT_IMAGE_CACHE_BYTES
    assert controller.setMaximumBytes(MINIMUM_IMAGE_CACHE_BYTES)
    assert cache.maximum == MINIMUM_IMAGE_CACHE_BYTES
    assert not controller.setMaximumBytes(MINIMUM_IMAGE_CACHE_BYTES - 1)

    cache.size = 321
    controller.refreshUsage()
    assert controller.currentBytes == 321


def test_failed_cache_limit_update_preserves_the_previous_limit(tmp_path):
    controller, cache = _controller(tmp_path)
    previous = controller.maximumBytes

    def fail(_value: int) -> None:
        raise OSError("read only")

    cache.setMaximumCacheSize = fail

    assert not controller.setMaximumBytes(MINIMUM_IMAGE_CACHE_BYTES)
    assert controller.maximumBytes == previous
    assert "无法更新" in controller.statusText


def test_cache_limit_is_persisted_before_becoming_visible(tmp_path):
    saved: list[int] = []
    created: list[FakeDiskCache] = []

    def build(parent: QObject):
        cache = FakeDiskCache(parent)
        created.append(cache)
        return cache

    controller = ImageCacheController(
        tmp_path / "images",
        disk_cache_factory=build,
        maximum_bytes_saver=saved.append,
    )
    cache = controller._ensure_cache(QObject())
    assert cache is created[0]

    assert controller.setMaximumBytes(MINIMUM_IMAGE_CACHE_BYTES)
    assert saved == [MINIMUM_IMAGE_CACHE_BYTES]
    assert controller.maximumBytes == MINIMUM_IMAGE_CACHE_BYTES
    assert cache.maximum == MINIMUM_IMAGE_CACHE_BYTES


def test_cache_limit_save_failure_restores_runtime_limit(tmp_path):
    previous = DEFAULT_IMAGE_CACHE_BYTES
    created: list[FakeDiskCache] = []

    def build(parent: QObject):
        cache = FakeDiskCache(parent)
        created.append(cache)
        return cache

    def fail_save(_value: int) -> None:
        raise OSError("settings are read only")

    controller = ImageCacheController(
        tmp_path / "images",
        disk_cache_factory=build,
        maximum_bytes_saver=fail_save,
    )
    cache = controller._ensure_cache(QObject())
    assert cache is created[0]

    assert not controller.setMaximumBytes(MINIMUM_IMAGE_CACHE_BYTES)
    assert controller.maximumBytes == previous
    assert cache.maximum == previous
    assert "无法保存" in controller.statusText


def test_cache_clear_failure_is_non_blocking(tmp_path):
    controller, cache = _controller(tmp_path)

    def fail() -> None:
        raise OSError("read only")

    cache.clear = fail

    assert controller.clearCache() is False
    assert "无法清理" in controller.statusText


def test_unusable_cache_directory_is_non_blocking(tmp_path):
    blocking_file = tmp_path / "blocking-file"
    blocking_file.write_text("x", encoding="utf-8")
    controller = ImageCacheController(blocking_file / "images")

    assert controller._ensure_cache(QObject()) is None
    assert controller.available is False
    assert "不可用" in controller.statusText


def test_qml_factory_creates_a_dedicated_cached_manager(tmp_path):
    controller = ImageCacheController(tmp_path / "images")
    factory = ImageNetworkAccessManagerFactory(controller)
    parent = QObject()

    manager = factory.create(parent)

    assert isinstance(manager, ImageNetworkAccessManager)
    assert manager.cache() is controller.cache
    assert controller.cache is not None
    assert Path(controller.cache.cacheDirectory()) == tmp_path / "images"
    assert controller.cache.maximumCacheSize() == DEFAULT_IMAGE_CACHE_BYTES
    _dispose(parent, controller)


def test_real_disk_cache_is_reused_by_a_new_manager(tmp_path, image_server):
    server = image_server()
    cache_directory = tmp_path / "images"
    first_controller = ImageCacheController(cache_directory)
    first_parent = QObject()
    first_manager = first_controller.create_manager(first_parent)

    first_reply, first_body = _fetch(first_manager, server.url + "?size=large#one")
    assert first_reply.error() == QNetworkReply.NetworkError.NoError
    request = first_reply.request()
    assert bytes(request.rawHeader("User-Agent")) == USER_AGENT.encode("ascii")
    assert request.attribute(QNetworkRequest.Attribute.Http2AllowedAttribute) is False
    assert request.transferTimeout() == DEFAULT_TIMEOUT_MS
    assert first_body == PNG_BYTES
    assert server.paths == ["/image.png?size=large"]
    first_controller.refreshUsage()
    assert first_controller.currentBytes > 0
    _dispose(first_reply, first_parent, first_controller)

    second_controller = ImageCacheController(cache_directory)
    second_parent = QObject()
    second_manager = second_controller.create_manager(second_parent)
    second_reply, second_body = _fetch(
        second_manager,
        server.url + "?size=large#different-fragment",
    )

    assert second_body == PNG_BYTES
    assert second_reply.attribute(
        QNetworkRequest.Attribute.SourceIsFromCacheAttribute
    ) is True
    assert server.paths == ["/image.png?size=large"]
    _dispose(second_reply, second_parent, second_controller)


def test_image_diagnostic_follows_source_generation_and_retry(tmp_path, image_server):
    server = image_server(status=404, redirect=True)
    controller = ImageCacheController(tmp_path / "images")
    parent = QObject()
    manager = controller.create_manager(parent)
    first_reply, _ = _fetch(manager, server.url)
    try:
        generation = controller.cacheGeneration
        assert controller.imageFailureText(server.url, generation) == "图片服务器返回 HTTP 404"
        assert controller.imageFailureText("https://example.invalid/other.jpg", generation) == ""
        assert controller.prepareManualRetry(server.url, server.final_url)
        assert controller.imageFailureText(server.url, generation) == ""
        second_reply, _ = _fetch(manager, server.url)
        assert controller.imageFailureText(server.url, generation) == "图片服务器返回 HTTP 404"
        assert controller.clearCache()
        assert controller.imageFailureText(server.url, generation) == ""
        assert controller.imageFailureText(server.url, controller.cacheGeneration) == ""
        second_reply.deleteLater()
    finally:
        _dispose(first_reply, parent, controller)


@pytest.mark.parametrize(
    "server_options",
    [
        {"status": 206},
        {"content_type": "text/html"},
        {"cache_control": "no-store"},
        {"body": b""},
    ],
)
def test_disallowed_real_responses_are_not_reused(
    tmp_path,
    image_server,
    server_options,
):
    server = image_server(**server_options)
    controller = ImageCacheController(tmp_path / "images")
    parent = QObject()
    manager = controller.create_manager(parent)

    first_reply, _first_body = _fetch(manager, server.url)
    second_reply, _second_body = _fetch(manager, server.url)

    assert len(server.paths) == 2
    assert second_reply.attribute(
        QNetworkRequest.Attribute.SourceIsFromCacheAttribute
    ) is not True
    _dispose(first_reply, second_reply, parent, controller)


def test_manual_retry_bypasses_an_existing_real_cache_once(
    tmp_path,
    image_server,
):
    server = image_server()
    controller = ImageCacheController(tmp_path / "images")
    parent = QObject()
    manager = controller.create_manager(parent)

    first_reply, _ = _fetch(manager, server.url)
    cached_reply, _ = _fetch(manager, server.url)
    assert cached_reply.attribute(
        QNetworkRequest.Attribute.SourceIsFromCacheAttribute
    ) is True
    assert len(server.paths) == 1

    assert controller.prepare_manual_retry(server.url).ok
    network_reply, _ = _fetch(manager, server.url)
    assert network_reply.attribute(
        QNetworkRequest.Attribute.SourceIsFromCacheAttribute
    ) is not True
    assert len(server.paths) == 2

    cached_again, _ = _fetch(manager, server.url)
    assert cached_again.attribute(
        QNetworkRequest.Attribute.SourceIsFromCacheAttribute
    ) is True
    assert len(server.paths) == 2
    _dispose(
        first_reply,
        cached_reply,
        network_reply,
        cached_again,
        parent,
        controller,
    )


def test_redirect_keeps_only_the_final_complete_image_entry(
    tmp_path,
    image_server,
):
    server = image_server(redirect=True)
    controller = ImageCacheController(tmp_path / "images")
    parent = QObject()
    manager = controller.create_manager(parent)

    reply, body = _fetch(manager, server.url)

    assert body == PNG_BYTES
    assert reply.url() == QUrl(server.final_url)
    assert controller.cache is not None
    assert controller.cache.metaData(QUrl(server.url)).isValid() is False
    assert controller.cache.metaData(QUrl(server.final_url)).isValid() is True
    assert controller.finalUrlFor(server.url) == server.final_url
    _dispose(reply, parent, controller)


def test_qt_disk_cache_enforces_its_configured_eviction_limit(
    tmp_path,
    image_server,
):
    server = image_server(body=b"x" * 1024)
    controller = ImageCacheController(tmp_path / "images")
    parent = QObject()
    manager = controller.create_manager(parent)
    assert controller.cache is not None
    controller.cache.setMaximumCacheSize(8 * 1024)
    replies: list[QNetworkReply] = []

    for index in range(16):
        reply, body = _fetch(manager, f"{server.url}?item={index}")
        replies.append(reply)
        assert body == b"x" * 1024

    assert controller.cache.cacheSize() <= 8 * 1024
    assert controller.cache.metaData(QUrl(f"{server.url}?item=15")).isValid()
    assert not controller.cache.metaData(QUrl(f"{server.url}?item=0")).isValid()
    _dispose(*replies, parent, controller)


@pytest.mark.parametrize("close", [False, True], ids=["clear", "shutdown"])
def test_clear_and_shutdown_abort_active_replies_without_late_cache_write(
    tmp_path,
    image_server,
    close,
):
    server = image_server(delay=0.2)
    controller = ImageCacheController(tmp_path / "images")
    parent = QObject()
    manager = controller.create_manager(parent)
    finished_signals: list[None] = []
    controller.requestFinished.connect(lambda: finished_signals.append(None))
    reply = manager.get(QNetworkRequest(QUrl(server.url)))
    assert _wait_until(lambda: len(server.paths) == 1)

    if close:
        controller.shutdown()
    else:
        assert controller.clearCache()

    assert _wait_until(reply.isFinished)
    assert reply.error() == QNetworkReply.NetworkError.OperationCanceledError
    time.sleep(0.25)
    APPLICATION.processEvents()
    controller.refreshUsage()
    assert controller.currentBytes == 0
    assert controller.cache is not None
    assert controller.cache.metaData(QUrl(server.url)).isValid() is False
    assert finished_signals == []

    if close:
        late_reply = manager.get(QNetworkRequest(QUrl(server.url + "?late=1")))
        assert _wait_until(late_reply.isFinished)
        assert late_reply.error() == QNetworkReply.NetworkError.OperationCanceledError
        assert finished_signals == []
        _dispose(late_reply)
    _dispose(reply, parent, controller)


def test_qml_thumbnail_decode_failure_retries_once_and_manual_retry_is_local(
    image_server,
):
    first_server = image_server(body=b"broken first image")
    second_server = image_server(status=403, body=b"Forbidden")
    engine, gallery = create_qml_engine(
        APPLICATION,
        danbooru_base_url="https://example.invalid",
    )
    gallery.postsModel.set_posts(
        (
            DanbooruPost(
                id=1,
                preview_url=first_server.url,
                large_file_url=None,
                file_url=None,
                score=0,
                fav_count=0,
                rating="g",
                width=100,
                height=100,
                file_ext="jpg",
            ),
            DanbooruPost(
                id=2,
                preview_url=second_server.url,
                large_file_url=None,
                file_url=None,
                score=0,
                fav_count=0,
                rating="g",
                width=100,
                height=100,
                file_ext="jpg",
            ),
        )
    )
    root = engine.rootObjects()[0]
    try:
        assert _wait_until(
            lambda: len(first_server.paths) == 2
            and len(second_server.paths) == 2
        )
        gallery_grid = root.findChild(QObject, "galleryGrid")
        gallery_grid.setProperty("currentIndex", 0)
        APPLICATION.processEvents()
        first_card = gallery_grid.property("currentItem")
        gallery_grid.setProperty("currentIndex", 1)
        APPLICATION.processEvents()
        second_card = gallery_grid.property("currentItem")
        first_error = first_card.findChild(QObject, "thumbnailError_1")
        second_error = second_card.findChild(QObject, "thumbnailError_2")
        first_retry = first_card.findChild(QObject, "thumbnailRetry_1")
        first_image = first_card.findChild(QObject, "thumbnail_1")
        assert first_error is not None
        assert second_error is not None
        assert _wait_until(
            lambda: first_error.property("visible") is True
            and second_error.property("visible") is True
        )
        assert first_retry is not None
        assert first_card.property("thumbnailFailureReason") == "无法读取或解码缩略图"
        assert second_card.property("thumbnailFailureReason") == "图片服务器返回 HTTP 403"
        assert first_image is not None and first_image.property("cache") is False

        first_retry.clicked.emit()

        assert _wait_until(lambda: len(first_server.paths) == 3)
        time.sleep(0.1)
        APPLICATION.processEvents()
        assert len(second_server.paths) == 2
        assert second_error.property("visible") is True
    finally:
        APPLICATION.aboutToQuit.emit()
        for window in engine.rootObjects():
            window.close()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APPLICATION.processEvents()


def test_qml_detail_decode_failure_retries_once_before_fallback(image_server):
    server = image_server(body=b"broken detail image")
    engine, gallery = create_qml_engine(
        APPLICATION,
        danbooru_base_url="https://example.invalid",
    )
    gallery.postsModel.set_posts(
        (
            DanbooruPost(
                id=3,
                preview_url=(
                    "data:image/png;base64,"
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
                    "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                ),
                large_file_url=server.url,
                file_url=None,
                score=0,
                fav_count=0,
                rating="g",
                width=100,
                height=100,
                file_ext="jpg",
            ),
        )
    )
    detail = engine.rootContext().contextProperty("detailController")
    navigation = engine.rootContext().contextProperty("detailNavigation")
    try:
        assert navigation.openBrowsePost(0, 0.0, 0, 800.0, 600.0, 1280.0, 800.0)
        assert _wait_until(
            lambda: len(server.paths) == 2 and detail.mediaState == "success"
        )
        assert detail.actualQualityText == "预览图"
    finally:
        APPLICATION.aboutToQuit.emit()
        for window in engine.rootObjects():
            window.close()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APPLICATION.processEvents()
