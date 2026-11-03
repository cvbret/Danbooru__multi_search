from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QObject, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickItem

from danbooru_multi_search.danbooru import DanbooruPost, RequestState
from danbooru_multi_search.ui.launcher import create_qml_engine, launch_gui


def test_minimal_qml_window_loads():
    application = QGuiApplication.instance() or QGuiApplication([])
    engine, controller = create_qml_engine(
        application,
        danbooru_base_url="https://example.invalid",
    )

    assert application is not None
    assert engine.rootObjects()
    root = engine.rootObjects()[0]
    for object_name in (
        "tagsInput",
        "ratingFilter",
        "pageSizeInput",
        "galleryGrid",
        "previousPageButton",
        "nextPageButton",
        "refreshButton",
        "retryButton",
    ):
        assert root.findChild(QObject, object_name) is not None
    rating_filter = root.findChild(QObject, "ratingFilter")
    page_size_input = root.findChild(QObject, "pageSizeInput")
    assert rating_filter.property("currentValue") == "all"
    assert page_size_input.property("value") == 40
    assert page_size_input.property("from") == 1
    assert page_size_input.property("to") == 200

    application.aboutToQuit.emit()
    controller.shutdown()
    controller.search("cat")
    assert controller.state == RequestState.ERROR
    assert "正在关闭" in controller.errorText


def test_thumbnail_error_stays_inside_its_gallery_card():
    application = QGuiApplication.instance() or QGuiApplication([])
    engine, controller = create_qml_engine(
        application,
        danbooru_base_url="https://example.invalid",
    )
    controller.postsModel.set_posts(
        (
            DanbooruPost(
                id=123,
                preview_url="qrc:/missing-gallery-thumbnail.jpg",
                large_file_url=None,
                file_url=None,
                score=None,
                fav_count=0,
                rating="g",
                width=640,
                height=480,
                file_ext="jpg",
            ),
        )
    )

    event_loop = QEventLoop()
    QTimer.singleShot(100, event_loop.quit)
    event_loop.exec()

    root = engine.rootObjects()[0]
    gallery = root.findChild(QObject, "galleryGrid")
    assert gallery is not None
    assert gallery.property("count") == 1
    gallery.setProperty("currentIndex", 0)
    application.processEvents()
    card = gallery.property("currentItem")
    assert card is not None
    assert card.objectName() == "postCard_123"
    error_overlay = card.findChild(QObject, "thumbnailError_123")
    assert error_overlay is not None
    assert error_overlay.property("visible") is True
    assert controller.postsModel.count == 1
    assert controller.state == RequestState.IDLE

    controller.postsModel.set_posts(
        (
            DanbooruPost(
                id=124,
                preview_url=(
                    "data:image/png;base64,"
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
                    "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                ),
                large_file_url=None,
                file_url=None,
                score=0,
                fav_count=None,
                rating="s",
                width=1,
                height=1,
                file_ext="png",
            ),
        )
    )
    replacement_loop = QEventLoop()
    QTimer.singleShot(100, replacement_loop.quit)
    replacement_loop.exec()
    gallery.setProperty("currentIndex", 0)
    application.processEvents()

    replacement_card = gallery.property("currentItem")
    assert replacement_card.objectName() == "postCard_124"
    replacement_error = replacement_card.findChild(
        QObject,
        "thumbnailError_124",
    )
    assert replacement_error is not None
    assert replacement_error.property("visible") is False
    assert controller.postsModel.count == 1
    assert controller.state == RequestState.IDLE
    controller.shutdown()


def test_gui_event_loop_starts_and_closes_cleanly():
    application = QGuiApplication.instance() or QGuiApplication([])
    QTimer.singleShot(0, application.quit)

    exit_code = launch_gui(
        application,
        danbooru_base_url="https://example.invalid",
    )

    assert exit_code == 0
