import json
from urllib.parse import parse_qs, urlsplit

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtNetwork import QNetworkAccessManager
from PySide6.QtTest import QTest

from tests.m2_qml_harness import create_m2_qml_test_engine, close_m2_qml_test_engine
from tests.test_eagle_client import ServerResponse, eagle_server
from tests.test_favorite_controller import APPLICATION, IMAGE, post
from danbooru_multi_search.danbooru.result_processing import ProcessedPost
from tests.test_qml_favorites import click, item, wait


def _page(start, number, score=0):
    posts = [dict(id=1000-i, score=score, fav_count=0, rating="g", file_ext="png", file_url=IMAGE,
                  image_width=1, image_height=1, tag_string_artist="", tag_string_general="cat",
                  tag_string_character="", tag_string_copyright="", tag_string_meta="") for i in range(start, start+number)]
    return ServerResponse(json.dumps(posts).encode(), headers={"Content-Type": "application/json"})


def test_only_user_scroll_requests_next_fifty_and_keeps_existing_view(eagle_server):
    server = eagle_server(_page(0, 50), _page(50, 50), _page(100, 2))
    engine, bridge = create_m2_qml_test_engine(APPLICATION, danbooru_base_url=server.base_url.split("/api/")[0],
                                              network_manager=QNetworkAccessManager())
    root = engine.rootObjects()[0]
    grid = item(root, "galleryGrid")
    inserts, resets = [], []
    bridge.postsModel.rowsInserted.connect(lambda _parent, start, end: inserts.append((start, end)))
    bridge.postsModel.modelReset.connect(lambda: resets.append(True))
    try:
        root.requestActivate()
        assert QTest.qWaitForWindowExposed(root)
        click(root, item(root, "latestButton"))
        assert wait(lambda: bridge.loadedCount == 50 and bridge.canLoadMore)
        QTest.qWait(200)
        assert len(server.requests) == 1
        end = grid.property("originY") + grid.property("contentHeight") - grid.height()
        grid.setProperty("contentY", end - 8)
        APPLICATION.processEvents()
        assert len(server.requests) == 1
        before = len(resets)
        center = grid.mapToScene(QPointF(grid.width()/2, grid.height()/2))
        QTest.wheelEvent(root, center, QPoint(0, -120))
        assert wait(lambda: bridge.loadedCount == 100 and bridge.canLoadMore, 3000)
        assert len(resets) == before and inserts[-1] == (50,99)
        assert abs(grid.property("contentY") - end) < 20
        QTest.qWait(200)
        assert len(server.requests) == 2
        click(root, item(root, "loadMoreWorksButton"))
        assert wait(lambda: bridge.loadedCount == 102 and not bridge.canLoadMore)
        assert [parse_qs(urlsplit(req.path).query)["page"] for req in server.requests] == [["1"],["2"],["3"]]
        assert all(parse_qs(urlsplit(req.path).query)["limit"] == ["50"] for req in server.requests)
    finally:
        close_m2_qml_test_engine(engine)


