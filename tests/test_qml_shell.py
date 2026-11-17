from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QEventLoop,
    QObject,
    QTimer,
    Signal,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from danbooru_multi_search.danbooru import (
    DanbooruPost,
    QueryExecutionState,
)
from danbooru_multi_search.core.bootstrap import initialize_runtime
from danbooru_multi_search.storage import HistoryError, HistoryRepository
from danbooru_multi_search.ui.launcher import (
    create_qml_engine,
    gui_smoke_capabilities_succeed,
    launch_gui,
)


class _PendingReply(QObject):
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


def _wait_until(predicate, timeout_ms: int = 2_000) -> bool:
    elapsed = 0
    while elapsed < timeout_ms:
        QGuiApplication.processEvents()
        if predicate():
            return True
        QTest.qWait(10)
        elapsed += 10
    return predicate()


def _ui_session(engine):
    session_controller = engine.rootContext().contextProperty(
        "sessionController"
    )
    return session_controller.parent()


def _close_engine(engine) -> None:
    _ui_session(engine).shutdown()
    for root in engine.rootObjects():
        root.close()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application = QGuiApplication.instance()
    if application is not None:
        application.processEvents()


@pytest.mark.parametrize(
    ("change", "value"),
    (
        ("qml_loaded", False),
        ("shutdown_completed", False),
        ("platform_name", "minimal"),
        ("tls_backends", ("openssl",)),
        ("supported_image_formats", ("jpg", "gif", "webp")),
        ("supported_image_formats", ("jpg", "png", "webp")),
        ("supported_image_formats", ("jpg", "png", "gif")),
        ("supported_image_formats", ("png", "gif", "webp")),
    ),
)
def test_gui_smoke_capability_contract_rejects_missing_requirement(
    change,
    value,
):
    values = {
        "qml_loaded": True,
        "shutdown_completed": True,
        "platform_name": "windows",
        "tls_backends": ("schannel",),
        "supported_image_formats": ("jpg", "png", "gif", "webp"),
    }
    values[change] = value

    assert gui_smoke_capabilities_succeed(**values) is False


def test_gui_smoke_capability_contract_accepts_jpeg_and_offscreen():
    assert gui_smoke_capabilities_succeed(
        qml_loaded=True,
        shutdown_completed=True,
        platform_name="offscreen",
        tls_backends=("SCHANNEL", "openssl"),
        supported_image_formats=("JPEG", "PNG", "GIF", "WEBP"),
    ) is True


def test_create_qml_engine_rejects_non_gui_application():
    with pytest.raises(RuntimeError, match="QGuiApplication"):
        create_qml_engine(
            QObject(),
            danbooru_base_url="https://example.invalid",
        )


def test_minimal_qml_window_loads():
    application = QGuiApplication.instance() or QGuiApplication([])
    engine, bridge = create_qml_engine(
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
        "searchButton",
        "globalRankButton",
        "latestButton",
        "queryStatusBar",
        "historyList",
        "refreshButton",
        "retryButton",
        "detailPage",
        "detailImageViewer",
    ):
        assert root.findChild(QObject, object_name) is not None
    rating_filter = root.findChild(QObject, "ratingFilter")
    page_size_input = root.findChild(QObject, "pageSizeInput")
    assert rating_filter.property("currentValue") == "all"
    assert page_size_input.property("text") == "200"
    assert root.findChild(QObject, "previousPageButton") is None
    assert root.findChild(QObject, "nextPageButton") is None
    assert engine.rootContext().contextProperty("searchController") is bridge
    controller = bridge.core_controller
    assert bridge.postsModel is controller.posts_model

    ui_session = _ui_session(engine)
    ui_session.shutdown()
    assert bridge.executeStandard() is False
    assert controller.closed is True
    _close_engine(engine)


