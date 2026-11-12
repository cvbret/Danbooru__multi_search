from __future__ import annotations

from dataclasses import replace

import pytest
from PySide6.QtCore import QObject, QPoint, QPointF, QUrl, Qt, QMetaObject, Q_ARG
from PySide6.QtTest import QTest

from danbooru_multi_search.core.bootstrap import initialize_runtime
from danbooru_multi_search.core.downloads import DownloadTaskState
from danbooru_multi_search.core.settings import AppSettings, SettingsStore
from danbooru_multi_search.storage.downloads import DownloadRepository
from tests.m2_qml_harness import create_m2_qml_test_engine, close_m2_qml_test_engine
from tests.test_download_manager import Network, works
from tests.test_download_repository import request
from tests.test_favorite_controller import APPLICATION, IMAGE
from tests.test_qml_favorites import click, wait, invoke
from tests.test_task_queue import ManualClock, ManualScheduler


def item(root, name):
    pending = [root.contentItem()]
    fallback = None
    while pending:
        candidate = pending.pop()
        if candidate.objectName() == name:
            if candidate.isVisible():
                return candidate
            fallback = candidate
        pending.extend(candidate.childItems())
    found = fallback if fallback is not None else root.findChild(QObject, name)
    assert found is not None, name
    return found


def start(tmp_path, *, narrow=False, history=0):
    runtime = initialize_runtime(tmp_path)
    repository = DownloadRepository(runtime.database)
    for index in range(history):
        repository.create_batch((request(100 + index),), runtime.paths.download_dir)
    network = Network(ManualScheduler(ManualClock()))
    engine, bridge = create_m2_qml_test_engine(APPLICATION, danbooru_base_url="https://example.invalid", runtime=runtime, network_manager=network)
    root = engine.rootObjects()[0]
    root.setWidth(820 if narrow else 1280)
    root.setHeight(600 if narrow else 820)
    bridge.postsModel.set_posts(tuple(replace(work, preview_url=IMAGE, large_file_url=IMAGE) for work in works()))
    root.requestActivate()
    assert QTest.qWaitForWindowExposed(root)
    assert wait(lambda: item(root, "postDownload_1").isVisible())
    return engine, root, bridge, bridge.parent(), runtime, network


def choose_folder(root, controller, name, path, purpose):
    dialog = item(root, name)
    token = controller.beginDirectorySelection(purpose)
    dialog.setProperty("choiceToken", token)
    # FolderDialog keeps its selectedFolder in the native helper, which is not
    # created in an offline test. Deliver the accepted URL to the real QML handler.
    assert QMetaObject.invokeMethod(dialog, "acceptSelection", Qt.ConnectionType.DirectConnection,
                                  Q_ARG("QVariant", QUrl.fromLocalFile(str(path))))
    assert not controller.errorText, (controller.errorText, controller.errorDetails)


def press_dialog(root, name):
    control = item(root, name)
    viewport = item(root, "downloadRequestScroll").property("contentItem")
    center = control.mapToScene(QPointF(control.width() / 2, control.height() / 2))
    middle = viewport.mapToScene(QPointF(0, viewport.height() / 2))
    maximum = max(0, viewport.property("contentHeight") - viewport.height())
    viewport.setProperty("contentY", max(0, min(viewport.property("contentY") + center.y() - middle.y(), maximum)))
    APPLICATION.processEvents()
    click(root, control)


def press_item_action(root, name):
    control = item(root, name)
    viewport = item(root, "downloadItemList")
    center = control.mapToScene(QPointF(control.width() / 2, control.height() / 2))
    middle = viewport.mapToScene(QPointF(0, viewport.height() / 2))
    origin = viewport.property("originY")
    maximum = max(origin, origin + viewport.property("contentHeight") - viewport.height())
    viewport.setProperty("contentY", max(origin, min(viewport.property("contentY") + center.y() - middle.y(), maximum)))
    APPLICATION.processEvents()
    click(root, control)


