import json

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest

from danbooru_multi_search.core.favorites import FavoriteCandidate
from tests.test_favorite_controller import APPLICATION, IMAGE
from tests.test_pool_requests import FakeReply
from tests.test_pool_parsing import pool_mapping
from tests.test_pool_post_parsing import raw_post
from tests.test_qml_pools import window, finish_request
from tests.test_qml_favorites import click, wait, item, find
from tests.m2_qml_harness import close_m2_qml_test_engine


def start_reader(*, narrow=False, variants=None, omitted=(), data_root=None, identifiers=None):
    if data_root is None:
        engine, root, bridge, session, network = window()
    else:
        from danbooru_multi_search.core.bootstrap import initialize_runtime
        from tests.m2_qml_harness import create_m2_qml_test_engine
        from tests.test_pool_requests import FakeManager
        network = FakeManager()
        engine, bridge = create_m2_qml_test_engine(
            APPLICATION, danbooru_base_url="https://example.invalid", network_manager=network,
            runtime=initialize_runtime(data_root),
        )
        root, session = engine.rootObjects()[0], bridge.parent()
        root.requestActivate()
        assert QTest.qWaitForWindowExposed(root)
    if narrow:
        root.setWidth(820)
        root.setHeight(600)
    repository = session.favorite_controller._repository
    repository.add_favorite(FavoriteCandidate.for_pool(7, "saved series"))
    click(root, item(root, "favoritesNavigationButton"))
    assert wait(lambda: session.favorite_collection.loadedCount == 1)
    key = str(session.favorite_collection._model.items[0].id)
    ids = identifiers or tuple(range(1, 82))
    pool = FakeReply(json.dumps(pool_mapping(7, post_ids=ids)).encode())
    rows = [raw_post(number, preview_file_url=IMAGE, large_file_url=IMAGE,
                     file_url=f"http://127.0.0.1:1/{number}.png") for number in dict.fromkeys(ids[:40]) if number not in omitted]
    for row in rows:
        row.update((variants or {}).get(row["id"], {}))
    pages = FakeReply(json.dumps(rows).encode())
    network.replies.extend([pool, pages])
    control = item(root, "openFavoritePool_" + key)
    grid = item(root, "favoriteGrid")
    point = control.mapToScene(QPointF(0, control.height()))
    bottom = grid.mapToScene(QPointF(0, grid.height())).y()
    if point.y() > bottom:
        grid.setProperty("contentY", grid.property("contentY") + point.y() - bottom + 8)
        APPLICATION.processEvents()
    click(root, control)
    finish_request(network, pool, 1)
    finish_request(network, pages, 2)
    click(root, item(root, "poolReadButton"))
    assert wait(lambda: root.property("activePage") == "reader" and session.pool_reader.active)
    return engine, root, bridge, session, network


def descendants(root):
    pending = [root.contentItem()]
    while pending:
        candidate = pending.pop()
        yield candidate
        pending.extend(candidate.childItems())


@pytest.mark.parametrize("narrow", [False, True])
def test_reader_real_scroll_mode_zoom_and_saved_collection_return(narrow):
    engine, root, bridge, session, network = start_reader(narrow=narrow)
    warnings = []
    engine.warnings.connect(warnings.extend)
    try:
        reader = session.pool_reader
        assert wait(lambda: reader._media.get(1) and reader._media[1].mediaState == "success")
        continuous = item(root, "readerContinuousList")
        assert continuous.height() > 100 and continuous.property("contentHeight") > continuous.height() * 3
        active = [control for control in descendants(root) if control.objectName().startswith("readerContinuousMedia_") and control.property("active")]
        assert 1 <= len(active) <= 3
        token = active[0].parentItem().property("rebuildToken")
        QTest.qWait(100)
        assert active[0].parentItem().property("rebuildToken") == token
        point = continuous.mapToScene(QPointF(continuous.width() / 2, continuous.height() / 2))
        before = continuous.property("contentY")
        QTest.wheelEvent(root, point, QPoint(0, -120), QPoint(), Qt.KeyboardModifier.NoModifier)
        assert wait(lambda: continuous.property("contentY") > before)
        click(root, item(root, "readerNextButton"))
        assert wait(lambda: reader.currentPage == 2)
        click(root, item(root, "readerZoomInButton"))
        zoom = reader.zoom
        click(root, item(root, "readerSingleButton"))
        assert wait(lambda: reader.mode == "single")
        assert reader.currentPage == 2 and reader.zoom == zoom
        assert wait(lambda: not any(control.property("active") for control in descendants(root)
                                   if control.objectName().startswith("readerContinuousMedia_")))
        click(root, item(root, "readerContinuousButton"))
        assert wait(lambda: reader.mode == "continuous")
        assert reader.currentPage == 2 and reader.zoom == zoom
        click(root, item(root, "readerBackButton"))
        assert wait(lambda: not reader.active and root.property("activePage") == "pool")
        assert not reader._media
        click(root, item(root, "poolReturnSourceButton"))
        assert wait(lambda: root.property("activePage") == "favorites")
        assert session.favorite_collection.loadedCount == 1
        assert len(network.requests) == 2 and session.history_repository.list_queries() == ()
        assert not [warning.toString() for warning in warnings
                    if "TypeError" in warning.toString() or "ReferenceError" in warning.toString()]
    finally:
        close_m2_qml_test_engine(engine)


