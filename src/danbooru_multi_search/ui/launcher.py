"""Assemble and run the M2 QML application."""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from PySide6.QtCore import QObject, QTimer, QUrl, Slot
from PySide6.QtGui import QGuiApplication, QImageReader
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtNetwork import QNetworkAccessManager, QSslSocket

from danbooru_multi_search.app.settings import SettingsController
from danbooru_multi_search.ui.autocomplete import DanbooruTagSuggestionController
from danbooru_multi_search.ui.query_controller import DanbooruM2QueryController
from danbooru_multi_search.ui.details import DanbooruDetailController
from danbooru_multi_search.core.settings import (
    AppSettings,
)
from danbooru_multi_search.storage.settings import SettingsStore
from danbooru_multi_search.danbooru import (
    DanbooruMultiBranchExecutor,
    TagTranslationStore,
)
from danbooru_multi_search.tasks import (
    BoundedTaskQueue,
    QtScheduler,
    SystemClock,
    TaskQueueConfig,
)
from danbooru_multi_search.storage import Database, HistoryRepository
from danbooru_multi_search.storage.favorites import FavoriteRepository
from danbooru_multi_search.storage.artist_combinations import ArtistCombinationRepository
from danbooru_multi_search.storage.saved_queries import SavedQueryRepository
from danbooru_multi_search.storage.query_checkpoints import QueryCheckpointRepository
from danbooru_multi_search.storage.cache_protection import CacheProtectionRepository
from danbooru_multi_search.storage.downloads import DownloadRepository
from danbooru_multi_search.app.favorites import FavoriteWriteService
from danbooru_multi_search.app.query_checkpoints import QueryCheckpointService
from danbooru_multi_search.app.cache_cleanup import CacheCleanupService
from danbooru_multi_search.app.cache_protection import CacheProtectionService
from danbooru_multi_search.app.write_access import SharedWriteAccess
from danbooru_multi_search.app.download_manager import PersistentDownloadManager
from danbooru_multi_search.app.download_submission import DownloadSubmissionService
from danbooru_multi_search.ui.downloads import DownloadUiController
from danbooru_multi_search.ui.artist_combinations import ArtistCombinationController
from danbooru_multi_search.ui.saved_queries import SavedQueryController
from danbooru_multi_search.ui.query_checkpoints import QueryCheckpointController
from danbooru_multi_search.ui.cache_protection import CacheProtectionController
from danbooru_multi_search.ui.cache_cleanup import CacheCleanupController
from danbooru_multi_search.ui.favorites import FavoriteController
from danbooru_multi_search.ui.favorite_collection import FavoriteCollectionController
from danbooru_multi_search.ui.favorite_transfer import FavoriteTransferController
from danbooru_multi_search.ui.gallery_selection import GallerySelectionController
from danbooru_multi_search.ui.detail_favorites import DetailFavoritesController
from danbooru_multi_search.ui.appearance import AppearanceController
from danbooru_multi_search.ui.detail_navigation import DetailNavigationController
from danbooru_multi_search.danbooru.pools import PoolRequestService
from danbooru_multi_search.danbooru.pool_pages import PoolPageRequestService
from danbooru_multi_search.ui.pools import PoolBrowserController
from danbooru_multi_search.ui.pool_navigation import PoolNavigationController
from danbooru_multi_search.ui.pool_reader import PoolReaderController
from danbooru_multi_search.storage.pool_progress import PoolProgressRepository
from danbooru_multi_search.storage.pool_downloads import PoolDownloadRepository
from danbooru_multi_search.app.pool_downloads import PoolDownloadManager
from danbooru_multi_search.ui.pool_downloads import PoolDownloadController
from danbooru_multi_search.ui.pool_progress import PoolProgressController
from danbooru_multi_search.ui.eagle import EagleController
from danbooru_multi_search.app.eagle_runtime import EagleRuntime
from danbooru_multi_search.app.eagle_service import EagleBackend, EagleBackendFactory
from danbooru_multi_search.integrations.eagle import EagleWebApiBridge
from danbooru_multi_search.integrations.eagle.contracts import EagleBridge
from danbooru_multi_search.platform import create_platform_adapter
from danbooru_multi_search.ui.image_cache import (
    ImageCacheController,
    ImageNetworkAccessManagerFactory,
)
from danbooru_multi_search.ui.query_state import M2QueryBridgeController
from danbooru_multi_search.ui.session_state import BrowserSessionController
from danbooru_multi_search.ui.window_state import ScreenGeometry, WindowGeometry