def test_card_click_confirm_cancel_and_batch_download_preserve_gallery_and_history(tmp_path):
    engine, root, bridge, session, runtime, network = start(tmp_path)
    try:
        original_posts, original_revision = bridge.postsModel.posts, bridge.postsModel.revision
        click(root, item(root, "postDownload_1"))
        assert wait(lambda: item(root, "downloadRequestDialog").property("opened"))
        assert not session.detail_controller.isOpen
        press_dialog(root, "cancelDownloadRequestButton")
        assert session.download_manager._repository.list_batches() == () and network.requests == []
        click(root, item(root, "postSelect_3"))
        click(root, item(root, "postSelect_1"))
        click(root, item(root, "downloadSelectedPostsButton"))
        assert wait(lambda: item(root, "downloadRequestDialog").property("opened"))
        folder = tmp_path / "本次 文件"
        folder.mkdir()
        choose_folder(root, session.download_ui, "downloadDestinationFolderDialog", folder, "request")
        policy = item(root, "downloadConflictPolicy")
        policy.setProperty("currentIndex", 2)
        press_dialog(root, "confirmDownloadRequestButton")
        assert wait(lambda: len(network.requests) == 2)
        batch = session.download_manager.snapshot(session.download_manager._repository.list_batches()[0])
        assert [entry.request.post_id for entry in batch.items] == [1, 3]
        assert all(entry.directory == folder and entry.policy.value == "auto_number" for entry in batch.items)
        assert bridge.postsModel.posts is original_posts and bridge.postsModel.revision == original_revision
        assert session.gallery_selection.selectedCount == 2
        assert session.history_repository.latest_query() is None
        click(root, item(root, "downloadsNavigationButton"))
        assert wait(lambda: session.download_ui.itemsModel.count == 2)
        network.replies[0].send(b"first complete")
        assert wait(lambda: session.download_ui.itemsModel.items[0].state is DownloadTaskState.COMPLETED)
        click(root, item(root, "favoritesNavigationButton"))
        click(root, item(root, "browseNavigationButton"))
        assert session.gallery_selection.selectedCount == 2 and bridge.postsModel.posts is original_posts
    finally:
        close_m2_qml_test_engine(engine)


def test_default_folder_rejection_and_save_use_same_settings_and_restart_value(tmp_path):
    engine, root, bridge, session, runtime, network = start(tmp_path, narrow=True)
    try:
        click(root, item(root, "downloadsNavigationButton"))
        assert item(root, "downloadsEmptyState").isVisible()
        old = session.settings_controller.settings
        dialog = item(root, "defaultDownloadFolderDialog")
        token = session.download_ui.beginDirectorySelection("default")
        dialog.setProperty("choiceToken", token)
        dialog.setProperty("selectedFolder", QUrl.fromLocalFile(str(tmp_path)))
        dialog.rejected.emit()
        dialog.accepted.emit()
        assert session.settings_controller.settings is old
        chosen = tmp_path / "默认 新目录"
        chosen.mkdir()
        choose_folder(root, session.download_ui, "defaultDownloadFolderDialog", chosen, "default")
        saved = SettingsStore(runtime.paths.settings_file).load_or_default(AppSettings())
        assert saved.download_directory == str(chosen)
        assert session.download_ui.defaultDirectory == str(chosen)
        assert network.requests == [] and session.download_manager._repository.list_batches() == ()
    finally:
        close_m2_qml_test_engine(engine)


def test_restored_batches_are_paginated_and_item_actions_capture_generation(tmp_path):
    engine, root, bridge, session, runtime, network = start(tmp_path, narrow=True, history=52)
    try:
        assert network.requests == []
        click(root, item(root, "downloadsNavigationButton"))
        assert session.download_ui.batchesModel.count == 50 and session.download_ui.itemsModel.count == 1
        assert network.requests == []
        selected = session.download_ui.itemsModel.items[0]
        assert selected.state is DownloadTaskState.AWAITING_RESUME
        click(root, item(root, "resumeDownloadBatchButton"))
        assert wait(lambda: len(network.requests) == 1)
        current = session.download_manager._repository.get_item(selected.item_id)
        button = item(root, "cancelDownloadItem_" + selected.item_id)
        center = button.mapToScene(QPointF(button.width() / 2, button.height() / 2))
        QTest.mousePress(root, Qt.MouseButton.LeftButton, pos=QPoint(round(center.x()), round(center.y())))
        network.replies[0].headers["Content-Length"] = b"10"
        network.replies[0].metaDataChanged.emit()
        network.replies[0].payload = b"abc"
        network.replies[0].readyRead.emit()
        APPLICATION.processEvents()
        QTest.mouseRelease(root, Qt.MouseButton.LeftButton, pos=QPoint(round(center.x()), round(center.y())))
        assert wait(lambda: session.download_manager._repository.get_item(selected.item_id).state is DownloadTaskState.CANCELLED)
        assert session.download_manager._repository.get_item(selected.item_id).generation > current.generation
        click(root, item(root, "loadMoreDownloadBatchesButton"))
        assert session.download_ui.batchesModel.count == 52
    finally:
        close_m2_qml_test_engine(engine)


