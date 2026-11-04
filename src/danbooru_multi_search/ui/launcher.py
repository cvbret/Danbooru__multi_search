"""Load and run the minimal QML application shell."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from PySide6.QtCore import QObject, QUrl, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtNetwork import QNetworkAccessManager

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
from danbooru_multi_search.tasks import (
    BoundedTaskQueue,
    QtScheduler,
    SystemClock,
    TaskQueueConfig,
)
from danbooru_multi_search.storage import Database, HistoryRepository
from danbooru_multi_search.ui.image_cache import (
    ImageCacheController,
    ImageNetworkAccessManagerFactory,
)
from danbooru_multi_search.ui.session_state import BrowserSessionController
from danbooru_multi_search.ui.window_state import ScreenGeometry, WindowGeometry

if TYPE_CHECKING:
    from danbooru_multi_search.core.bootstrap import RuntimeContext


def main_qml_path() -> Path:
    return Path(__file__).resolve().parent / "qml" / "Main.qml"


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
        self.gallery_controller = DanbooruGalleryController(
            danbooru_base_url,
            network_manager=self.network_manager,
            task_queue=self.task_queue,
            connect_application_quit=False,
            parent=self,
        )
        self.detail_controller = DanbooruDetailController(
            danbooru_base_url,
            self.gallery_controller.postsModel,
            self.translation_store,
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
            self.gallery_controller,
            settings,
            resolved_screen_geometries,
            self,
        )
        self._closed = False
        application.aboutToQuit.connect(self.shutdown)

    @Slot()
    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.gallery_controller.begin_shutdown()
        self.tag_controller.begin_shutdown()
        self.session_controller.begin_shutdown()
        self.detail_controller.shutdown()
        self.gallery_controller.shutdown()
        self.tag_controller.shutdown()
        self.image_cache_controller.shutdown()
        self.task_queue.shutdown()


def create_qml_engine(
    application: QGuiApplication,
    *,
    danbooru_base_url: str,
    network_manager: QNetworkAccessManager | None = None,
    runtime: RuntimeContext | None = None,
    screen_geometries: Sequence[ScreenGeometry] | None = None,
) -> tuple[QQmlApplicationEngine, DanbooruGalleryController]:
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
        session.gallery_controller,
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
    return engine, session.gallery_controller


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

