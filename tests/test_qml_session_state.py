from __future__ import annotations

import json
import os
from urllib.parse import parse_qs

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from PySide6.QtCore import (
    QMetaObject,
    QObject,
    Qt,
    Signal,
)
from PySide6.QtGui import QGuiApplication, QWindow
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest
from PySide6.QtTest import QTest

from danbooru_multi_search.core.bootstrap import initialize_runtime
from danbooru_multi_search.danbooru import DanbooruPost
from danbooru_multi_search.storage import GalleryPosition, HistoryRepository
from danbooru_multi_search.ui.window_state import (
    ScreenGeometry,
    WindowGeometry,
    restore_window_geometry,
)
from tests.m2_qml_harness import (
    close_m2_qml_test_engine,
    create_m2_qml_test_engine,
)


APPLICATION = QGuiApplication.instance() or QGuiApplication([])


class _PendingReply(QObject):
    metaDataChanged = Signal()
    readyRead = Signal()
    finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.abort_count = 0
        self.payload = b""

    def abort(self) -> None:
        self.abort_count += 1

    def readAll(self) -> bytes:
        return self.payload

    def error(self):
        return QNetworkReply.NetworkError.NoError

    def errorString(self) -> str:
        return ""

    def attribute(self, attribute):
        if attribute == QNetworkRequest.Attribute.HttpStatusCodeAttribute:
            return 200
        return None

    def rawHeader(self, _name) -> bytes:
        return b""

    def deleteLater(self) -> None:
        return


class _RecordingNetworkManager:
    def __init__(self) -> None:
        self.requests: list[QNetworkRequest] = []
        self.replies: list[_PendingReply] = []

    def get(self, request: QNetworkRequest) -> _PendingReply:
        reply = _PendingReply()
        self.requests.append(request)
        self.replies.append(reply)
        return reply


def _wait_until(predicate, timeout_ms: int = 2000) -> bool:
    elapsed = 0
    while elapsed < timeout_ms:
        APPLICATION.processEvents()
        if predicate():
            return True
        QTest.qWait(10)
        elapsed += 10
    return predicate()


def _close(engine) -> None:
    close_m2_qml_test_engine(engine)


def test_startup_and_history_selection_stay_offline_then_reexecute_first_page(
    tmp_path,
):
    runtime = initialize_runtime(tmp_path)
    repository = HistoryRepository(runtime.database)
    saved = repository.record_accepted_query(
        display_text="cat dog",
        normalized_tags=("cat", "dog"),
        rating="general",
        page_size=40,
        page=3,
        count_tag_usage=True,
    )
    assert repository.mark_query_committed(saved.id, 2)
    manager = _RecordingNetworkManager()
    engine, gallery = create_m2_qml_test_engine(
        APPLICATION,
        danbooru_base_url="https://example.invalid",
        network_manager=manager,
        runtime=runtime,
    )
    root = engine.rootObjects()[0]
    session = engine.rootContext().contextProperty("sessionController")
    try:
        QTest.qWait(350)
        APPLICATION.processEvents()
        assert manager.requests == []

        session.selectHistory(0)
        QTest.qWait(350)
        APPLICATION.processEvents()
        assert manager.requests == []

        session.restoreSelected()
        assert _wait_until(lambda: len(manager.requests) == 1)
        restore_query = parse_qs(manager.requests[0].url().query())
        assert restore_query["page"] == ["1"]

        gallery.cancel()
        search_button = root.findChild(QObject, "searchButton")
        search_button.clicked.emit()
        assert _wait_until(lambda: len(manager.requests) == 2)
        ordinary_query = parse_qs(manager.requests[1].url().query())
        assert ordinary_query["page"] == ["1"]
        assert all(
            request.url().path() == "/posts.json"
            for request in manager.requests
        )
    finally:
        _close(engine)


def test_hidden_window_does_not_replace_last_normal_geometry():
    engine, _gallery = create_m2_qml_test_engine(
        APPLICATION,
        danbooru_base_url="https://example.invalid",
        network_manager=_RecordingNetworkManager(),
    )
    root = engine.rootObjects()[0]
    session = engine.rootContext().contextProperty("sessionController")
    try:
        root.setX(70)
        root.setY(80)
        root.setWidth(1000)
        root.setHeight(700)
        assert _wait_until(
            lambda: session._normal_geometry.x == 70
            and session._normal_geometry.y == 80
            and session._normal_geometry.width == 1000
            and session._normal_geometry.height == 700
        )
        expected = session._normal_geometry

        root.showMaximized()
        APPLICATION.processEvents()
        root.hide()
        APPLICATION.processEvents()

        assert session._normal_geometry == expected
        assert session._is_maximized is True
    finally:
        _close(engine)


