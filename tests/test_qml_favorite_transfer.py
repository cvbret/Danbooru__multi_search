from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import QUrl, QPointF

import danbooru_multi_search.app.favorite_transfer as transfer_app
from danbooru_multi_search.core.bootstrap import initialize_runtime
from danbooru_multi_search.core.favorites import FavoriteCandidate
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.favorites import FavoriteRepository
from danbooru_multi_search.storage.favorite_transfer import (
    FavoriteTransferDocument, FavoriteTransferService, FavoriteTransferError,
    read_favorite_transfer_document, write_favorite_transfer_document,
)
from tests.m2_qml_harness import create_m2_qml_test_engine, close_m2_qml_test_engine
from tests.test_favorite_controller import APPLICATION, post
from tests.test_qml_favorites import NoNetwork, item, click, wait


def start(tmp_path, *, narrow=False):
    runtime = initialize_runtime(tmp_path / "application")
    repository = FavoriteRepository(runtime.database)
    folder = repository.create_folder("Existing")
    repository.add_favorite(FavoriteCandidate.for_post(post(1)), folder_id=folder.id)
    source_db = Database(tmp_path / "source.sqlite3")
    source_db.initialize()
    source = FavoriteRepository(source_db)
    source.add_favorite(FavoriteCandidate.for_pool(9, "Source"))
    path = tmp_path / "source.json"
    FavoriteTransferService(source).export_file(path)
    network = NoNetwork()
    engine, bridge = create_m2_qml_test_engine(
        APPLICATION, danbooru_base_url="https://example.invalid", runtime=runtime,
        network_manager=network,
    )
    root = engine.rootObjects()[0]
    if narrow:
        root.setWidth(820)
        root.setHeight(600)
    session = bridge.parent()
    click(root, item(root, "favoritesNavigationButton"))
    click(root, item(root, "selectLoadedFavoritesButton"))
    click(root, item(root, "openFavoriteTransferButton"))
    assert wait(lambda: item(root, "favoriteTransferDialog").property("opened"))
    return engine, root, session, repository, path, network


def choose_import(root, controller, path):
    dialog = item(root, "favoriteImportFileDialog")
    token = controller.beginImportSelection()
    dialog.setProperty("choiceToken", token)
    assert dialog.setProperty("selectedFile", QUrl.fromLocalFile(str(path)))
    dialog.accepted.emit()


def choose_export(root, controller, path):
    dialog = item(root, "favoriteExportFileDialog")
    token = controller.beginExportSelection()
    dialog.setProperty("choiceToken", token)
    assert dialog.setProperty("selectedFile", QUrl.fromLocalFile(str(path)))
    dialog.accepted.emit()


def reveal(root, control):
    viewport = item(root, "favoriteTransferScroll").property("contentItem")
    center = control.mapToScene(QPointF(control.width() / 2, control.height() / 2))
    middle = viewport.mapToScene(QPointF(0, viewport.height() / 2))
    maximum = max(0, viewport.property("contentHeight") - viewport.height())
    viewport.setProperty("contentY", max(0, min(viewport.property("contentY") + center.y() - middle.y(), maximum)))
    APPLICATION.processEvents()


def press(root, name):
    button = item(root, name)
    reveal(root, button)
    click(root, button)


def test_dialog_uses_one_immutable_preview_cancel_then_import_refreshes_collection(tmp_path):
    engine, root, session, repository, source, network = start(tmp_path, narrow=True)
    controller = session.favorite_transfer
    try:
        choose_import(root, controller, source)
        assert wait(lambda: not controller.busy)
        assert controller.canConfirm, controller.message
        original = controller._preview
        assert original.added_favorites == 1
        assert item(root, "previewAddedFavorites").property("text").endswith("1")
        assert item(root, "previewDeletedFavorites").property("text").endswith("0")
        press(root, "cancelFavoriteTransferButton")
        assert not controller.canConfirm
        assert len(repository.list_favorites()) == 1
        press(root, "previewFavoriteImportAgainButton")
        assert wait(lambda: not controller.busy)
        original = controller._preview
        token = controller.preview["token"]
        assert controller._preview is original
        press(root, "confirmFavoriteImportButton")
        assert wait(lambda: not controller.busy)
        assert len(repository.list_favorites()) == 2
        assert not controller.confirmImport(token)
        assert session.favorite_collection.selectedCount == 0
        assert session.favorite_collection.totalCount == 2
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


