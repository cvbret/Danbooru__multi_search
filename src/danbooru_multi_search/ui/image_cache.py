"""Qt Quick image networking with bounded, removable disk caching."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from weakref import WeakSet
from contextlib import nullcontext

from PySide6.QtCore import QIODevice, QObject, Property, QTimer, QUrl, Signal, Slot
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkDiskCache,
    QNetworkReply,
    QNetworkRequest,
)
from PySide6.QtQml import QQmlNetworkAccessManagerFactory
from danbooru_multi_search.ui.protected_image_cache import ProtectedImageDiskCache, ProtectionProvider


MIB = 1024 * 1024
MINIMUM_IMAGE_CACHE_BYTES = 64 * MIB
DEFAULT_IMAGE_CACHE_BYTES = 512 * MIB
MAXIMUM_IMAGE_CACHE_BYTES = 16 * 1024 * MIB

# PySide exposes QUrl.adjusted() with ComponentFormattingOption even though
# NormalizePathSegments belongs to QUrl::UrlFormattingOption in Qt's C++ API.
_NORMALIZE_PATH_SEGMENTS = QUrl.ComponentFormattingOption(0x1000)


def normalized_cache_url(value: QUrl | str) -> QUrl:
    """Return Qt's normalized cache key while retaining the query string."""

    url = QUrl(value) if isinstance(value, str) else QUrl(value)
    url = url.adjusted(_NORMALIZE_PATH_SEGMENTS)
    url.setFragment(None)
    return url


def cache_key(value: QUrl | str) -> bytes:
    """Return a stable byte key for retry and generation bookkeeping."""

    return bytes(
        normalized_cache_url(value).toEncoded(
            QUrl.ComponentFormattingOption.FullyEncoded
        )
    )


def _is_remote_cache_url(value: QUrl | str) -> bool:
    return normalized_cache_url(value).scheme().casefold() in {"http", "https"}


def is_complete_image_response(
    *,
    operation: QNetworkAccessManager.Operation,
    url: QUrl | str,
    network_error: QNetworkReply.NetworkError,
    http_status: object,
    content_type: object,
    received_bytes: int,
    cache_control: bytes | bytearray | str = b"",
    cache_save_allowed: bool = True,
) -> bool:
    """Decide whether a finished response may remain in the image cache."""

    normalized = normalized_cache_url(url)
    if operation != QNetworkAccessManager.Operation.GetOperation:
        return False
    if normalized.scheme().casefold() not in {"http", "https"}:
        return False
    if network_error != QNetworkReply.NetworkError.NoError:
        return False
    if isinstance(http_status, bool):
        return False
    try:
        status = int(http_status)
    except (TypeError, ValueError, OverflowError):
        return False
    if status != 200 or received_bytes <= 0 or not cache_save_allowed:
        return False

    if isinstance(content_type, bytes):
        media_type = content_type.decode("latin-1", errors="replace")
    elif hasattr(content_type, "data"):
        media_type = bytes(content_type).decode("latin-1", errors="replace")
    else:
        media_type = str(content_type or "")
    if not media_type.split(";", 1)[0].strip().casefold().startswith("image/"):
        return False

    if isinstance(cache_control, str):
        directives = cache_control
    else:
        directives = bytes(cache_control).decode("latin-1", errors="replace")
    return "no-store" not in {
        directive.strip().split("=", 1)[0].casefold()
        for directive in directives.split(",")
    }


@dataclass(frozen=True, slots=True)
class CacheActionResult:
    """Non-throwing result returned by user-visible cache operations."""

    ok: bool
    message: str = ""
    removed_any: bool = False


@dataclass(slots=True)
class _ReplyState:
    original_url: QUrl
    operation: QNetworkAccessManager.Operation
    generation: int
    received_bytes: int = 0
    handled: bool = False


DiskCacheFactory = Callable[[QObject], QNetworkDiskCache]
MaximumBytesSaver = Callable[[int], object]


