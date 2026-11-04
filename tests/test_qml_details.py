from __future__ import annotations

import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from PySide6.QtCore import (
    Q_ARG,
    QCoreApplication,
    QEvent,
    QObject,
    QMetaObject,
    QPoint,
    QPointF,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from danbooru_multi_search.danbooru import (
    DanbooruPost,
    TagIndexSnapshot,
    TagSuggestion,
    build_gallery_request_spec,
)
from danbooru_multi_search.ui.launcher import create_qml_engine


APPLICATION = QGuiApplication.instance() or QGuiApplication([])
PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
    "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
GIF_DATA_URL = (
    "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
)


class FakeReply(QObject):
    metaDataChanged = Signal()
    readyRead = Signal()
    finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.abort_count = 0

    def abort(self) -> None:
        self.abort_count += 1

    def readAll(self) -> bytes:
        return b""

    def error(self):
        return QNetworkReply.NetworkError.NoError

    def errorString(self) -> str:
        return ""

    def attribute(self, attribute):
        if attribute == QNetworkRequest.Attribute.HttpStatusCodeAttribute:
            return 200
        return None

    def rawHeader(self, name: str) -> bytes:
        return b""

    def deleteLater(self) -> None:
        pass


class FakeNetworkManager:
    def __init__(self) -> None:
        self.requests = []
        self.replies: list[FakeReply] = []

    def get(self, request):
        reply = FakeReply()
        self.requests.append(request)
        self.replies.append(reply)
        return reply


def _post(
    post_id: int = 1,
    *,
    extension: str = "jpg",
    media_url: str = PNG_DATA_URL,
    preview_url: str = PNG_DATA_URL,
):
    return DanbooruPost(
        id=post_id,
        preview_url=preview_url,
        large_file_url=media_url,
        file_url=media_url,
        score=0,
        fav_count=None,
        rating="g",
        width=1200,
        height=900,
        file_ext=extension,
        created_at="2024-01-02T03:04:05.000000Z",
        file_size=1024,
        source="<b>plain source</b>",
        artist_tags=("sample_artist",),
        general_tags=("blue_sky",),
    )


def _create_window(posts: tuple[DanbooruPost, ...]):
    manager = FakeNetworkManager()
    engine, gallery_controller = create_qml_engine(
        APPLICATION,
        danbooru_base_url="https://example.invalid",
        network_manager=manager,
    )
    gallery_controller.postsModel.set_posts(posts)
    root = engine.rootObjects()[0]
    detail_controller = engine.rootContext().contextProperty("detailController")
    return engine, root, gallery_controller, detail_controller, manager


def _wait_until(predicate, timeout_ms: int = 2_000) -> bool:
    elapsed = 0
    while elapsed < timeout_ms:
        APPLICATION.processEvents()
        if predicate():
            return True
        QTest.qWait(10)
        elapsed += 10
    return predicate()


def _find_visual_item(root, object_name: str):
    content_item = root.findChild(QQuickItem, "ApplicationWindowContentControl")
    pending = [content_item]
    while pending:
        item = pending.pop()
        if item.objectName() == object_name:
            return item
        pending.extend(item.childItems())
    return None


def _invoke(item, method: str) -> None:
    assert QMetaObject.invokeMethod(item, method, Qt.ConnectionType.DirectConnection)


def _open_post_details(root, row: int) -> None:
    assert QMetaObject.invokeMethod(
        root,
        "openPostDetails",
        Qt.ConnectionType.DirectConnection,
        Q_ARG("QVariant", row),
    )


def _close(engine) -> None:
    roots = tuple(engine.rootObjects())
    APPLICATION.aboutToQuit.emit()
    for root in roots:
        root.close()
    assert _wait_until(lambda: all(not root.isVisible() for root in roots))
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    APPLICATION.processEvents()


def test_opening_detail_loads_local_image_without_metadata_request():
    engine, root, gallery, detail, manager = _create_window((_post(),))
    try:
        gallery_grid = root.findChild(QObject, "galleryGrid")
        _open_post_details(root, 0)
        assert detail.isOpen is True
        assert detail.current_post is gallery.postsModel.posts[0]
        assert manager.requests == []
        assert _wait_until(lambda: detail.mediaState == "success")

        detail_page = root.findChild(QObject, "detailPage")
        viewer = root.findChild(QObject, "detailImageViewer")
        source_text = root.findChild(QObject, "detailSourceText")
        static_image = root.findChild(QObject, "detailStaticImage")
        assert detail_page.property("visible") is True
        assert viewer.property("intrinsicWidth") == 1
        assert viewer.property("zoomScale") > 8
        assert source_text.property("text") == "<b>plain source</b>"
        assert static_image is not None
        assert gallery_grid.property("count") == 1
    finally:
        _close(engine)


def test_clicking_gallery_card_opens_the_same_post():
    post = _post(77)
    engine, root, gallery, detail, manager = _create_window((post,))
    try:
        assert _wait_until(
            lambda: _find_visual_item(root, "postCard_77") is not None
        )
        card = _find_visual_item(root, "postCard_77")
        content_item = root.findChild(QQuickItem, "ApplicationWindowContentControl")
        point = card.mapToItem(
            content_item,
            card.property("width") / 2,
            card.property("height") / 2,
        )

        QTest.mouseClick(
            root,
            Qt.MouseButton.LeftButton,
            pos=QPoint(round(point.x()), round(point.y())),
        )

        assert _wait_until(lambda: detail.isOpen)
        assert detail.current_post is gallery.postsModel.posts[0]
        assert manager.requests == []
    finally:
        _close(engine)


def test_regular_back_restores_gallery_without_request():
    posts = tuple(_post(index + 1) for index in range(30))
    engine, root, gallery, detail, manager = _create_window(posts)
    try:
        grid = root.findChild(QObject, "galleryGrid")
        assert _wait_until(lambda: grid.property("count") == 30)
        grid.setProperty("currentIndex", 5)
        grid.setProperty("contentY", 175.25)
        saved_model = gallery.postsModel
        saved_revision = saved_model.revision

        _open_post_details(root, 5)
        assert detail.isOpen is True
        _invoke(root, "returnToGallery")
        assert _wait_until(lambda: not detail.isOpen)
        assert _wait_until(
            lambda: math.isclose(grid.property("contentY"), 175.25, abs_tol=0.01)
        )

        assert gallery.postsModel is saved_model
        assert gallery.postsModel.revision == saved_revision
        assert gallery.postsModel.count == 30
        assert grid.property("currentIndex") == 5
        assert manager.requests == []
    finally:
        _close(engine)


def test_escape_returns_once_and_preserves_gallery_query_controls():
    posts = tuple(_post(index + 1) for index in range(20))
    engine, root, gallery, detail, manager = _create_window(posts)
    try:
        committed = build_gallery_request_spec(
            "blue_sky",
            "questionable",
            page=3,
            limit=80,
        )
        gallery._committed_spec = committed
        tags_input = root.findChild(QObject, "tagsInput")
        rating_filter = root.findChild(QObject, "ratingFilter")
        page_size = root.findChild(QObject, "pageSizeInput")
        grid = root.findChild(QObject, "galleryGrid")
        tags_input.setProperty("text", "blue_sky")
        rating_filter.setProperty("currentIndex", 3)
        page_size.setProperty("value", 80)
        grid.setProperty("currentIndex", 4)
        grid.setProperty("contentY", 120.5)
        before_generation = detail.mediaGeneration
        before_revision = gallery.postsModel.revision

        _open_post_details(root, 4)
        assert detail.mediaGeneration == before_generation + 1
        escape_shortcut = root.findChild(QObject, "detailEscapeShortcut")
        assert escape_shortcut.property("enabled") is True
        escape_shortcut.activated.emit()

        assert _wait_until(lambda: not detail.isOpen)
        assert _wait_until(
            lambda: math.isclose(grid.property("contentY"), 120.5, abs_tol=0.01)
        )
        assert detail.mediaGeneration == before_generation + 2
        assert detail.mediaUrl == ""
        assert gallery._committed_spec is committed
        assert gallery.currentPage == 3
        assert gallery.pageSize == 80
        assert gallery.postsModel.revision == before_revision
        assert tags_input.property("text") == "blue_sky"
        assert rating_filter.property("currentValue") == "questionable"
        assert page_size.property("value") == 80
        assert grid.property("currentIndex") == 4
        assert manager.requests == []
    finally:
        _close(engine)


def test_detail_tag_click_starts_exactly_one_gallery_request_from_top():
    engine, root, _gallery, detail, manager = _create_window((_post(),))
    try:
        _open_post_details(root, 0)
        assert _wait_until(
            lambda: _find_visual_item(root, "detailTag_sample_artist") is not None
        )
        tag_button = _find_visual_item(root, "detailTag_sample_artist")
        tag_button.clicked.emit()

        assert _wait_until(lambda: len(manager.requests) == 1)
        assert detail.isOpen is False
        assert detail.gallery_snapshot is None
        assert root.findChild(QObject, "tagsInput").property("text") == "sample_artist"
        assert root.findChild(QObject, "galleryGrid").property("currentIndex") == -1
    finally:
        _close(engine)


def test_five_tag_groups_and_open_detail_translation_refresh():
    engine, root, _gallery, detail, _manager = _create_window((_post(),))
    try:
        _open_post_details(root, 0)
        group_names = (
            "artist",
            "character",
            "copyright",
            "general",
            "meta",
        )
        assert _wait_until(
            lambda: all(
                _find_visual_item(root, f"detailTagGroup_{name}") is not None
                for name in group_names
            )
        )
        for empty_name in ("character", "copyright", "meta"):
            empty_label = _find_visual_item(
                root,
                f"detailEmptyTagGroup_{empty_name}",
            )
            assert empty_label.property("visible") is True
            assert empty_label.property("text") == "暂无标签"

        artist_button = _find_visual_item(root, "detailTag_sample_artist")
        assert artist_button.property("text") == "sample_artist"
        current_post = detail.current_post
        generation = detail.mediaGeneration
        detail.translation_store.replace_snapshot(
            TagIndexSnapshot.build(
                (
                    TagSuggestion(
                        "sample_artist",
                        ("示例画师", "另一名称"),
                        1,
                        1,
                    ),
                )
            )
        )

        assert _wait_until(
            lambda: "示例画师、另一名称"
            in _find_visual_item(
                root,
                "detailTag_sample_artist",
            ).property("text")
        )
        assert detail.current_post is current_post
        assert detail.mediaGeneration == generation
    finally:
        _close(engine)


def test_gif_and_restricted_formats_use_expected_qml_components():
    gif_engine, gif_root, _gallery, gif_detail, _manager = _create_window(
        (_post(extension="gif", media_url=GIF_DATA_URL),)
    )
    try:
        _open_post_details(gif_root, 0)
        assert _wait_until(
            lambda: gif_root.findChild(QObject, "detailAnimatedImage") is not None
        )
        assert gif_detail.mediaComponent == "animated"
        assert gif_detail.requestQuality("preview") is True
        assert _wait_until(
            lambda: gif_root.findChild(QObject, "detailStaticImage") is not None
        )
        assert gif_detail.mediaComponent == "image"
        assert gif_detail.requestQuality("original") is True
        assert _wait_until(
            lambda: gif_root.findChild(QObject, "detailAnimatedImage") is not None
        )
        assert gif_detail.mediaComponent == "animated"
    finally:
        _close(gif_engine)

    webm_engine, webm_root, _gallery, webm_detail, _manager = _create_window(
        (_post(extension="webm", media_url="https://unused.invalid/video.webm"),)
    )
    try:
        _open_post_details(webm_root, 0)
        assert _wait_until(lambda: webm_detail.mediaState == "success")
        assert webm_detail.restrictedFormat is True
        assert webm_detail.mediaComponent == "image"
        assert webm_detail.mediaUrl == PNG_DATA_URL
        assert webm_root.findChild(QObject, "detailStaticImage") is not None
        format_status = webm_root.findChild(QObject, "detailFormatStatus")
        assert format_status.property("visible") is True
        assert format_status.property("text") == "受限格式：WEBM"
    finally:
        _close(webm_engine)


def test_detail_media_failure_does_not_change_gallery_or_start_request():
    missing = "qrc:/missing-detail-image.jpg"
    engine, root, gallery, detail, manager = _create_window(
        (_post(media_url=missing, preview_url=missing),)
    )
    try:
        before_posts = gallery.postsModel.posts
        before_revision = gallery.postsModel.revision
        _open_post_details(root, 0)

        assert _wait_until(lambda: detail.mediaState == "error")
        assert gallery.postsModel.posts is before_posts
        assert gallery.postsModel.revision == before_revision
        assert gallery.postsModel.count == 1
        assert manager.requests == []
    finally:
        _close(engine)


def test_new_post_uses_its_own_decoded_dimensions(tmp_path):
    second_path = tmp_path / "second image.png"
    image = QImage(2, 3, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.red)
    assert image.save(str(second_path), "PNG")
    second_url = QUrl.fromLocalFile(str(second_path)).toString()
    posts = (
        _post(1),
        _post(
            2,
            extension="png",
            media_url=second_url,
            preview_url=second_url,
        ),
    )
    engine, root, _gallery, detail, _manager = _create_window(posts)
    try:
        _open_post_details(root, 0)
        viewer = root.findChild(QObject, "detailImageViewer")
        assert _wait_until(lambda: viewer.property("intrinsicWidth") == 1)

        _open_post_details(root, 1)

        assert _wait_until(
            lambda: viewer.property("intrinsicWidth") == 2
            and viewer.property("intrinsicHeight") == 3
        )
        assert detail.current_post is posts[1]
        assert viewer.property("scaleMode") == "fit-window"
    finally:
        _close(engine)


def test_image_viewer_fit_modes_manual_limits_and_center_anchor():
    engine, root, _gallery, detail, _manager = _create_window((_post(),))
    try:
        _open_post_details(root, 0)
        assert _wait_until(lambda: detail.mediaState == "success")
        viewer = root.findChild(QObject, "detailImageViewer")
        viewport = root.findChild(QObject, "detailImageViewport")
        fit_window = root.findChild(QObject, "detailFitWindowButton")
        fit_width = root.findChild(QObject, "detailFitWidthButton")
        actual = root.findChild(QObject, "detailActualSizeButton")
        zoom_in = root.findChild(QObject, "detailZoomInButton")
        zoom_out = root.findChild(QObject, "detailZoomOutButton")

        viewer.setProperty("intrinsicWidth", 1000.0)
        viewer.setProperty("intrinsicHeight", 500.0)
        fit_window.clicked.emit()
        expected_fit = min(
            viewport.property("width") / 1000,
            viewport.property("height") / 500,
        )
        assert math.isclose(viewer.property("zoomScale"), expected_fit, rel_tol=1e-6)

        fit_width.clicked.emit()
        assert math.isclose(
            viewer.property("zoomScale"),
            viewport.property("width") / 1000,
            rel_tol=1e-6,
        )
        assert viewport.property("contentY") == 0

        actual.clicked.emit()
        assert viewer.property("zoomScale") == 1.0
        viewport.setProperty("contentX", 100.0)
        old_content_x = viewport.property("contentX")
        center_x = viewport.property("width") / 2
        zoom_in.clicked.emit()
        assert viewer.property("scaleMode") == "manual"
        assert math.isclose(viewer.property("zoomScale"), 1.15, rel_tol=1e-9)
        expected_x = (old_content_x + center_x) * 1.15 - center_x
        maximum_x = max(1000 * 1.15 - viewport.property("width"), 0)
        assert math.isclose(
            viewport.property("contentX"),
            max(0, min(expected_x, maximum_x)),
            abs_tol=0.01,
        )

        viewer.setProperty("intrinsicHeight", 2000.0)
        actual.clicked.emit()
        viewport.setProperty("contentX", 100.0)
        viewport.setProperty("contentY", 200.0)
        old_content_x = viewport.property("contentX")
        old_content_y = viewport.property("contentY")
        center_x = viewport.property("width") / 2
        center_y = viewport.property("height") / 2
        zoom_in.clicked.emit()
        expected_x = (old_content_x + center_x) * 1.15 - center_x
        expected_y = (old_content_y + center_y) * 1.15 - center_y
        maximum_x = max(1000 * 1.15 - viewport.property("width"), 0)
        maximum_y = max(2000 * 1.15 - viewport.property("height"), 0)
        assert math.isclose(
            viewport.property("contentX"),
            max(0, min(expected_x, maximum_x)),
            abs_tol=0.01,
        )
        assert math.isclose(
            viewport.property("contentY"),
            max(0, min(expected_y, maximum_y)),
            abs_tol=0.01,
        )

        viewer.setProperty("zoomScale", 7.99)
        zoom_in.clicked.emit()
        assert viewer.property("zoomScale") == 8.0
        viewer.setProperty("zoomScale", 0.051)
        zoom_out.clicked.emit()
        assert viewer.property("zoomScale") == 0.05

        viewer.setProperty("intrinsicWidth", 1.0)
        viewer.setProperty("intrinsicHeight", 1.0)
        fit_window.clicked.emit()
        assert viewer.property("zoomScale") > 8.0

        previous_scale = viewer.property("zoomScale")
        viewer.setProperty("intrinsicWidth", 0.0)
        fit_window.clicked.emit()
        assert viewer.property("zoomScale") == previous_scale
    finally:
        _close(engine)


def test_real_wheel_event_zooms_one_step_and_stops_default_scrolling():
    engine, root, _gallery, detail, _manager = _create_window((_post(),))
    warnings = []
    engine.warnings.connect(warnings.extend)
    try:
        _open_post_details(root, 0)
        assert _wait_until(lambda: detail.mediaState == "success")
        viewer = root.findChild(QQuickItem, "detailImageViewer")
        viewport = root.findChild(QQuickItem, "detailImageViewport")
        actual = root.findChild(QObject, "detailActualSizeButton")

        root.requestActivate()
        assert QTest.qWaitForWindowExposed(root)
        assert QTest.qWaitForWindowActive(root)
        root.update()
        QTest.qWait(100)

        viewer.setProperty("intrinsicWidth", 2000.0)
        viewer.setProperty("intrinsicHeight", 2000.0)
        actual.clicked.emit()
        assert _wait_until(
            lambda: math.isclose(viewport.property("contentWidth"), 2000.0)
            and math.isclose(viewport.property("contentHeight"), 2000.0)
        )
        viewport.setProperty("contentX", 100.0)
        viewport.setProperty("contentY", 150.0)
        assert _wait_until(
            lambda: math.isclose(viewport.property("contentX"), 100.0)
            and math.isclose(viewport.property("contentY"), 150.0)
        )

        anchor = QPointF(300.0, 250.0)
        window_position = viewer.mapToScene(anchor)
        old_x = viewport.property("contentX")
        old_y = viewport.property("contentY")

        QTest.mouseMove(
            root,
            QPoint(round(window_position.x()), round(window_position.y())),
        )
        QTest.qWait(50)
        QTest.wheelEvent(
            root,
            window_position,
            QPoint(0, 1),
            QPoint(),
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollBegin,
        )
        QTest.wheelEvent(
            root,
            window_position,
            QPoint(0, 120),
            QPoint(),
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate,
        )
        QTest.wheelEvent(
            root,
            window_position,
            QPoint(0, 1),
            QPoint(),
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollEnd,
        )
        delivered = _wait_until(
            lambda: viewer.property("scaleMode") == "manual",
            timeout_ms=750,
        )
        assert delivered, (
            window_position,
            viewer.property("visible"),
            viewport.property("visible"),
            viewport.property("width"),
            viewport.property("height"),
            root.width(),
            root.height(),
            root.isActive(),
            [warning.toString() for warning in warnings],
        )

        assert viewer.property("scaleMode") == "manual"
        assert math.isclose(viewer.property("zoomScale"), 1.15, rel_tol=1e-9)
        assert math.isclose(
            viewport.property("contentX"),
            (old_x + anchor.x()) * 1.15 - anchor.x(),
            abs_tol=0.01,
        )
        assert math.isclose(
            viewport.property("contentY"),
            (old_y + anchor.y()) * 1.15 - anchor.y(),
            abs_tol=0.01,
        )
        assert not any(
            "TypeError" in warning.toString()
            or "ImageViewer.qml" in warning.toString()
            for warning in warnings
        )
    finally:
        _close(engine)