def test_maximized_state_is_persisted_and_applied_on_actual_restart(tmp_path):
    runtime = initialize_runtime(tmp_path)
    first_engine, _gallery = create_m2_qml_test_engine(
        APPLICATION,
        danbooru_base_url="https://example.invalid",
        network_manager=_RecordingNetworkManager(),
        runtime=runtime,
    )
    first_root = first_engine.rootObjects()[0]
    first_session = first_engine.rootContext().contextProperty(
        "sessionController"
    )
    try:
        first_root.setX(60)
        first_root.setY(70)
        first_root.setWidth(1000)
        first_root.setHeight(700)
        assert _wait_until(
            lambda: first_session._normal_geometry
            == WindowGeometry(60, 70, 1000, 700)
        )
        first_root.showMaximized()
        assert _wait_until(lambda: first_session._is_maximized is True)
    finally:
        _close(first_engine)

    persisted = HistoryRepository(runtime.database).load_session()
    assert persisted.is_maximized is True
    assert persisted.geometry is not None
    assert persisted.geometry.x == 60
    assert persisted.geometry.y == 70
    assert persisted.geometry.width == 1000
    assert persisted.geometry.height == 700
    expected_geometry = restore_window_geometry(
        WindowGeometry(60, 70, 1000, 700),
        (ScreenGeometry(WindowGeometry(0, 0, 1280, 820), primary=True),),
    )

    second_engine, _gallery = create_m2_qml_test_engine(
        APPLICATION,
        danbooru_base_url="https://example.invalid",
        network_manager=_RecordingNetworkManager(),
        runtime=runtime,
    )
    second_root = second_engine.rootObjects()[0]
    second_session = second_engine.rootContext().contextProperty(
        "sessionController"
    )
    try:
        assert second_session.startMaximized is True
        assert _wait_until(
            lambda: second_root.visibility()
            == QWindow.Visibility.Maximized
        )
        assert second_session._normal_geometry == expected_geometry
    finally:
        _close(second_engine)


def test_closing_reports_same_event_cycle_geometry_before_final_save(tmp_path):
    runtime = initialize_runtime(tmp_path)
    engine, _gallery = create_m2_qml_test_engine(
        APPLICATION,
        danbooru_base_url="https://example.invalid",
        network_manager=_RecordingNetworkManager(),
        runtime=runtime,
    )
    root = engine.rootObjects()[0]
    root.setX(90)
    root.setY(110)
    root.setWidth(800)
    root.setHeight(650)
    expected = WindowGeometry(root.x(), root.y(), root.width(), root.height())

    root.close()
    close_m2_qml_test_engine(engine)

    persisted = HistoryRepository(runtime.database).load_session()
    assert persisted.geometry is not None
    assert (
        persisted.geometry.x,
        persisted.geometry.y,
        persisted.geometry.width,
        persisted.geometry.height,
    ) == (expected.x, expected.y, expected.width, expected.height)


def test_small_screen_qml_minimum_does_not_force_window_off_screen():
    engine, _gallery = create_m2_qml_test_engine(
        APPLICATION,
        danbooru_base_url="https://example.invalid",
        network_manager=_RecordingNetworkManager(),
        screen_geometries=(
            ScreenGeometry(WindowGeometry(10, 20, 640, 480), primary=True),
        ),
    )
    root = engine.rootObjects()[0]
    try:
        assert root.property("minimumWidth") == 640
        assert root.property("minimumHeight") == 480
        assert root.width() == 640
        assert root.height() == 480
    finally:
        _close(engine)


def _gallery_post(post_id: int) -> DanbooruPost:
    return DanbooruPost(
        id=post_id,
        preview_url="qrc:/missing-session-thumbnail.png",
        large_file_url=None,
        file_url=None,
        score=0,
        fav_count=0,
        rating="g",
        width=100,
        height=100,
        file_ext="jpg",
    )


