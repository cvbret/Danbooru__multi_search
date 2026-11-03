"""Load and run the minimal QML application shell."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtNetwork import QNetworkAccessManager

from danbooru_multi_search.danbooru import (
    DanbooruGalleryController,
    DanbooruTagSuggestionController,
)
from danbooru_multi_search.tasks import (
    BoundedTaskQueue,
    QtScheduler,
    SystemClock,
    TaskQueueConfig,
)


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
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        scheduler = QtScheduler(self)
        self.task_queue = BoundedTaskQueue(
            TaskQueueConfig(),
            clock=SystemClock(),
            scheduler=scheduler,
        )
        self.network_manager = network_manager or QNetworkAccessManager(self)
        self.gallery_controller = DanbooruGalleryController(
            danbooru_base_url,
            network_manager=self.network_manager,
            task_queue=self.task_queue,
            parent=self,
        )
        self.tag_controller = DanbooruTagSuggestionController(
            danbooru_base_url,
            network_manager=self.network_manager,
            task_queue=self.task_queue,
            parent=self,
        )
        self._closed = False
        application.aboutToQuit.connect(self.shutdown)

    @Slot()
    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.gallery_controller.shutdown()
        self.tag_controller.shutdown()
        self.task_queue.shutdown()


def create_qml_engine(
    application: QGuiApplication,
    *,
    danbooru_base_url: str,
    network_manager: QNetworkAccessManager | None = None,
) -> tuple[QQmlApplicationEngine, DanbooruGalleryController]:
    engine = QQmlApplicationEngine()
    session = _UiSession(
        application,
        danbooru_base_url,
        network_manager=network_manager,
        parent=engine,
    )
    engine.rootContext().setContextProperty(
        "searchController",
        session.gallery_controller,
    )
    engine.rootContext().setContextProperty(
        "tagSuggestionController",
        session.tag_controller,
    )
    engine.load(QUrl.fromLocalFile(str(main_qml_path())))
    return engine, session.gallery_controller


def launch_gui(application: QGuiApplication, *, danbooru_base_url: str) -> int:
    engine, _controller = create_qml_engine(
        application,
        danbooru_base_url=danbooru_base_url,
    )
    if not engine.rootObjects():
        return 1
    return application.exec()

