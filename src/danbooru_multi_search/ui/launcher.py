"""Load and run the minimal QML application shell."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

from danbooru_multi_search.danbooru import DanbooruSearchController


def main_qml_path() -> Path:
    return Path(__file__).resolve().parent / "qml" / "Main.qml"


def create_qml_engine(
    application: QGuiApplication,
    *,
    danbooru_base_url: str,
) -> tuple[QQmlApplicationEngine, DanbooruSearchController]:
    engine = QQmlApplicationEngine()
    controller = DanbooruSearchController(danbooru_base_url, parent=engine)
    application.aboutToQuit.connect(controller.shutdown)
    engine.rootContext().setContextProperty("searchController", controller)
    engine.load(QUrl.fromLocalFile(str(main_qml_path())))
    return engine, controller


def launch_gui(application: QGuiApplication, *, danbooru_base_url: str) -> int:
    engine, _controller = create_qml_engine(
        application,
        danbooru_base_url=danbooru_base_url,
    )
    if not engine.rootObjects():
        return 1
    return application.exec()

