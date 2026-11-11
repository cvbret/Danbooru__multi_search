"""Assemble and run the M2 QML application."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from PySide6.QtCore import QObject, QTimer, QUrl, Slot
from PySide6.QtGui import QGuiApplication, QImageReader
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtNetwork import QNetworkAccessManager, QSslSocket

from danbooru_multi_search.core.settings import (
    AppSettings,
    SettingsController,
    SettingsStore,
)
from danbooru_multi_search.danbooru import (
    DanbooruDetailController,
    DanbooruM2QueryController,
    DanbooruMultiBranchExecutor,
    DanbooruTagSuggestionController,
    TagTranslationStore,
)
from danbooru_multi_search.tasks import (
    BoundedTaskQueue,
    QtScheduler,
    SystemClock,
    TaskQueueConfig,
)
from danbooru_multi_search.storage import Database, HistoryRepository
from danbooru_multi_search.ui.detail_navigation import DetailNavigationController
from danbooru_multi_search.ui.image_cache import (
    ImageCacheController,
    ImageNetworkAccessManagerFactory,
)
from danbooru_multi_search.ui.query_state import M2QueryBridgeController
from danbooru_multi_search.ui.session_state import BrowserSessionController
from danbooru_multi_search.ui.window_state import ScreenGeometry, WindowGeometry

if TYPE_CHECKING:
    from danbooru_multi_search.core.bootstrap import RuntimeContext


def main_qml_path() -> Path:
    return Path(__file__).resolve().parent / "qml" / "M2Main.qml"


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
        self.history_repository = HistoryRepository(database)
        scheduler = QtScheduler(self)
        self.task_queue = BoundedTaskQueue(
            TaskQueueConfig(),
            clock=SystemClock(),
            scheduler=scheduler,
        )
        self.network_manager = network_manager or QNetworkAccessManager(self)
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
        self.detail_navigation = DetailNavigationController(
            self.detail_controller,
            self.query_controller.posts_model,
            parent=self,
        )
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
        self.session_controller = BrowserSessionController(
            self.history_repository,
            None,
            settings,
            resolved_screen_geometries,
            self,
            m2_controller=self.query_controller,
            query_bridge=self.query_bridge,
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
            self.query_bridge.beginClosing,
            self.query_controller.shutdown,
            self.session_controller.begin_shutdown,
            self.session_controller.detach_events,
            self.query_bridge.detach,
            self.detail_navigation.shutdown,
            self.tag_controller.begin_shutdown,
            self.tag_controller.shutdown,
            self.image_cache_controller.shutdown,
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
            and self.query_controller.closed
            and self.query_executor.closed
            and self.query_bridge.closing
            and self.query_bridge.detached
            and self.tag_controller._shutdown
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