def test_stale_card_press_and_invalid_detail_original_url_do_not_create_download(tmp_path):
    engine, root, bridge, session, runtime, network = start(tmp_path)
    try:
        button = item(root, "postDownload_1")
        center = button.mapToScene(QPointF(button.width() / 2, button.height() / 2))
        QTest.mousePress(root, Qt.MouseButton.LeftButton, pos=QPoint(round(center.x()), round(center.y())))
        bridge.postsModel.set_posts(tuple(reversed(bridge.postsModel.posts)))
        APPLICATION.processEvents()
        QTest.mouseRelease(root, Qt.MouseButton.LeftButton, pos=QPoint(round(center.x()), round(center.y())))
        assert not item(root, "downloadRequestDialog").property("visible")
        assert session.download_manager._repository.list_batches() == ()
        invalid = replace(works(1, 99)[0], preview_url=IMAGE, large_file_url=IMAGE, file_url="invalid-original-address")
        assert session.detail_navigation.open_saved_post(invalid, source_page="favorites")
        assert wait(lambda: item(root, "detailDownloadButton").isVisible())
        click(root, item(root, "detailDownloadButton"))
        notice = item(root, "detailDownloadNotice")
        assert notice.isVisible() and "HTTP" in notice.property("text")
        assert not item(root, "downloadRequestDialog").property("visible")
        assert session.download_manager._repository.list_batches() == () and network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


def test_favorite_snapshot_detail_confirm_downloads_snapshot_without_changing_browse(tmp_path):
    engine, root, bridge, session, runtime, network = start(tmp_path, narrow=True)
    try:
        snapshot = replace(works(1, 900)[0], preview_url=IMAGE, large_file_url=IMAGE)
        previous = bridge.postsModel.posts
        assert session.detail_navigation.open_saved_post(snapshot, source_page="favorites")
        assert wait(lambda: item(root, "detailDownloadButton").isVisible())
        click(root, item(root, "detailDownloadButton"))
        assert wait(lambda: item(root, "downloadRequestDialog").property("opened"))
        press_dialog(root, "confirmDownloadRequestButton")
        assert wait(lambda: len(network.requests) == 1)
        assert network.requests[0].url().path() == "/900.jpg"
        assert bridge.postsModel.posts is previous and session.history_repository.latest_query() is None
        assert session.detail_controller.current_post is snapshot
    finally:
        close_m2_qml_test_engine(engine)


def test_download_page_failure_retry_and_batch_cancel_use_real_actions(tmp_path):
    engine, root, bridge, session, runtime, network = start(tmp_path, narrow=True)
    try:
        click(root, item(root, "postDownload_1"))
        assert wait(lambda: item(root, "downloadRequestDialog").property("opened"))
        press_dialog(root, "confirmDownloadRequestButton")
        assert wait(lambda: len(network.requests) == 1)
        click(root, item(root, "downloadsNavigationButton"))
        key = session.download_ui.itemsModel.items[0].item_id
        network.replies[0].send(b"not found", status=404)
        assert wait(lambda: session.download_ui.itemsModel.items[0].state is DownloadTaskState.FAILED)
        assert session.download_ui.itemsModel.mapping(0)["reasonText"] == "服务器返回错误"
        press_item_action(root, "retryDownloadItem_" + key)
        assert wait(lambda: len(network.requests) == 2)
        click(root, item(root, "cancelDownloadBatchButton"))
        assert wait(lambda: session.download_ui.itemsModel.items[0].state is DownloadTaskState.CANCELLED)
        assert network.replies[1].abort_count == 1
        assert session.download_ui.errorText == ""
    finally:
        close_m2_qml_test_engine(engine)
