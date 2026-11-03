"""Load and run the minimal QML application shell."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

from danbooru_multi_search.danbooru import DanbooruSearchController


def main_qml_path() -> Path:
    return Path(__file__).resolve().parent / "qml" / "Main.qml"


def launch_gui(application: QGuiApplication, *, danbooru_base_url: str) -> int:
    engine = QQmlApplicationEngine()
    controller = DanbooruSearchController(danbooru_base_url, parent=engine)
    engine.rootContext().setContextProperty("searchController", controller)
    engine.load(QUrl.fromLocalFile(str(main_qml_path())))
    if not engine.rootObjects():
        return 1
    return application.exec()

