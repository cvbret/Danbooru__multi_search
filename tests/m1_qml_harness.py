"""Isolated M1 QML assembly retained for compatibility tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QNetworkAccessManager
from PySide6.QtQml import QQmlApplicationEngine

from danbooru_multi_search.core.settings import (
    AppSettings,
    SettingsController,
    SettingsStore,
)
from danbooru_multi_search.danbooru import (
    DanbooruDetailController,
    DanbooruGalleryController,
    DanbooruTagSuggestionController,
    TagTranslationStore,
)
from danbooru_multi_search.storage import Database, HistoryRepository
from danbooru_multi_search.tasks import (
    BoundedTaskQueue,
    QtScheduler,
    SystemClock,
    TaskQueueConfig,
)
from danbooru_multi_search.ui.detail_navigation import DetailNavigationController
from danbooru_multi_search.ui.image_cache import (
    ImageCacheController,
    ImageNetworkAccessManagerFactory,
)
from danbooru_multi_search.ui import session_state as session_state_module
from danbooru_multi_search.ui.session_state import BrowserSessionController
from danbooru_multi_search.ui.window_state import ScreenGeometry, WindowGeometry

if TYPE_CHECKING:
    from danbooru_multi_search.core.bootstrap import RuntimeContext


class _M1QmlTestSession(QObject):
    """Own one M1-only controller graph and its private task queue."""

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
                prefix="danbooru-m1-qml-test-"
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
        self.gallery_controller = DanbooruGalleryController(
            danbooru_base_url,
            network_manager=self.network_manager,
            task_queue=self.task_queue,
            connect_application_quit=False,
            parent=self,
        )
        self.detail_controller = DanbooruDetailController(
            danbooru_base_url,
            self.translation_store,
            parent=self,
        )
        self.detail_navigation = DetailNavigationController(
            self.detail_controller,
            self.gallery_controller.postsModel,
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
            resolved_screens = tuple(
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
            resolved_screens = tuple(screen_geometries)
        self.session_controller = BrowserSessionController(
            self.history_repository,
            self.gallery_controller,
            settings,
            resolved_screens,
            self,
        )
        self._closing = False
        self._closed = False
        application.aboutToQuit.connect(self.shutdown)

    def shutdown(self) -> None:
        if self._closed or self._closing:
            return
        self._closing = True
        steps = (
            self.gallery_controller.begin_shutdown,
            self.tag_controller.begin_shutdown,
            self.session_controller.begin_shutdown,
            self.session_controller.detach_events,
            self.detail_navigation.shutdown,
            self.gallery_controller.shutdown,
            self.tag_controller.shutdown,
            self.image_cache_controller.shutdown,
            self.task_queue.shutdown,
        )
        try:
            for step in steps:
                try:
                    step()
                except Exception:
                    continue
        finally:
            self._closed = True
            self._closing = False


def create_m1_qml_test_engine(
    application: QGuiApplication,
    *,
    danbooru_base_url: str,
    network_manager: QNetworkAccessManager | None = None,
    runtime: RuntimeContext | None = None,
    screen_geometries: Sequence[ScreenGeometry] | None = None,
) -> tuple[QQmlApplicationEngine, DanbooruGalleryController]:
    """Load the retained M1 root without changing the production launcher."""

    if (
        not isinstance(application, QGuiApplication)
        or QGuiApplication.instance() is not application
    ):
        raise RuntimeError(
            "create_m1_qml_test_engine requires the active QGuiApplication"
        )
    engine = QQmlApplicationEngine()
    session = _M1QmlTestSession(
        application,
        danbooru_base_url,
        network_manager=network_manager,
        runtime=runtime,
        screen_geometries=screen_geometries,
        parent=engine,
    )
    engine.setNetworkAccessManagerFactory(session.image_network_factory)
    context = engine.rootContext()
    context.setContextProperty("searchController", session.gallery_controller)
    context.setContextProperty("tagSuggestionController", session.tag_controller)
    context.setContextProperty("detailController", session.detail_controller)
    context.setContextProperty("detailNavigation", session.detail_navigation)
    context.setContextProperty("sessionController", session.session_controller)
    context.setContextProperty(
        "imageCacheController",
        session.image_cache_controller,
    )
    qml_path = (
        Path(session_state_module.__file__).resolve().parent
        / "qml"
        / "Main.qml"
    )
    engine.load(QUrl.fromLocalFile(str(qml_path)))
    if session.settings_controller.settings.tag_csv_path:
        session.tag_controller.importSavedCsv(
            session.settings_controller.settings.tag_csv_path
        )
    return engine, session.gallery_controller


def close_m1_qml_test_engine(engine: QQmlApplicationEngine) -> None:
    """Close only the M1 graph owned by ``engine`` and drain Qt deletion."""

    session_controller = engine.rootContext().contextProperty(
        "sessionController"
    )
    owner = session_controller.parent()
    owner.shutdown()
    for root in engine.rootObjects():
        root.close()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application = QGuiApplication.instance()
    if application is not None:
        application.processEvents()
