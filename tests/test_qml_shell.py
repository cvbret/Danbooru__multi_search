from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PySide6.QtCore import QEventLoop, QObject, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickItem

from danbooru_multi_search.danbooru import DanbooruPost, RequestState
from danbooru_multi_search.core.bootstrap import initialize_runtime
from danbooru_multi_search.storage import HistoryError
from danbooru_multi_search.ui.launcher import create_qml_engine, launch_gui


def test_create_qml_engine_rejects_non_gui_application():
    with pytest.raises(RuntimeError, match="QGuiApplication"):
        create_qml_engine(
            QObject(),
            danbooru_base_url="https://example.invalid",
        )


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
        "detailPage",
        "detailImageViewer",
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


def test_qml_session_shares_one_queue_and_shutdown_is_repeatable():
    application = QGuiApplication.instance() or QGuiApplication([])
    engine, gallery_controller = create_qml_engine(
        application,
        danbooru_base_url="https://example.invalid",
    )
    tag_controller = engine.rootContext().contextProperty(
        "tagSuggestionController"
    )
    detail_controller = engine.rootContext().contextProperty("detailController")
    shared_queue = gallery_controller._task_queue

    assert tag_controller._task_queue is shared_queue
    assert gallery_controller._owns_queue is False
    assert tag_controller._owns_queue is False
    assert detail_controller.translation_store is tag_controller.translation_store

    tag_controller.shutdown()
    assert shared_queue.is_shutdown is False

    application.aboutToQuit.emit()
    application.aboutToQuit.emit()

    assert shared_queue.is_shutdown is True
    assert shared_queue.has_work is False


def test_coordinated_shutdown_continues_after_final_save_failure_and_reentry(
    tmp_path,
    monkeypatch,
):
    application = QGuiApplication.instance() or QGuiApplication([])
    runtime = initialize_runtime(tmp_path)
    engine, gallery = create_qml_engine(
        application,
        danbooru_base_url="https://example.invalid",
        runtime=runtime,
    )
    session_controller = engine.rootContext().contextProperty(
        "sessionController"
    )
    ui_session = session_controller.parent()
    detail = ui_session.detail_controller
    tags = ui_session.tag_controller
    images = ui_session.image_cache_controller
    queue = ui_session.task_queue
    events: list[str] = []
    accepted_requests = []
    gallery.requestAccepted.connect(accepted_requests.append)

    def fail_final_save(**_kwargs):
        raise HistoryError("simulated final save failure")

    monkeypatch.setattr(
        ui_session.history_repository,
        "save_browser_state",
        fail_final_save,
    )
    for name, owner, attribute in (
        ("session", session_controller, "begin_shutdown"),
        ("detail", detail, "shutdown"),
        ("gallery", gallery, "shutdown"),
        ("tags", tags, "shutdown"),
        ("images", images, "shutdown"),
        ("queue", queue, "shutdown"),
    ):
        original = getattr(owner, attribute)

        def record(*args, _name=name, _original=original, **kwargs):
            events.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(owner, attribute, record)

    reentry_count = 0

    def reenter_shutdown_and_request() -> None:
        nonlocal reentry_count
        reentry_count += 1
        ui_session.shutdown()
        gallery.search("reentrant")
        tags.updateInput("cat", 3)

    session_controller.noticeTextChanged.connect(reenter_shutdown_and_request)
    gallery.search("waiting")
    assert queue.has_work is True
    assert len(accepted_requests) == 1

    ui_session.shutdown()

    assert reentry_count == 1
    assert events == ["session", "detail", "gallery", "tags", "images", "queue"]
    assert "simulated final save failure" in session_controller.noticeText
    assert detail._shutdown is True
    assert gallery._shutdown is True
    assert tags._shutdown is True
    assert images.closed is True
    assert queue.is_shutdown is True
    assert queue.has_work is False
    assert len(accepted_requests) == 1

    for root in engine.rootObjects():
        root.close()
    engine.deleteLater()