def test_reader_unsupported_and_failed_pages_keep_positions_and_retry_one_page():
    engine, root, bridge, session, network = start_reader(
        variants={1: {"file_ext": "mp4"}, 2: {"is_deleted": True}, 4: {"image_width": 0}}, omitted=(3,),
    )
    try:
        reader = session.pool_reader
        assert wait(lambda: reader._media.get(1) is not None)
        assert "MP4" in item(root, "readerPageNotice_1").property("text")
        assert [reader.model.slot(number).metadata.state.value for number in (2, 3, 4)] == ["deleted", "not_returned", "failed"]
        before = reader.model.cache[4]
        click(root, item(root, "readerNextButton"))
        assert wait(lambda: reader.currentPage == 2)
        assert not reader.favoriteState["enabled"]
        response = FakeReply(json.dumps([raw_post(2, preview_file_url=IMAGE, large_file_url=IMAGE)]).encode())
        network.replies.append(response)
        click(root, item(root, "readerRetryButton"))
        finish_request(network, response, 3)
        assert wait(lambda: reader.model.slot(2).metadata.state.value == "available")
        assert reader.model.cache[4] is before and reader.totalPages == 81
        assert "id:2 status:any" in network.requests[-1].url().toString()
    finally:
        close_m2_qml_test_engine(engine)


def test_directory_browsing_current_actions_and_far_detail_return_keep_actual_anchor():
    engine, root, bridge, session, network = start_reader()
    try:
        reader = session.pool_reader
        click(root, item(root, "readerDirectoryButton"))
        assert wait(lambda: item(root, "readerDirectory").property("opened"))
        click(root, item(root, "readerDirectoryNextButton"))
        assert wait(lambda: reader.directoryStart == 6)
        assert reader.currentPage == 1
        assert item(root, "readerDirectoryPage_6") is not None
        assert len(network.requests) == 2
        click(root, item(root, "readerDirectoryCloseButton"))
        assert wait(lambda: not item(root, "readerDirectory").property("visible"))
        response = FakeReply(json.dumps([
            raw_post(number, preview_file_url=IMAGE, large_file_url=IMAGE,
                     file_url=f"http://127.0.0.1:1/{number}.png") for number in range(48, 53)
        ]).encode())
        network.replies.append(response)
        item(root, "readerJumpInput").setProperty("text", "50")
        click(root, item(root, "readerJumpButton"))
        finish_request(network, response, 3)
        assert wait(lambda: reader.currentPage == 50 and reader._media.get(50) and reader._media[50].mediaState == "success")
        continuous = item(root, "readerContinuousList")
        continuous.setProperty("contentY", continuous.property("contentY") + 40)
        APPLICATION.processEvents()
        page = item(root, "readerPage_50")
        offset = continuous.property("contentY") - page.y()
        assert abs(reader.scrollOffset - offset) < 2
        click(root, item(root, "readerFavoriteButton"))
        assert reader.favoriteState["saved"] and reader.active and not session.detail_controller.isOpen
        click(root, item(root, "readerDownloadButton"))
        assert wait(lambda: item(root, "downloadRequestDialog").property("opened"))
        from tests.test_qml_downloads import press_dialog
        press_dialog(root, "cancelDownloadRequestButton")
        assert session.download_manager._repository.list_batches() == ()
        assert wait(lambda: not item(root, "downloadRequestDialog").property("visible"))
        click(root, item(root, "readerDetailButton"))
        assert wait(lambda: session.detail_controller.isOpen and session.detail_controller.current_post.id == 50)
        assert not reader.active and not reader._media
        click(root, item(root, "detailBackButton"))
        assert wait(lambda: reader.active and find(root, "readerPage_50") is not None)
        assert wait(lambda: abs(continuous.property("contentY") - item(root, "readerPage_50").y() - offset) < 2)
        assert reader.currentPage == 50 and len(network.requests) == 3
    finally:
        close_m2_qml_test_engine(engine)