if TYPE_CHECKING:
    from danbooru_multi_search.app.bootstrap import RuntimeContext


def main_qml_path() -> Path:
    return Path(__file__).resolve().parent / "qml" / "M2Main.qml"


def create_eagle_backend_factory(bridge: EagleBridge) -> EagleBackendFactory:
    """Capture configuration now; create and dispose all Qt I/O in the worker."""
    if not bridge.is_supported or not bridge.availability.available:
        @contextmanager
        def unavailable(cancelled: Callable[[], bool], deadline: float) -> Iterator[EagleBackend]:
            yield EagleBackend(bridge, lambda: False)
        return unavailable
    if not isinstance(bridge, EagleWebApiBridge):
        raise TypeError("A supported Eagle backend requires explicit HTTP configuration.")
    configuration = bridge.configuration

    @contextmanager
    def backend(cancelled: Callable[[], bool], deadline: float) -> Iterator[EagleBackend]:
        from danbooru_multi_search.integrations.eagle.qt_transport import QtEagleHttpTransport
        transport = QtEagleHttpTransport(cancelled, deadline)
        try:
            client = EagleWebApiBridge(
                configuration.base_url,
                transport=transport,
                timeout_seconds=configuration.timeout_seconds,
                maximum_response_bytes=configuration.maximum_response_bytes,
                user_agent=configuration.user_agent,
            )
            yield EagleBackend(client, lambda: transport.sent_post)
        finally:
            transport.close()
    return backend


def configure_controls_style() -> None:
    """Select the shared palette-based style before loading any QML controls."""

    if QQuickStyle.name() != "Basic":
        QQuickStyle.setStyle("Basic")