def test_empty_replace_preview_requires_explicit_confirmation(tmp_path):
    engine, root, session, repository, source, network = start(tmp_path)
    controller = session.favorite_transfer
    empty = tmp_path / "empty.json"
    write_favorite_transfer_document(empty, FavoriteTransferDocument(datetime.now(timezone.utc), (), ()))
    try:
        mode = item(root, "favoriteImportMode")
        mode.setProperty("currentIndex", 1)
        mode.activated.emit(1)
        choose_import(root, controller, empty)
        assert wait(lambda: not controller.busy)
        assert controller.canConfirm
        assert controller._preview.deleted_folders == 1
        assert controller._preview.deleted_favorites == 1
        assert item(root, "confirmFavoriteImportButton").property("text") == "确认覆盖导入"
        assert len(repository.list_favorites()) == 1
        press(root, "confirmFavoriteImportButton")
        assert wait(lambda: not controller.busy)
        assert repository.list_favorites() == () and repository.list_folders() == ()
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


def test_file_selection_rejection_starts_no_file_or_import_job(tmp_path):
    engine, root, session, repository, source, network = start(tmp_path)
    controller = session.favorite_transfer
    before = repository.capture_collection()
    try:
        for kind, name in (("import", "favoriteImportFileDialog"), ("export", "favoriteExportFileDialog")):
            dialog = item(root, name)
            token = controller.beginImportSelection() if kind == "import" else controller.beginExportSelection()
            dialog.setProperty("choiceToken", token)
            dialog.setProperty("selectedFile", QUrl.fromLocalFile(str(source)))
            dialog.rejected.emit()
            dialog.accepted.emit()  # A delayed acceptance for the cancelled selection is stale.
        assert controller._thread is None
        assert not controller.busy and not controller.canConfirm
        assert repository.capture_collection() == before
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


def test_stale_preview_is_cleared_and_export_preserves_page_selection(tmp_path):
    engine, root, session, repository, source, network = start(tmp_path)
    controller = session.favorite_transfer
    try:
        choose_import(root, controller, source)
        assert wait(lambda: not controller.busy)
        frozen = controller._preview
        token = controller.preview["token"]
        generation = session.favorite_collection.generation
        selected = session.favorite_collection.selectedCount
        output = tmp_path / "exported.json"
        choose_export(root, controller, output)
        assert wait(lambda: not controller.busy)
        assert len(read_favorite_transfer_document(output).favorites) == 1
        assert controller._preview is frozen and controller.preview["token"] == token
        assert session.favorite_collection.generation == generation
        assert session.favorite_collection.selectedCount == selected
        other = FavoriteRepository(repository._database)
        other.add_favorite(FavoriteCandidate.for_pool(7, "foreign"))
        press(root, "confirmFavoriteImportButton")
        assert wait(lambda: not controller.busy)
        assert controller.errorCode == "stale_preview"
        assert not controller.canConfirm
        assert not item(root, "favoriteImportPreviewCounts").property("visible")
        assert not item(root, "confirmFavoriteImportButton").property("enabled")
        assert len(repository.list_favorites()) == 2
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


def test_export_error_exposes_original_code_and_residual_path(tmp_path, monkeypatch):
    engine, root, session, repository, source, network = start(tmp_path)
    controller = session.favorite_transfer
    residual = tmp_path / ".export.json.tmp"
    def fail(*args):
        raise FavoriteTransferError("file_cleanup_error", "cleanup failed", residual_path=residual)
    monkeypatch.setattr(transfer_app, "write_favorite_transfer_bytes", fail)
    try:
        choose_export(root, controller, tmp_path / "export.json")
        assert wait(lambda: not controller.busy)
        assert item(root, "favoriteTransferErrorCode").property("text") == "file_cleanup_error"
        assert str(residual) in item(root, "favoriteTransferResidualPath").property("text")
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)
