from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication

from danbooru_multi_search.danbooru import RequestState
from danbooru_multi_search.ui.launcher import create_qml_engine


def test_minimal_qml_window_loads():
    application = QGuiApplication.instance() or QGuiApplication([])
    engine, controller = create_qml_engine(
        application,
        danbooru_base_url="https://example.invalid",
    )

    assert application is not None
    assert engine.rootObjects()

    application.aboutToQuit.emit()
    controller.shutdown()
    controller.search("cat")
    assert controller.state == RequestState.ERROR
    assert "正在关闭" in controller.errorText