class _UiSession(QObject):
    """Own the controllers and their one shared task queue for a GUI session."""

    def __init__(
        self,
        application: QGuiApplication,
        danbooru_base_url: str,
        *,
        network_manager: QNetworkAccessManager | None,
        runtime: RuntimeContext | None,
        screen_geometries: Sequence[ScreenGeometry] | None,
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        self._temporary_root: tempfile.TemporaryDirectory[str] | None = None
        if runtime is None:
            self._temporary_root = tempfile.TemporaryDirectory(
                prefix="danbooru-multi-search-ui-"
            )
            temporary_path = Path(self._temporary_root.name)
            database = Database(temporary_path / "application.sqlite3")
            database.initialize()
            settings = AppSettings()
            settings_store = SettingsStore(temporary_path / "settings.json")
            image_cache_directory = temporary_path / "cache" / "images"
        else:
            database = runtime.database
            settings = runtime.settings
            settings_store = SettingsStore(runtime.paths.settings_file)
            image_cache_directory = runtime.paths.cache_dir / "images"
        self.settings_controller = SettingsController(settings_store, settings)
        self.appearance_controller = AppearanceController(
            self.settings_controller,
            parent=self,
        )
        self.image_cache_controller = ImageCacheController(
            image_cache_directory,
            settings.image_cache_limit_bytes,
            maximum_bytes_saver=(
                self.settings_controller.set_image_cache_limit_bytes
            ),
            parent=self,
        )
        self.image_network_factory = ImageNetworkAccessManagerFactory(
            self.image_cache_controller
        )
        self.history_repository = HistoryRepository(database, busy_timeout_ms=0)
        scheduler = QtScheduler(self)
        self.task_queue = BoundedTaskQueue(
            TaskQueueConfig(),
            clock=SystemClock(),
            scheduler=scheduler,
        )
        self.network_manager = network_manager or QNetworkAccessManager(self)
        platform = runtime.platform if runtime is not None else create_platform_adapter()
        eagle_bridge = platform.services.eagle
        self.eagle_runtime = EagleRuntime(
            create_eagle_backend_factory(eagle_bridge), eagle_bridge.availability,
            self.task_queue, self,
        )
        self.eagle = EagleController(self.eagle_runtime, self)
        download_repository = DownloadRepository(database)
        self.download_manager = PersistentDownloadManager(
            download_repository, self.task_queue, self.network_manager,
            scheduler=scheduler, parent=self,
        )
        self.translation_store = TagTranslationStore(parent=self)
        self.query_executor = DanbooruMultiBranchExecutor(
            danbooru_base_url,
            network_manager=self.network_manager,
            task_queue=self.task_queue,
            parent=self,
        )
        self.query_controller = DanbooruM2QueryController(
            danbooru_base_url,
            executor=self.query_executor,
            tag_store=self.translation_store,
            parent=self,
        )
        self.query_bridge = M2QueryBridgeController(
            self.query_controller,
            parent=self,
        )
        self.detail_controller = DanbooruDetailController(
            danbooru_base_url,
            self.translation_store,
            parent=self,
        )
        favorite_repository = FavoriteRepository(database)
        self.write_access = SharedWriteAccess(self)
        self.favorite_writes = FavoriteWriteService(favorite_repository, self.write_access)
        self.favorite_controller = FavoriteController(favorite_repository, self.favorite_writes, self)
        self.cache_protection_service = CacheProtectionService(
            CacheProtectionRepository(database), self.write_access,
        )
        self.cache_protection = CacheProtectionController(
            self.cache_protection_service, self.favorite_controller, self.image_cache_controller, self,
        )
        self.gallery_selection = GallerySelectionController(
            self.query_controller.posts_model, self.favorite_controller, self
        )
        self.detail_favorites = DetailFavoritesController(
            self.detail_controller, self.favorite_controller, self
        )
        self.detail_navigation = DetailNavigationController(
            self.detail_controller,
            self.query_controller.posts_model,
            parent=self,
        )
        self.favorite_collection = FavoriteCollectionController(
            favorite_repository, self.favorite_controller, self.detail_navigation, self
        )
        self.pool_browser = PoolBrowserController(
            PoolRequestService(danbooru_base_url, task_queue=self.task_queue, network_manager=self.network_manager, parent=self),
            PoolRequestService(danbooru_base_url, task_queue=self.task_queue, network_manager=self.network_manager, parent=self),
            PoolPageRequestService(danbooru_base_url, self.task_queue, self.network_manager, self),
            self.favorite_controller, self,
        )
        self.pool_navigation = PoolNavigationController(self.pool_browser, self.detail_navigation, self)
        self.query_executor.executionBound.connect(lambda event: self.pool_navigation.invalidate())
        self.favorite_transfer = FavoriteTransferController(database.path, self.favorite_controller, self.write_access, self)
        self.artist_combinations = ArtistCombinationController(
            ArtistCombinationRepository(database), self.favorite_controller, self.query_bridge, self,
            access=self.write_access, favorite_repository=favorite_repository,
        )
        self.saved_queries = SavedQueryController(
            SavedQueryRepository(database), self.query_bridge, self.write_access, self
        )
        self.download_submission = DownloadSubmissionService(self.settings_controller)
        self.download_ui = DownloadUiController(
            self.download_manager, download_repository, self.settings_controller,
            self.query_controller.posts_model, self.gallery_selection, self.detail_controller,
            Path(settings.download_directory) if settings.download_directory else (runtime.paths.download_dir if runtime else temporary_path / "downloads"), self,
            submission=self.download_submission,
        )
        self.pool_reader = PoolReaderController(
            PoolPageRequestService(danbooru_base_url, self.task_queue, self.network_manager, self),
            self.favorite_controller, self.download_ui, self.translation_store, danbooru_base_url, self,
        )
        self.pool_navigation.attach_reader(self.pool_reader)
        self.pool_progress = PoolProgressController(PoolProgressRepository(database), self.pool_reader, danbooru_base_url, self)
        self.pool_download_manager = PoolDownloadManager(
            PoolDownloadRepository(database),
            PoolPageRequestService(danbooru_base_url, self.task_queue, self.network_manager, self),
            self.download_manager, danbooru_base_url, self,
        )
        self.pool_downloads = PoolDownloadController(
            self.pool_download_manager, self.settings_controller, self.download_ui,
            self.pool_browser, self.pool_reader, self,
            submission=self.download_submission,
        )
        self.pool_reader.changed.connect(self.download_ui.refresh_snapshot_source)
        self.favorite_collection.poolRequested.connect(self.pool_navigation.openFavoritePool)
        self.tag_controller = DanbooruTagSuggestionController(
            danbooru_base_url,
            network_manager=self.network_manager,
            task_queue=self.task_queue,
            translation_store=self.translation_store,
            csv_path_saver=self.settings_controller.set_tag_csv_path,
            connect_application_quit=False,
            parent=self,
        )
        if screen_geometries is None:
            resolved_screen_geometries = tuple(
                ScreenGeometry(
                    WindowGeometry(
                        screen.availableGeometry().x(),
                        screen.availableGeometry().y(),
                        screen.availableGeometry().width(),
                        screen.availableGeometry().height(),
                    ),
                    primary=screen is application.primaryScreen(),
                )
                for screen in application.screens()
            )
        else:
            resolved_screen_geometries = tuple(screen_geometries)
        checkpoint_repository = QueryCheckpointRepository(database)
        self.checkpoint_service = QueryCheckpointService(
            checkpoint_repository, self.history_repository, self.query_controller, self.write_access,
        )
        self.query_checkpoints = QueryCheckpointController(
            checkpoint_repository, self.checkpoint_service, self.query_bridge, self.write_access, self,
        )
        self.query_executor.before_attempt = self.checkpoint_service.persist_execution
        self.query_executor.before_abandon = self.checkpoint_service.persist_execution
        self.query_executor.before_publish = self.checkpoint_service.persist_execution
        self.session_controller = BrowserSessionController(
            self.history_repository,
            resolved_screen_geometries,
            self,
            m2_controller=self.query_controller,
            query_bridge=self.query_bridge,
            checkpoint_service=self.checkpoint_service,
        )
        self.query_checkpoints.attach_session(self.session_controller)
        self.cache_cleanup_service = CacheCleanupService(
            self.history_repository, checkpoint_repository, self.checkpoint_service, self.write_access,
        )
        self.cache_cleanup = CacheCleanupController(
            self.cache_cleanup_service, self.session_controller, self.write_access, self.image_cache_controller, self,
        )
        self._closed = False
        self._closing = False
        self._shutdown_errors: list[Exception] = []
        application.aboutToQuit.connect(self.shutdown)

    @Slot()
    def shutdown(self) -> None:
        if self._closed or self._closing:
            return
        self._closing = True
        steps = (
            self.eagle.shutdown,
            self.eagle_runtime.begin_shutdown,
            self.pool_downloads.shutdown,
            self.pool_download_manager.begin_shutdown,
            self.pool_progress.begin_shutdown,
            self.pool_reader.shutdown,
            self.pool_navigation.shutdown,
            self.pool_browser.shutdown,
            self.cache_cleanup.shutdown,
            self.download_ui.shutdown,
            self.download_manager.begin_shutdown,
            self.query_checkpoints.begin_shutdown,
            self.checkpoint_service.begin_shutdown,
            self.favorite_transfer.begin_shutdown,
            self.artist_combinations.shutdown,
            self.saved_queries.shutdown,
            self.favorite_collection.shutdown,
            self.favorite_controller.shutdown,
            self.gallery_selection.shutdown,
            self.detail_favorites.shutdown,
            self.query_bridge.beginClosing,
            self.favorite_transfer.shutdown,
            self.eagle_runtime.shutdown,
            self.pool_download_manager.finish_shutdown,
            self.pool_progress.finish_shutdown,
            self.checkpoint_service.finish_shutdown,
            self.download_manager.shutdown,
            self.appearance_controller.shutdown,
            self.query_controller.shutdown,
            self.session_controller.begin_shutdown,
            self.session_controller.detach_events,
            self.query_bridge.detach,
            self.detail_navigation.shutdown,
            self.tag_controller.begin_shutdown,
            self.tag_controller.shutdown,
            self.image_cache_controller.shutdown,
            self.cache_protection_service.shutdown,
            self.task_queue.shutdown,
        )
        try:
            for step in steps:
                try:
                    step()
                except Exception as exc:
                    self._shutdown_errors.append(exc)
        finally:
            self._closed = True
            self._closing = False

    @property
    def shutdown_completed(self) -> bool:
        """Report whether every session-owned asynchronous service is closed."""

        return (
            self._closed
            and self.session_controller._closed
            and self.session_controller.events_detached
            and self.detail_controller._shutdown
            and self.detail_navigation._closed
            and self.pool_navigation._closed
            and self.pool_browser._closed
            and self.pool_reader._closed
            and self.pool_progress._closed
            and self.pool_downloads._closed
            and self.pool_download_manager._closed
            and self.eagle._closed
            and self.eagle_runtime.shutdown_completed
            and self.favorite_transfer.shutdown_completed
            and self.download_manager.closed
            and self.download_ui._closed
            and self.artist_combinations._closed
            and self.saved_queries._closed
            and self.query_checkpoints._closed
            and self.checkpoint_service.closed
            and self.cache_cleanup._closed
            and self.cache_protection_service.closed
            and self.favorite_collection._closed
            and self.favorite_controller._closed
            and self.gallery_selection._closed
            and self.detail_favorites._closed
            and self.appearance_controller._closed
            and self.query_controller.closed
            and self.query_executor.closed
            and self.query_bridge.closing
            and self.query_bridge.detached
            and self.tag_controller._service._shutdown
            and self.image_cache_controller.closed
            and self.task_queue.is_shutdown
        )


def _create_qml_session(
    application: QGuiApplication,
    *,
    danbooru_base_url: str,
    network_manager: QNetworkAccessManager | None = None,
    runtime: RuntimeContext | None = None,
    screen_geometries: Sequence[ScreenGeometry] | None = None,
) -> tuple[QQmlApplicationEngine, _UiSession]:
    if (
        not isinstance(application, QGuiApplication)
        or QGuiApplication.instance() is not application
    ):
        raise RuntimeError(
            "create_qml_engine requires the active QGuiApplication instance"
        )
    configure_controls_style()
    engine = QQmlApplicationEngine()
    session = _UiSession(
        application,
        danbooru_base_url,
        network_manager=network_manager,
        runtime=runtime,
        screen_geometries=screen_geometries,
        parent=engine,
    )
    engine.setNetworkAccessManagerFactory(session.image_network_factory)
    engine.rootContext().setContextProperty(
        "appearanceController",
        session.appearance_controller,
    )
    engine.rootContext().setContextProperty(
        "searchController",
        session.query_bridge,
    )
    engine.rootContext().setContextProperty(
        "tagSuggestionController",
        session.tag_controller,
    )
    engine.rootContext().setContextProperty(
        "detailController",
        session.detail_controller,
    )
    engine.rootContext().setContextProperty(
        "detailNavigation",
        session.detail_navigation,
    )
    engine.rootContext().setContextProperty(
        "sessionController",
        session.session_controller,
    )
    engine.rootContext().setContextProperty(
        "imageCacheController",
        session.image_cache_controller,
    )
    engine.rootContext().setContextProperty("favoriteTransfer", session.favorite_transfer)
    engine.rootContext().setContextProperty("downloadUi", session.download_ui)
    engine.rootContext().setContextProperty("artistCombinations", session.artist_combinations)
    engine.rootContext().setContextProperty("savedQueries", session.saved_queries)
    engine.rootContext().setContextProperty("queryCheckpoints", session.query_checkpoints)
    engine.rootContext().setContextProperty("cacheCleanup", session.cache_cleanup)
    engine.rootContext().setContextProperty("poolBrowser", session.pool_browser)
    engine.rootContext().setContextProperty("poolNavigation", session.pool_navigation)
    engine.rootContext().setContextProperty("poolReader", session.pool_reader)
    engine.rootContext().setContextProperty("poolDownloads", session.pool_downloads)
    engine.rootContext().setContextProperty("eagleController", session.eagle)
    engine.rootContext().setContextProperty("favoriteCollection", session.favorite_collection)
    engine.rootContext().setContextProperty("favoriteController", session.favorite_controller)
    engine.rootContext().setContextProperty("gallerySelection", session.gallery_selection)
    engine.rootContext().setContextProperty("detailFavorites", session.detail_favorites)
    engine.load(QUrl.fromLocalFile(str(main_qml_path())))
    if session.settings_controller.settings.tag_csv_path:
        session.tag_controller.importSavedCsv(
            session.settings_controller.settings.tag_csv_path
        )
    return engine, session


def create_qml_engine(
    application: QGuiApplication,
    *,
    danbooru_base_url: str,
    network_manager: QNetworkAccessManager | None = None,
    runtime: RuntimeContext | None = None,
    screen_geometries: Sequence[ScreenGeometry] | None = None,
) -> tuple[QQmlApplicationEngine, M2QueryBridgeController]:
    engine, session = _create_qml_session(
        application,
        danbooru_base_url=danbooru_base_url,
        network_manager=network_manager,
        runtime=runtime,
        screen_geometries=screen_geometries,
    )
    return engine, session.query_bridge


def _normalized_image_formats() -> list[str]:
    values = {
        bytes(value).decode("ascii", errors="ignore").casefold()
        for value in QImageReader.supportedImageFormats()
    }
    values.discard("")
    return sorted(values)


def _normalized_tls_backends() -> list[str]:
    return sorted(
        {
            str(value).strip().casefold()
            for value in QSslSocket.availableBackends()
            if str(value).strip()
        }
    )


def gui_smoke_capabilities_succeed(
    *,
    qml_loaded: bool,
    shutdown_completed: bool,
    platform_name: str,
    tls_backends: Sequence[str],
    supported_image_formats: Sequence[str],
) -> bool:
    """Validate the runtime capabilities required by the Windows package."""

    normalized_backends = {value.casefold() for value in tls_backends}
    normalized_formats = {value.casefold() for value in supported_image_formats}
    return (
        qml_loaded
        and shutdown_completed
        and platform_name.casefold() in {"windows", "offscreen"}
        and "schannel" in normalized_backends
        and {"png", "gif", "webp"} <= normalized_formats
        and bool({"jpg", "jpeg"} & normalized_formats)
    )


def run_gui_smoke_check(
    application: QGuiApplication,
    *,
    danbooru_base_url: str,
    runtime: RuntimeContext,
) -> tuple[dict[str, object], int]:
    """Load the real QML shell, run one event turn, and close the session."""

    engine: QQmlApplicationEngine | None = None
    session: _UiSession | None = None
    qml_loaded = False
    shutdown_completed = False
    platform_name = QGuiApplication.platformName().casefold()
    tls_backends = _normalized_tls_backends()
    supported_image_formats = _normalized_image_formats()
    event_exit_code = 1
    try:
        engine, session = _create_qml_session(
            application,
            danbooru_base_url=danbooru_base_url,
            runtime=runtime,
        )
        qml_loaded = bool(engine.rootObjects())
        QTimer.singleShot(0, application.quit)
        event_exit_code = application.exec()
    finally:
        if session is not None:
            session.shutdown()
            shutdown_completed = session.shutdown_completed
        if engine is not None:
            for root in engine.rootObjects():
                close = getattr(root, "close", None)
                if callable(close):
                    close()
        application.processEvents()

    succeeded = gui_smoke_capabilities_succeed(
        qml_loaded=qml_loaded,
        shutdown_completed=shutdown_completed,
        platform_name=platform_name,
        tls_backends=tls_backends,
        supported_image_formats=supported_image_formats,
    ) and event_exit_code == 0
    return (
        {
            "status": "ok" if succeeded else "error",
            "mode": "gui",
            "version": application.applicationVersion(),
            "schema_version": runtime.database_version,
            "qml_loaded": qml_loaded,
            "shutdown_completed": shutdown_completed,
            "platform_name": platform_name,
            "tls_backends": tls_backends,
            "supported_image_formats": supported_image_formats,
            "data_directory": str(runtime.paths.data_dir),
        },
        0 if succeeded else 1,
    )


def launch_gui(
    application: QGuiApplication,
    *,
    danbooru_base_url: str,
    runtime: RuntimeContext | None = None,
) -> int:
    engine, _controller = create_qml_engine(
        application,
        danbooru_base_url=danbooru_base_url,
        runtime=runtime,
    )
    if not engine.rootObjects():
        return 1
    return application.exec()

