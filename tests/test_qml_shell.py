from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

from danbooru_multi_search.danbooru import DanbooruSearchController
from danbooru_multi_search.ui.launcher import main_qml_path


def test_minimal_qml_window_loads():
    application = QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlApplicationEngine()
    controller = DanbooruSearchController("https://example.invalid", parent=engine)
    engine.rootContext().setContextProperty("searchController", controller)

    engine.load(QUrl.fromLocalFile(str(main_qml_path())))

    assert application is not None
    assert engine.rootObjects()