def test_browse_limit_and_manual_continue_after_empty_filtered_page(eagle_server):
    server = eagle_server(_page(0, 50), _page(50, 2, 3))
    engine, bridge = create_m2_qml_test_engine(APPLICATION, danbooru_base_url=server.base_url.split("/api/")[0],
                                              network_manager=QNetworkAccessManager())
    root = engine.rootObjects()[0]
    try:
        maximum = item(root, "browseMaximum")
        maximum.forceActiveFocus()
        QTest.keyClick(root, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(root, Qt.Key.Key_5)
        QTest.keyClick(root, Qt.Key.Key_2)
        QTest.keyClick(root, Qt.Key.Key_Return)
        assert str(bridge.draftFormValues["candidateLimit"]) == "52"
        assert bridge.updateDraftForm({**bridge.draftFormValues, "minimumScore": "1"})
        click(root, item(root, "latestButton"))
        assert wait(lambda: bridge.canLoadMore and bridge.visibleCount == 0)
        QTest.qWait(150)
        assert len(server.requests) == 1
        click(root, item(root, "loadMoreWorksButton"))
        assert wait(lambda: bridge.visibleCount == 2 and not bridge.canLoadMore)
        assert len(server.requests) == 2 and bridge.uniqueCandidates == 52
    finally:
        close_m2_qml_test_engine(engine)


def test_failed_next_page_retries_same_page_only_on_click(eagle_server):
    server = eagle_server(_page(0, 50), ServerResponse(b"denied", status=403), _page(50, 1))
    engine, bridge = create_m2_qml_test_engine(APPLICATION, danbooru_base_url=server.base_url.split("/api/")[0],
                                              network_manager=QNetworkAccessManager())
    root = engine.rootObjects()[0]
    try:
        click(root, item(root, "latestButton"))
        assert wait(lambda: bridge.canLoadMore)
        click(root, item(root, "loadMoreWorksButton"))
        assert wait(lambda: bridge.canRetry) and bridge.loadedCount == 50
        QTest.qWait(150)
        assert len(server.requests) == 2
        click(root, item(root, "retryButton"))
        assert wait(lambda: bridge.loadedCount == 51)
        assert [parse_qs(urlsplit(req.path).query)["page"] for req in server.requests] == [["1"],["2"],["2"]]
    finally:
        close_m2_qml_test_engine(engine)


def test_dragging_scrollbar_to_bottom_loads_only_one_more_page(eagle_server):
    server = eagle_server(_page(0, 50), _page(50, 1))
    engine, bridge = create_m2_qml_test_engine(
        APPLICATION, danbooru_base_url=server.base_url.split("/api/")[0],
        network_manager=QNetworkAccessManager(),
    )
    root = engine.rootObjects()[0]
    grid = item(root, "galleryGrid")
    try:
        root.requestActivate()
        assert QTest.qWaitForWindowExposed(root)
        click(root, item(root, "latestButton"))
        assert wait(lambda: bridge.loadedCount == 50 and bridge.canLoadMore)
        bar = next(child for child in grid.childItems()
                   if "ScrollBar" in child.metaObject().className())
        thumb = bar.property("contentItem")
        start = thumb.mapToScene(QPointF(thumb.width()/2, thumb.height()/2))
        end = bar.mapToScene(QPointF(bar.width()/2, bar.height()-2))
        point = lambda value: QPoint(round(value.x()), round(value.y()))
        QTest.mouseMove(root, point(start))
        QTest.mousePress(root, Qt.MouseButton.LeftButton, pos=point(start))
        for step in range(1, 6):
            position = start + (end-start) * (step/5)
            QTest.mouseMove(root, point(position), delay=20)
        QTest.mouseRelease(root, Qt.MouseButton.LeftButton, pos=point(end))
        assert wait(lambda: bridge.loadedCount == 51 and not bridge.canLoadMore)
        QTest.qWait(150)
        assert len(server.requests) == 2
        assert [parse_qs(urlsplit(req.path).query)["page"] for req in server.requests] == [["1"], ["2"]]
    finally:
        close_m2_qml_test_engine(engine)


def test_sorted_new_rows_keep_first_visible_work_and_offset():
    engine, bridge = create_m2_qml_test_engine(APPLICATION, danbooru_base_url="https://example.invalid")
    root = engine.rootObjects()[0]
    grid = item(root, "galleryGrid")
    try:
        rows = tuple(ProcessedPost(post(i), None) for i in range(1,61))
        assert bridge.postsModel.set_processed_posts(7, rows)
        root.requestActivate()
        assert QTest.qWaitForWindowExposed(root)
        grid.forceLayout()
        grid.positionViewAtIndex(12, 0)
        QTest.qWait(80)
        grid.setProperty("contentY", grid.property("contentY") + 24)
        QTest.qWait(50)
        y = grid.property("contentY")
        old = grid.topVisibleItem(y)
        assert old is not None, (grid.property("count"), grid.height(), y)
        key = old.property("postKey")
        offset = y - old.y()
        added = tuple(ProcessedPost(post(i), None) for i in range(61,75))
        assert bridge.postsModel.set_processed_posts(7, added + rows)
        def kept():
            top = grid.topVisibleItem(grid.property("contentY"))
            if not top:
                return False
            columns = max(1, int(grid.width() / grid.property("cellWidth") + .001))
            return (top.property("index") <= bridge.postsModel.rowForPostKey(key) < top.property("index") + columns
                    and abs(grid.property("contentY") - top.y() - offset) < 1)
        assert wait(kept)
    finally:
        close_m2_qml_test_engine(engine)