class ImageCacheController(QObject):
    """Own image cache policy independently from metadata networking."""

    usageChanged = Signal()
    maximumBytesChanged = Signal()
    availabilityChanged = Signal()
    statusChanged = Signal()
    cacheCleared = Signal(bool, str)
    requestFinished = Signal()
    generationChanged = Signal()

    def __init__(
        self,
        cache_directory: Path,
        maximum_bytes: int = DEFAULT_IMAGE_CACHE_BYTES,
        *,
        disk_cache_factory: DiskCacheFactory | None = None,
        maximum_bytes_saver: MaximumBytesSaver | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if not MINIMUM_IMAGE_CACHE_BYTES <= maximum_bytes <= MAXIMUM_IMAGE_CACHE_BYTES:
            raise ValueError(
                "image cache limit must be between 64 MiB and 16 GiB"
            )
        self._cache_directory = cache_directory
        self._maximum_bytes = maximum_bytes
        self._disk_cache_factory = disk_cache_factory
        self._protection_provider: ProtectionProvider = lambda: nullcontext(frozenset())
        self._redirect_observer: Callable[[str, str], None] | None = None
        self._cache_store_allowed: Callable[[], bool] = lambda: True
        self._maximum_bytes_saver = maximum_bytes_saver
        self._cache: QNetworkDiskCache | None = None
        self._managers: WeakSet[ImageNetworkAccessManager] = WeakSet()
        self._current_bytes = 0
        self._status_text = ""
        self._generation = 0
        self._closed = False
        self._clearing = False
        self._always_network: set[bytes] = set()
        self._automatic_reload_attempted: set[bytes] = set()
        self._automatic_reload_suppressed: set[bytes] = set()
        self._known_final_urls: dict[bytes, QUrl] = {}
        self._usage_refresh_pending = False
        self._usage_timer = QTimer(self)
        self._usage_timer.setSingleShot(True)
        self._usage_timer.setInterval(100)
        self._usage_timer.timeout.connect(self.refreshUsage)

    @Property("qlonglong", notify=usageChanged)
    def currentBytes(self) -> int:
        return self._current_bytes

    @Property("qlonglong", notify=maximumBytesChanged)
    def maximumBytes(self) -> int:
        return self._maximum_bytes

    @Property(str, notify=statusChanged)
    def statusText(self) -> str:
        return self._status_text

    @Property(bool, notify=availabilityChanged)
    def available(self) -> bool:
        return self._cache is not None

    @property
    def generation(self) -> int:
        return self._generation

    @Property(int, notify=generationChanged)
    def cacheGeneration(self) -> int:
        return self._generation

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def cache(self) -> QNetworkDiskCache | None:
        return self._cache

    def _set_status(self, message: str) -> None:
        if self._status_text == message:
            return
        self._status_text = message
        self.statusChanged.emit()

    def configure_protection(self, provider: ProtectionProvider, redirect_observer: Callable[[str, str], None],
                             can_store: Callable[[], bool] = lambda: True) -> None:
        """Install loaded protection before Qt can inspect directory capacity."""
        if self._cache is not None:
            raise RuntimeError("Image protection must be installed before cache initialization")
        self._protection_provider, self._redirect_observer = provider, redirect_observer
        self._cache_store_allowed = can_store

    def _ensure_cache(self, parent: QObject | None = None) -> QNetworkDiskCache | None:
        if self._cache is not None or self._closed:
            return self._cache
        if self._cache_store_allowed():
            self._set_status("")
        try:
            self._cache_directory.mkdir(parents=True, exist_ok=True)
            if not self._cache_directory.is_dir():
                raise OSError("image cache path is not a directory")
            cache = self._disk_cache_factory(parent or self) if self._disk_cache_factory else ProtectedImageDiskCache(
                parent or self, key=cache_key, protection=self._protection_provider, status=self._set_status, can_store=self._cache_store_allowed)
            cache.setCacheDirectory(str(self._cache_directory))
            cache.setMaximumCacheSize(self._maximum_bytes)
        except (OSError, RuntimeError, ValueError) as exc:
            self._set_status(f"图片缓存不可用：{exc}")
            return None
        self._cache = cache
        self.availabilityChanged.emit()
        return cache

    def create_manager(self, parent: QObject) -> "ImageNetworkAccessManager":
        """Create the manager requested by a QML engine's factory."""

        manager = ImageNetworkAccessManager(self, parent=parent)
        cache = self._ensure_cache(manager)
        if cache is not None:
            manager.setCache(cache)
        self._managers.add(manager)
        return manager

    def _forget_manager(self, manager: "ImageNetworkAccessManager") -> None:
        self._managers.discard(manager)

    def take_always_network(self, value: QUrl | str) -> bool:
        key = cache_key(value)
        if self._closed or key not in self._always_network:
            return False
        self._always_network.remove(key)
        return True

    def known_final_url(self, value: QUrl | str) -> QUrl | None:
        final = self._known_final_urls.get(cache_key(value))
        return QUrl(final) if final is not None else None

    @Slot(str, result=str)
    def finalUrlFor(self, value: str) -> str:
        final = self.known_final_url(value)
        return final.toString() if final is not None else ""

    def remember_final_url(self, original: QUrl, final: QUrl) -> None:
        self._known_final_urls[cache_key(original)] = normalized_cache_url(final)
        if self._redirect_observer is not None:
            self._redirect_observer(cache_key(original).decode("ascii"), cache_key(final).decode("ascii"))

    def remove_entries(
        self,
        original: QUrl | str,
        final: QUrl | str | None = None,
    ) -> CacheActionResult:
        """Remove original and final cache keys; absent entries are successful."""

        cache = self._cache
        if cache is None:
            return CacheActionResult(True)
        urls = [normalized_cache_url(original)]
        known_final = final or self.known_final_url(original)
        if known_final is not None:
            normalized_final = normalized_cache_url(known_final)
            if cache_key(normalized_final) != cache_key(urls[0]):
                urls.append(normalized_final)
        removed_any = False
        try:
            for url in urls:
                removed_any = cache.remove(url) or removed_any
        except (OSError, RuntimeError) as exc:
            message = f"图片缓存项无法删除：{exc}"
            self._set_status(message)
            return CacheActionResult(False, message, removed_any)
        return CacheActionResult(True, removed_any=removed_any)

    def prepare_manual_retry(
        self,
        original: QUrl | str,
        final: QUrl | str | None = None,
    ) -> CacheActionResult:
        """Delete stale entries and make the next request bypass the cache once."""

        result = self.remove_entries(original, final)
        if not self._closed and _is_remote_cache_url(original):
            self._automatic_reload_suppressed.discard(cache_key(original))
            self._always_network.add(cache_key(original))
        return result

    @Slot(str, str, result=bool)
    def prepareManualRetry(self, original: str, final: str) -> bool:
        return self.prepare_manual_retry(original, final or None).ok

    def report_decode_failure(
        self,
        original: QUrl | str,
        final: QUrl | str | None = None,
    ) -> bool:
        """Return true exactly once when an automatic network reload is allowed."""

        return self.decode_failure_action(original, final) == "retry"

    def decode_failure_action(
        self,
        original: QUrl | str,
        final: QUrl | str | None = None,
        request_generation: int | None = None,
    ) -> str:
        """Classify a decoder error without reviving cleared or closed requests."""

        key = cache_key(original)
        if self._closed:
            return "ignored"
        if (
            request_generation is not None
            and request_generation != self._generation
            and _is_remote_cache_url(original)
        ):
            return "stopped"
        self.remove_entries(original, final)
        if key in self._automatic_reload_suppressed:
            return "stopped"
        if (
            not _is_remote_cache_url(original)
            or key in self._automatic_reload_attempted
        ):
            return "failed"
        self._automatic_reload_attempted.add(key)
        self._always_network.add(key)
        return "retry"

    @Slot(str, str, result=str)
    def decodeFailureAction(self, original: str, final: str) -> str:
        return self.decode_failure_action(original, final or None)

    @Slot(str, str, int, result=str)
    def decodeFailureActionForGeneration(
        self,
        original: str,
        final: str,
        request_generation: int,
    ) -> str:
        return self.decode_failure_action(
            original,
            final or None,
            request_generation,
        )

    @Slot(str, str, result=bool)
    def reportDecodeFailure(self, original: str, final: str) -> bool:
        return self.report_decode_failure(original, final or None)

    def report_decode_success(self, original: QUrl | str) -> None:
        key = cache_key(original)
        self._automatic_reload_attempted.discard(key)
        self._automatic_reload_suppressed.discard(key)

    def decode_success_action(
        self,
        original: QUrl | str,
        request_generation: int | None = None,
    ) -> str:
        key = cache_key(original)
        if self._closed:
            return "ignored"
        if (
            request_generation is not None
            and request_generation != self._generation
            and _is_remote_cache_url(original)
        ):
            return "stopped"
        if key in self._automatic_reload_suppressed:
            return "stopped"
        self.report_decode_success(original)
        return "accepted"

    @Slot(str, result=str)
    def decodeSuccessAction(self, original: str) -> str:
        return self.decode_success_action(original)

    @Slot(str, int, result=str)
    def decodeSuccessActionForGeneration(
        self,
        original: str,
        request_generation: int,
    ) -> str:
        return self.decode_success_action(original, request_generation)

    @Slot(str)
    def reportDecodeSuccess(self, original: str) -> None:
        self.report_decode_success(original)

    @Slot("qlonglong", result=bool)
    def setMaximumBytes(self, maximum_bytes: int) -> bool:
        if not MINIMUM_IMAGE_CACHE_BYTES <= maximum_bytes <= MAXIMUM_IMAGE_CACHE_BYTES:
            self._set_status("图片缓存上限必须在 64 MiB 至 16 GiB 之间")
            return False
        if self._closed:
            return False
        previous_bytes = self._maximum_bytes
        if maximum_bytes == previous_bytes:
            return True
        if self._cache_store_allowed():
            self._set_status("")
        if self._cache is not None:
            try:
                self._cache.setMaximumCacheSize(maximum_bytes)
            except (OSError, RuntimeError) as exc:
                self._set_status(f"图片缓存上限无法更新：{exc}")
                return False
        if self._maximum_bytes_saver is not None:
            try:
                self._maximum_bytes_saver(maximum_bytes)
            except Exception as exc:
                rollback_error = ""
                if self._cache is not None:
                    try:
                        self._cache.setMaximumCacheSize(previous_bytes)
                    except (OSError, RuntimeError) as rollback_exc:
                        rollback_error = f"；运行中上限回复失败：{rollback_exc}"
                self._set_status(f"图片缓存上限无法保存：{exc}{rollback_error}")
                return False
        self._maximum_bytes = maximum_bytes
        self.maximumBytesChanged.emit()
        self.schedule_usage_refresh()
        return True

    @Slot()
    def refreshUsage(self) -> None:
        self._usage_refresh_pending = False
        size = 0
        cache = self._cache or self._ensure_cache()
        if cache is not None:
            try:
                size = max(0, int(cache.cacheSize()))
            except (OSError, RuntimeError, TypeError, ValueError):
                size = 0
        if size != self._current_bytes:
            self._current_bytes = size
            self.usageChanged.emit()

    def schedule_usage_refresh(self) -> None:
        if self._closed or self._usage_refresh_pending:
            return
        self._usage_refresh_pending = True
        self._usage_timer.start()

    @Slot(result=bool)
    def clearCache(self) -> bool:
        """Abort active image replies before clearing the disk cache."""

        if self._closed:
            return False
        self._generation += 1
        self.generationChanged.emit()
        self._clearing = True
        for manager in tuple(self._managers):
            self._automatic_reload_suppressed.update(
                manager.active_remote_cache_keys()
            )
            manager.abort_remote_active()
        ok = True
        message = ""
        cache = self._cache or self._ensure_cache()
        if cache is not None:
            try:
                cache.clear()
                if isinstance(cache, ProtectedImageDiskCache) and not cache.maintenance_ok:
                    ok = False
                    message = cache.last_message or self._status_text or "收藏图片保护暂不可用，已保留现有缓存。"
            except (OSError, RuntimeError) as exc:
                ok = False
                message = f"图片缓存无法清理：{exc}"
                self._set_status(message)
        self._always_network.clear()
        self._automatic_reload_attempted.clear()
        self._known_final_urls.clear()
        self._clearing = False
        if self._closed:
            return False
        self.refreshUsage()
        self.cacheCleared.emit(ok, message)
        return ok

    def response_generation_is_current(self, generation: int) -> bool:
        return not self._closed and not self._clearing and generation == self._generation

    @Slot()
    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        self.generationChanged.emit()
        self._usage_timer.stop()
        self._usage_refresh_pending = False
        self._always_network.clear()
        self._automatic_reload_attempted.clear()
        self._automatic_reload_suppressed.clear()
        self._known_final_urls.clear()
        for manager in tuple(self._managers):
            manager.abort_active()


class ImageNetworkAccessManager(QNetworkAccessManager):
    """A QML-only manager that validates every disk-cached response."""

    def __init__(
        self,
        controller: ImageCacheController,
        *,
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._active: dict[QNetworkReply, _ReplyState] = {}
        self.destroyed.connect(lambda: controller._forget_manager(self))

    def createRequest(
        self,
        operation: QNetworkAccessManager.Operation,
        request: QNetworkRequest,
        outgoing_data: QIODevice | None = None,
    ) -> QNetworkReply:
        prepared = QNetworkRequest(request)
        original_url = normalized_cache_url(prepared.url())
        if self._controller.closed:
            prepared.setAttribute(
                QNetworkRequest.Attribute.CacheLoadControlAttribute,
                QNetworkRequest.CacheLoadControl.AlwaysNetwork,
            )
            prepared.setAttribute(
                QNetworkRequest.Attribute.CacheSaveControlAttribute,
                False,
            )
        elif (
            operation == QNetworkAccessManager.Operation.GetOperation
            and self._controller.take_always_network(original_url)
        ):
            prepared.setAttribute(
                QNetworkRequest.Attribute.CacheLoadControlAttribute,
                QNetworkRequest.CacheLoadControl.AlwaysNetwork,
            )
        reply = super().createRequest(operation, prepared, outgoing_data)
        state = _ReplyState(
            original_url=original_url,
            operation=operation,
            generation=self._controller.generation,
        )
        self._active[reply] = state
        reply.downloadProgress.connect(
            lambda received, _total, tracked=reply: self._record_progress(
                tracked,
                received,
            )
        )
        reply.finished.connect(lambda tracked=reply: self._finish_reply(tracked))
        reply.redirected.connect(lambda target, tracked=reply: self._record_redirect(tracked, target))
        reply.metaDataChanged.connect(lambda tracked=reply: self._record_redirect(tracked, tracked.url()))
        if self._controller.closed:
            reply.abort()
            if reply.isFinished():
                QTimer.singleShot(
                    0,
                    self,
                    lambda tracked=reply: self._finish_reply(tracked),
                )
        elif reply.isFinished():
            QTimer.singleShot(
                0,
                self,
                lambda tracked=reply: self._finish_reply(tracked),
            )
        return reply

    def _record_redirect(self, reply: QNetworkReply, target: QUrl) -> None:
        state = self._active.get(reply)
        if state is None or not self._controller.response_generation_is_current(state.generation):
            return
        final = reply.url().resolved(target)
        if _is_remote_cache_url(final) and cache_key(final) != cache_key(state.original_url):
            self._controller.remember_final_url(state.original_url, final)

    def _record_progress(self, reply: QNetworkReply, received: int) -> None:
        state = self._active.get(reply)
        if state is not None:
            state.received_bytes = max(state.received_bytes, int(received))

    def _finish_reply(self, reply: QNetworkReply) -> None:
        state = self._active.pop(reply, None)
        if state is None or state.handled:
            return
        state.handled = True
        if not self._controller.response_generation_is_current(state.generation):
            return
        final_url = normalized_cache_url(reply.url())
        self._controller.remember_final_url(state.original_url, final_url)
        content_length = reply.header(QNetworkRequest.KnownHeaders.ContentLengthHeader)
        try:
            received_bytes = max(state.received_bytes, int(content_length or 0))
        except (TypeError, ValueError, OverflowError):
            received_bytes = state.received_bytes
        save_attribute = reply.request().attribute(
            QNetworkRequest.Attribute.CacheSaveControlAttribute
        )
        cache_save_allowed = save_attribute is not False
        cacheable = is_complete_image_response(
            operation=state.operation,
            url=final_url,
            network_error=reply.error(),
            http_status=reply.attribute(
                QNetworkRequest.Attribute.HttpStatusCodeAttribute
            ),
            content_type=reply.header(
                QNetworkRequest.KnownHeaders.ContentTypeHeader
            ),
            received_bytes=received_bytes,
            cache_control=bytes(reply.rawHeader("Cache-Control")),
            cache_save_allowed=cache_save_allowed,
        )
        if not cacheable:
            self._controller.remove_entries(state.original_url, final_url)
        elif cache_key(state.original_url) != cache_key(final_url):
            # The original key is a redirect response rather than a complete image.
            self._controller.remove_entries(state.original_url, state.original_url)
        self._controller.schedule_usage_refresh()
        self._controller.requestFinished.emit()

    def abort_active(self) -> None:
        for reply in tuple(self._active):
            if not reply.isFinished():
                reply.abort()

    def abort_remote_active(self) -> None:
        """Abort only HTTP(S) replies affected by a disk-cache clear."""

        for reply, state in tuple(self._active.items()):
            if _is_remote_cache_url(state.original_url) and not reply.isFinished():
                reply.abort()

    def active_remote_cache_keys(self) -> tuple[bytes, ...]:
        """Return active HTTP(S) keys that a disk-cache clear invalidates."""

        return tuple(
            cache_key(state.original_url)
            for state in self._active.values()
            if _is_remote_cache_url(state.original_url)
        )


class ImageNetworkAccessManagerFactory(QQmlNetworkAccessManagerFactory):
    """Factory installed on QQmlApplicationEngine before loading QML."""

    def __init__(self, controller: ImageCacheController) -> None:
        super().__init__()
        self.controller = controller

    def create(self, parent: QObject) -> QNetworkAccessManager:
        return self.controller.create_manager(parent)