def _complete_query(bridge, manager, posts):
    """Deliver complete local rows through the formal executor and history events."""

    if bridge.core_controller.operation_id is None:
        assert bridge.executeStandard()
    assert _wait_until(lambda: bool(manager.replies))
    manager.replies[-1].payload = json.dumps([
        {
            "id": post.id,
            "preview_file_url": post.preview_url,
            "large_file_url": post.large_file_url,
            "file_url": post.file_url,
            "score": post.score,
            "fav_count": post.fav_count,
            "rating": post.rating,
            "image_width": post.width,
            "image_height": post.height,
            "file_ext": post.file_ext,
            "tag_string_artist": "",
            "tag_string_character": "",
            "tag_string_copyright": "",
            "tag_string_general": "cat",
            "tag_string_meta": "",
        }
        for post in posts
    ]).encode("utf-8")
    manager.replies[-1].finished.emit()


def test_first_visible_card_and_position_are_saved_after_500ms(tmp_path):
    runtime = initialize_runtime(tmp_path)
    repository = HistoryRepository(runtime.database)
    query = repository.record_accepted_query(
        display_text="cat",
        normalized_tags=("cat",),
        rating="all",
        page_size=40,
        page=1,
        count_tag_usage=True,
    )
    assert repository.mark_query_committed(query.id, 1)
    manager = _RecordingNetworkManager()
    engine, gallery = create_m2_qml_test_engine(
        APPLICATION,
        danbooru_base_url="https://example.invalid",
        network_manager=manager,
        runtime=runtime,
    )
    root = engine.rootObjects()[0]
    session = engine.rootContext().contextProperty("sessionController")
    grid = root.findChild(QObject, "galleryGrid")
    try:
        assert gallery.applyAdvancedText("cat")
        _complete_query(
            gallery, manager, tuple(_gallery_post(index + 1) for index in range(30))
        )
        assert _wait_until(lambda: session.activeHistoryId == query.id)
        assert _wait_until(lambda: grid.property("count") == 30)
        grid.setProperty("contentY", 330.0)
        assert _wait_until(
            lambda: session._gallery_position is not None
            and session._gallery_position.anchor_index > 0
        )
        position = session._gallery_position
        columns = max(1, int(grid.property("width") / grid.property("cellWidth")))
        assert position.anchor_index == columns
        assert position.anchor_post_id == columns + 1
        assert position.anchor_offset == 10.0

        QTest.qWait(350)
        assert repository.get_query(query.id).anchor_post_id is None
        QTest.qWait(250)
        persisted = repository.get_query(query.id)
        assert persisted.anchor_post_id == columns + 1
        assert persisted.anchor_index == columns
        assert persisted.anchor_offset == 10.0
        assert persisted.scroll_position == 330.0
    finally:
        _close(engine)


def test_delayed_restore_callback_cannot_move_gallery_after_edit(tmp_path):
    runtime = initialize_runtime(tmp_path)
    repository = HistoryRepository(runtime.database)
    query = repository.record_accepted_query(
        display_text="cat",
        normalized_tags=("cat",),
        rating="all",
        page_size=40,
        page=1,
        count_tag_usage=True,
    )
    assert repository.mark_query_committed(query.id, 1)
    assert repository.update_gallery_position(
        GalleryPosition(query.id, 6, 5, 0.0, 340.0)
    )
    manager = _RecordingNetworkManager()
    engine, gallery = create_m2_qml_test_engine(
        APPLICATION,
        danbooru_base_url="https://example.invalid",
        network_manager=manager,
        runtime=runtime,
    )
    root = engine.rootObjects()[0]
    session = engine.rootContext().contextProperty("sessionController")
    grid = root.findChild(QObject, "galleryGrid")
    try:
        session.restoreSelected()
        _complete_query(
            gallery, manager, tuple(_gallery_post(index + 1) for index in range(20))
        )
        assert session._restore_ready is not None
        grid.setProperty("currentIndex", -1)
        grid.setProperty("contentY", 0.0)
        assert QMetaObject.invokeMethod(
            root, "restoreHistoryPosition", Qt.ConnectionType.DirectConnection
        )
        token, revision = session._restore_application
        assert session.galleryRestorePlanCurrent(token, revision)
        session.controlsEdited()
        assert not session.galleryRestorePlanCurrent(token, revision)
        APPLICATION.processEvents()
        QTest.qWait(20)

        assert grid.property("currentIndex") == -1
        assert grid.property("contentY") == 0.0
    finally:
        _close(engine)