def test_persistent_reader_reopens_by_post_and_actual_offset_then_restarts_and_resizes(tmp_path):
    from danbooru_multi_search.storage.pool_progress import PoolProgressRepository
    engine, root, bridge, session, network = start_reader(data_root=tmp_path)
    try:
        reader = session.pool_reader
        item(root, "readerJumpInput").setProperty("text", "3")
        click(root, item(root, "readerJumpButton"))
        assert wait(lambda: reader.currentPage == 3 and reader.position_ready and find(root, "readerPage_3") is not None)
        view = item(root, "readerContinuousList")
        view.setProperty("contentY", view.property("contentY") + 35)
        assert wait(lambda: reader.scrollOffset > 20)
        store = session.pool_progress._repository
        assert wait(lambda: (stored := store.get("https://example.invalid", 7)) is not None and stored.page_number == 3 and stored.offset > 20)
        saved = store.get("https://example.invalid", 7)
    finally:
        close_m2_qml_test_engine(engine)
    ids = (1, 2, 4, 5, 3) + tuple(range(6, 82))
    engine, root, bridge, session, network = start_reader(data_root=tmp_path, identifiers=ids)
    try:
        reader = session.pool_reader
        assert wait(lambda: reader.currentPage == 5 and reader.position_ready and find(root, "readerPage_5") is not None)
        view = item(root, "readerContinuousList")
        assert wait(lambda: abs(view.property("contentY") - item(root, "readerPage_5").y() - saved.offset) < 2)
        assert session.pool_progress._repository.get(saved.source, 7).post_id == 3
        root.setWidth(820)
        root.setHeight(600)
        assert wait(lambda: reader.position_ready and reader.viewport_size[0] < saved.viewport_width)
        assert reader.currentPage == 5 and reader.scrollOffset == 0
        click(root, item(root, "readerRestartButton"))
        assert wait(lambda: reader.currentPage == 1 and reader.position_ready)
        assert wait(lambda: (stored := session.pool_progress._repository.get(saved.source, 7)).page_number == 1 and stored.post_id == 1)
        assert reader.scrollOffset == 0 and len(network.requests) == 2
    finally:
        close_m2_qml_test_engine(engine)


def test_reader_progress_read_error_can_be_retried_by_real_minimum_window_button(tmp_path, monkeypatch):
    from danbooru_multi_search.core.bootstrap import initialize_runtime
    from danbooru_multi_search.core.pool_progress import PoolProgressError
    from danbooru_multi_search.storage.pool_progress import PoolProgressRepository
    from tests.test_pool_progress import progress
    runtime = initialize_runtime(tmp_path)
    store = PoolProgressRepository(runtime.database)
    saved = store.save(progress(source="https://example.invalid", post_id=3), expected=None)
    original = PoolProgressRepository.get
    with monkeypatch.context() as failed:
        def read_error(*args):
            raise PoolProgressError("storage_error", "simulated read failure")
        failed.setattr(PoolProgressRepository, "get", read_error)
        engine, root, bridge, session, network = start_reader(narrow=True, data_root=tmp_path)
        assert wait(lambda: session.pool_reader.position_ready)
        QTest.qWait(450)
        assert session.pool_reader.currentPage == 1
        assert original(store, saved.source, 7) == saved
    try:
        button = item(root, "readerRetryProgressButton")
        assert button.property("visible")
        click(root, button)
        assert wait(lambda: session.pool_reader.currentPage == 3 and session.pool_reader.position_ready)
        assert not session.pool_reader.progressStatus
        assert original(store, saved.source, 7).post_id == 3
    finally:
        close_m2_qml_test_engine(engine)