def test_thumbnail_error_stays_inside_its_gallery_card():
    application = QGuiApplication.instance() or QGuiApplication([])
    engine, bridge = create_qml_engine(
        application,
        danbooru_base_url="https://example.invalid",
    )
    controller = bridge.core_controller
    bridge.postsModel.set_posts(
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
    assert bridge.postsModel.count == 1
    assert controller.executor.snapshot.state == QueryExecutionState.IDLE

    bridge.postsModel.set_posts(
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
    assert bridge.postsModel.count == 1
    assert controller.executor.snapshot.state == QueryExecutionState.IDLE
    _close_engine(engine)


def test_gui_event_loop_starts_and_closes_cleanly():
    application = QGuiApplication.instance() or QGuiApplication([])
    QTimer.singleShot(0, application.quit)

    exit_code = launch_gui(
        application,
        danbooru_base_url="https://example.invalid",
    )

    assert exit_code == 0


def test_m2_startup_loads_saved_draft_without_network(tmp_path):
    application = QGuiApplication.instance() or QGuiApplication([])
    runtime = initialize_runtime(tmp_path)
    repository = HistoryRepository(runtime.database)
    saved = repository.record_accepted_query(
        display_text="cat",
        normalized_tags=("cat",),
        rating="general",
        page_size=40,
        page=1,
        count_tag_usage=True,
    )
    assert saved is not None
    network_manager = _RecordingNetworkManager()

    engine, bridge = create_qml_engine(
        application,
        danbooru_base_url="https://example.invalid",
        network_manager=network_manager,
        runtime=runtime,
    )
    assert engine.rootContext().contextProperty("searchController") is bridge
    session_controller = engine.rootContext().contextProperty(
        "sessionController"
    )

    QTest.qWait(350)
    application.processEvents()
    assert bridge.advancedText == "cat"
    assert session_controller.canRestoreSelected is True
    assert session_controller.historyModel.count == 1
    assert network_manager.requests == []

    _close_engine(engine)


def test_qml_session_shares_one_queue_and_shutdown_is_repeatable():
    application = QGuiApplication.instance() or QGuiApplication([])
    engine, bridge = create_qml_engine(
        application,
        danbooru_base_url="https://example.invalid",
    )
    query_controller = bridge.core_controller
    tag_controller = engine.rootContext().contextProperty(
        "tagSuggestionController"
    )
    detail_controller = engine.rootContext().contextProperty("detailController")
    session_controller = engine.rootContext().contextProperty("sessionController")
    ui_session = session_controller.parent()
    executor = query_controller.executor
    shared_queue = executor._task_queue

    assert tag_controller._task_queue is shared_queue
    assert ui_session.pool_browser._pools._queue is shared_queue
    assert ui_session.pool_browser._memberships._queue is shared_queue
    assert ui_session.pool_browser._pages._queue is shared_queue
    assert ui_session.pool_browser._pages._manager is ui_session.network_manager
    assert executor.owns_queue is False
    assert tag_controller._owns_queue is False
    assert executor._network_manager is ui_session.network_manager
    assert tag_controller._network_manager is ui_session.network_manager
    assert bridge.core_controller is query_controller
    assert bridge.postsModel is query_controller.posts_model
    navigation = engine.rootContext().contextProperty("detailNavigation")
    assert navigation._browse_model is query_controller.posts_model
    assert navigation.detail_controller is detail_controller
    assert not hasattr(detail_controller, "_gallery_model")
    assert session_controller._posts_model is query_controller.posts_model
    assert detail_controller.translation_store is tag_controller.translation_store
    assert query_controller._tag_store is tag_controller.translation_store

    ui_session.shutdown()
    ui_session.shutdown()
    session_controller.detach_events()
    bridge.detach()

    assert shared_queue.is_shutdown is True
    assert shared_queue.has_work is False
    assert ui_session.shutdown_completed is True
    _close_engine(engine)


def test_coordinated_shutdown_continues_after_final_save_failure_and_reentry(
    tmp_path,
    monkeypatch,
):
    application = QGuiApplication.instance() or QGuiApplication([])
    runtime = initialize_runtime(tmp_path)
    network_manager = _RecordingNetworkManager()
    engine, bridge = create_qml_engine(
        application,
        danbooru_base_url="https://example.invalid",
        network_manager=network_manager,
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
    query_controller = bridge.core_controller
    assert bridge is ui_session.query_bridge
    events: list[str] = []
    accepted_queries = []
    result_events = []
    query_controller.queryAccepted.connect(accepted_queries.append)
    query_controller.resultSummaryChanged.connect(result_events.append)

    assert bridge.applyAdvancedText("cat") is True
    assert bridge.executeStandard() is True
    assert _wait_until(
        lambda: len(network_manager.requests) == 1
        and len(accepted_queries) == 1
    )

    def fail_final_save(**_kwargs):
        raise HistoryError("simulated final save failure")

    monkeypatch.setattr(
        ui_session.history_repository,
        "save_browser_state",
        fail_final_save,
    )
    for name, owner, attribute in (
        ("cleanup-stop", ui_session.cache_cleanup, "shutdown"),
        ("downloads-ui", ui_session.download_ui, "shutdown"),
        ("downloads-stop", ui_session.download_manager, "begin_shutdown"),
        ("checkpoints-stop", ui_session.query_checkpoints, "begin_shutdown"),
        ("downloads-save", ui_session.download_manager, "shutdown"),
        ("transfer-stop", ui_session.favorite_transfer, "begin_shutdown"),
        ("combinations", ui_session.artist_combinations, "shutdown"),
        ("saved-queries", ui_session.saved_queries, "shutdown"),
        ("transfer-wait", ui_session.favorite_transfer, "shutdown"),
        ("checkpoints-save", ui_session.query_checkpoints, "finish_shutdown"),
        ("collection", ui_session.favorite_collection, "shutdown"),
        ("favorites", ui_session.favorite_controller, "shutdown"),
        ("selection", ui_session.gallery_selection, "shutdown"),
        ("detail-favorites", ui_session.detail_favorites, "shutdown"),
        ("bridge-closing", bridge, "beginClosing"),
        ("query", query_controller, "shutdown"),
        ("session", session_controller, "begin_shutdown"),
        ("session-detach", session_controller, "detach_events"),
        ("bridge-detach", bridge, "detach"),
        ("detail", detail, "shutdown"),
        ("tags-closing", tags, "begin_shutdown"),
        ("tags", tags, "shutdown"),
        ("images", images, "shutdown"),
        ("cache-protection", ui_session.cache_protection, "shutdown"),
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
        assert bridge.executeLatest() is False
        tags.updateInput("cat", 3)

    session_controller.noticeTextChanged.connect(reenter_shutdown_and_request)
    assert queue.has_work is True

    ui_session.shutdown()

    assert reentry_count == 1
    assert events == [
        "cleanup-stop",
        "downloads-ui",
        "downloads-stop",
        "checkpoints-stop",
        "transfer-stop",
        "combinations",
        "saved-queries",
        "collection",
        "favorites",
        "selection",
        "detail-favorites",
        "bridge-closing",
        "transfer-wait",
        "checkpoints-save",
        "downloads-save",
        "query",
        "session",
        "session-detach",
        "bridge-detach",
        "detail",
        "tags-closing",
        "tags",
        "images",
        "cache-protection",
        "queue",
    ]
    assert "simulated final save failure" in session_controller.noticeText
    assert detail._shutdown is True
    assert query_controller.closed is True
    assert query_controller.executor.closed is True
    assert bridge.closing is True
    assert bridge.detached is True
    assert bridge.queryState == QueryExecutionState.PARTIAL_CANCELLED.value
    assert session_controller.events_detached is True
    assert tags._shutdown is True
    assert images.closed is True
    assert queue.is_shutdown is True
    assert queue.has_work is False
    assert len(accepted_queries) == 1
    assert sum(
        event.summary.state == QueryExecutionState.PARTIAL_CANCELLED
        for event in result_events
    ) == 1
    assert network_manager.replies[0].abort_count == 1
    stored = ui_session.history_repository.latest_query()
    assert stored is not None
    assert stored.result_summary is not None
    assert stored.result_summary.state == QueryExecutionState.PARTIAL_CANCELLED

    _close_engine(engine)


def test_active_import_finishes_before_m2_cancel_and_final_history_save(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from threading import Event, Thread, get_ident
    from PySide6.QtCore import QUrl
    from danbooru_multi_search.core.favorites import FavoriteCandidate
    from danbooru_multi_search.storage.database import Database
    from danbooru_multi_search.storage.favorites import FavoriteRepository
    from danbooru_multi_search.storage.favorite_transfer import FavoriteTransferService

    application = QGuiApplication.instance() or QGuiApplication([])
    runtime = initialize_runtime(tmp_path / "application")
    source_db = Database(tmp_path / "source.sqlite3")
    source_db.initialize()
    source_repository = FavoriteRepository(source_db)
    source_repository.add_favorite(FavoriteCandidate.for_pool(9, "source"))
    source_path = tmp_path / "source.json"
    FavoriteTransferService(source_repository).export_file(source_path)
    manager = _RecordingNetworkManager()
    engine, bridge = create_qml_engine(application, danbooru_base_url="https://example.invalid", runtime=runtime, network_manager=manager)
    session = bridge.parent()
    entered, release = Event(), Event()
    gui = get_ident()
    original_write = FavoriteRepository._write_connection
    @contextmanager
    def hold_commit(repository):
        with original_write(repository) as connection:
            yield connection
            if get_ident() != gui:
                entered.set()
                assert release.wait(5)
    monkeypatch.setattr(FavoriteRepository, "_write_connection", hold_commit)
    transfer = session.favorite_transfer
    releaser = None
    try:
        assert bridge.applyAdvancedText("cat") and bridge.executeStandard()
        assert _wait_until(lambda: len(manager.requests) == 1)
        token = transfer.beginImportSelection()
        assert transfer.acceptImportFile(QUrl.fromLocalFile(str(source_path)), token)
        assert _wait_until(lambda: not transfer.busy)
        assert transfer.confirmImport(transfer.preview["token"])
        assert _wait_until(entered.is_set)
        original_query_shutdown = session.query_controller.shutdown
        checked = []
        def query_shutdown():
            assert transfer.shutdown_completed
            assert len(FavoriteRepository(runtime.database).list_favorites()) == 1
            checked.append(True)
            original_query_shutdown()
        monkeypatch.setattr(session.query_controller, "shutdown", query_shutdown)
        releaser = Thread(target=lambda: (transfer._closing_event.wait(5), release.set()))
        releaser.start()
        session.shutdown()
        releaser.join()
        assert checked == [True]
        assert session.shutdown_completed
        stored = session.history_repository.latest_query()
        assert stored.result_summary.state == QueryExecutionState.PARTIAL_CANCELLED
        assert manager.replies[0].abort_count == 1
    finally:
        release.set()
        if releaser is not None:
            releaser.join()
        _close_engine(engine)


def test_active_download_shutdown_preserves_shared_query_and_final_history(tmp_path):
    from danbooru_multi_search.core.downloads import DownloadTaskState
    application = QGuiApplication.instance() or QGuiApplication([])
    runtime = initialize_runtime(tmp_path)
    network = _RecordingNetworkManager()
    engine, bridge = create_qml_engine(application, danbooru_base_url="https://example.invalid", runtime=runtime, network_manager=network)
    session = bridge.parent()
    try:
        assert bridge.applyAdvancedText("cat") and bridge.executeStandard()
        assert _wait_until(lambda: len(network.requests) == 1)
        work = DanbooruPost(id=321, preview_url=None, large_file_url=None, file_url="https://example.invalid/321.jpg",
            score=0, fav_count=0, rating="g", width=100, height=100, file_ext="jpg")
        batch = session.download_manager.enqueue_download(work, runtime.paths.download_dir)
        assert _wait_until(lambda: len(network.requests) == 2)
        session.download_manager.begin_shutdown()
        assert network.replies[1].abort_count == 1 and network.replies[0].abort_count == 0
        assert not session.query_controller.closed and not session.task_queue.is_shutdown
        session.shutdown()
        assert session.shutdown_completed
        assert network.replies[0].abort_count == 1 and network.replies[1].abort_count == 1
        stored = session.history_repository.latest_query()
        assert stored.result_summary.state == QueryExecutionState.PARTIAL_CANCELLED
        assert session.download_manager.snapshot(batch.batch_id).items[0].state is DownloadTaskState.AWAITING_RESUME
        network.replies[1].readyRead.emit()
        network.replies[1].finished.emit()
        assert not list(runtime.paths.download_dir.glob("*.jpg"))
    finally:
        _close_engine(engine)
