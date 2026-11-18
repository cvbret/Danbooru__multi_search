from __future__ import annotations

from contextlib import contextmanager
from threading import Event, Thread, get_ident

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtCore import Qt

import danbooru_multi_search.app.favorite_transfer as transfer_app
from danbooru_multi_search.core.favorites import FavoriteCandidate, FavoriteIdentity, FavoriteObjectType
from danbooru_multi_search.storage.favorites import FavoriteRepository
from danbooru_multi_search.storage.favorite_transfer import FavoriteTransferService
from danbooru_multi_search.ui.favorite_transfer import FavoriteTransferController
from tests.test_favorite_controller import APPLICATION, post, controller_for
from tests.test_favorite_transfer_service import source_file
from tests.test_qml_favorites import wait


def setup_transfer(tmp_path):
    source, target = source_file(tmp_path)
    shared = controller_for(FavoriteRepository(target))
    controller = FavoriteTransferController(target.path, shared, shared._writes.access)
    return source, target, shared, controller


def preview(controller, source):
    token = controller.beginImportSelection()
    assert controller.acceptImportFile(QUrl.fromLocalFile(str(source)), token)
    assert wait(lambda: not controller.busy)
    assert controller.canConfirm, controller.message


def test_every_phase_is_off_gui_thread_with_one_reused_thread_and_independent_repository(tmp_path, monkeypatch):
    source, target, shared, controller = setup_transfer(tmp_path)
    gui = get_ident()
    phases = []
    ui_notifications = []
    controller.changed.connect(lambda: ui_notifications.append(get_ident()))
    for name, owner, attribute in (
        ("read", transfer_app, "read_favorite_transfer_bytes"), ("decode", transfer_app, "decode_favorite_transfer_document"),
        ("preview", FavoriteTransferService, "preview_import"),
        ("snapshot", FavoriteTransferService, "create_export_document"),
        ("encode", transfer_app, "encode_favorite_transfer_document"),
        ("write", transfer_app, "write_favorite_transfer_bytes"), ("apply", FavoriteTransferService, "apply_import"),
    ):
        original = getattr(owner, attribute)
        def wrapped(*args, _name=name, _original=original, **kwargs):
            phases.append((_name, get_ident()))
            if isinstance(args[0], FavoriteTransferService):
                assert args[0]._repository is not shared._repository
                assert args[0]._repository._lock is not shared._repository._lock
            return _original(*args, **kwargs)
        monkeypatch.setattr(owner, attribute, wrapped)
    try:
        preview(controller, source)
        original_preview = controller._preview
        original_token = controller.preview["token"]
        thread = controller._thread
        destination = tmp_path / "output.json"
        choice = controller.beginExportSelection()
        assert controller.acceptExportFile(QUrl.fromLocalFile(str(destination)), choice)
        assert wait(lambda: not controller.busy)
        assert destination.is_file()
        assert controller._preview is original_preview
        assert controller.preview["token"] == original_token
        assert controller.confirmImport(original_token)
        assert wait(lambda: not controller.busy)
        assert len(FavoriteRepository(target).list_favorites()) == 1
        assert controller._thread is thread
        assert {name for name, _ in phases} == {"read", "decode", "preview", "snapshot", "encode", "write", "apply"}
        assert len({identifier for _, identifier in phases}) == 1
        assert all(identifier != gui for _, identifier in phases)
        assert set(ui_notifications) == {gui}
    finally:
        controller.shutdown()


def test_preview_and_export_do_not_publish_collection_and_cancelled_choice_starts_nothing(tmp_path):
    source, target, shared, controller = setup_transfer(tmp_path)
    writes = []
    shared.collectionChanged.connect(lambda: writes.append(True))
    try:
        choice = controller.beginImportSelection()
        controller.cancelSelection(choice)
        assert not controller.acceptImportFile(QUrl.fromLocalFile(str(source)), choice)
        assert controller._thread is None
        preview(controller, source)
        old_token = controller.preview["token"]
        controller.setImportMode("replace")
        assert not controller.canConfirm
        assert not controller.confirmImport(old_token)
        assert controller.previewAgain()
        assert wait(lambda: not controller.busy)
        assert controller.canConfirm
        controller.cancel()
        assert not controller.canConfirm
        choice = controller.beginExportSelection()
        destination = tmp_path / "empty.json"
        assert controller.acceptExportFile(QUrl.fromLocalFile(str(destination)), choice)
        assert wait(lambda: not controller.busy)
        assert writes == []
        assert FavoriteRepository(target).capture_collection().revision == 0
    finally:
        controller.shutdown()


def test_import_publishes_complete_observed_state_before_releasing_write_reservation(tmp_path):
    source, target, shared, controller = setup_transfer(tmp_path)
    old = shared._repository.add_favorite(FavoriteCandidate.for_post(post(2)))
    shared.ensure((old.identity,))
    events = []
    try:
        controller.setImportMode("replace")
        preview(controller, source)
        shared.collectionChanged.connect(lambda: events.append((shared.busy, shared.state(old.identity)["saved"], shared.set_post(post(3), True, guard=lambda: True))))
        assert controller.confirmImport(controller.preview["token"])
        assert shared.busy
        assert wait(lambda: not controller.busy)
        assert events == [(True, False, False)]
        assert not shared.busy
        assert not shared.state(old.identity)["saved"]
        assert shared.state(FavoriteIdentity(FavoriteObjectType.POOL, "1"))["saved"]
        assert shared._repository.get_by_identity(FavoriteCandidate.for_post(post(3)).identity) is None
    finally:
        controller.shutdown()


def test_foreign_revision_failure_removes_executable_preview(tmp_path):
    source, target, shared, controller = setup_transfer(tmp_path)
    try:
        preview(controller, source)
        foreign = FavoriteRepository(target)
        original = foreign.add_favorite(FavoriteCandidate.for_pool(9, "foreign"))
        assert controller.confirmImport(controller.preview["token"])
        assert wait(lambda: not controller.busy)
        assert controller.errorCode == "stale_preview"
        assert not controller.canConfirm
        assert controller.preview["token"] == ""
        assert foreign.get_favorite(original.id) == original
        assert not shared.busy
    finally:
        controller.shutdown()


def test_background_read_lock_does_not_block_gui_repository(tmp_path, monkeypatch):
    source, target, shared, controller = setup_transfer(tmp_path)
    entered, release = Event(), Event()
    original = FavoriteRepository.capture_collection
    gui = get_ident()
    def hold_own_lock(repository):
        if get_ident() == gui:
            return original(repository)
        assert repository is not shared._repository
        with repository._lock:
            entered.set()
            assert release.wait(5)
            return original(repository)
    monkeypatch.setattr(FavoriteRepository, "capture_collection", hold_own_lock)
    try:
        choice = controller.beginImportSelection()
        assert controller.acceptImportFile(QUrl.fromLocalFile(str(source)), choice)
        assert wait(entered.is_set)
        assert shared._repository.list_favorites() == ()
        assert not release.is_set()
        release.set()
        assert wait(lambda: not controller.busy)
        assert controller.canConfirm
    finally:
        release.set()
        controller.shutdown()


def test_cancel_after_read_never_starts_decode_or_import(tmp_path, monkeypatch):
    source, target, shared, controller = setup_transfer(tmp_path)
    entered, release = Event(), Event()
    original = transfer_app.read_favorite_transfer_bytes
    decoded = []
    def read(path):
        value = original(path)
        entered.set()
        assert release.wait(5)
        return value
    monkeypatch.setattr(transfer_app, "read_favorite_transfer_bytes", read)
    monkeypatch.setattr(transfer_app, "decode_favorite_transfer_document", lambda value: decoded.append(value))
    try:
        choice = controller.beginImportSelection()
        assert controller.acceptImportFile(QUrl.fromLocalFile(str(source)), choice)
        assert wait(entered.is_set)
        assert controller.cancel()
        release.set()
        assert wait(lambda: not controller.busy)
        assert decoded == []
        assert not controller.canConfirm
        assert FavoriteRepository(target).capture_collection().revision == 0
    finally:
        release.set()
        controller.shutdown()


@pytest.mark.parametrize("stage", ["encode", "atomic_write"])
def test_close_waits_without_starting_new_steps_or_accepting_late_ui_results(tmp_path, monkeypatch, stage):
    source, target, shared, controller = setup_transfer(tmp_path)
    entered, release = Event(), Event()
    owner, attribute = (transfer_app, "encode_favorite_transfer_document") if stage == "encode" else (transfer_app, "write_favorite_transfer_bytes")
    original = getattr(owner, attribute)
    def pause(*args, **kwargs):
        if stage == "encode":
            value = original(*args, **kwargs)
            entered.set()
            assert release.wait(5)
            return value
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(owner, attribute, pause)
    destination = tmp_path / "output.json"
    notifications = []
    controller.changed.connect(lambda: notifications.append(True))
    try:
        choice = controller.beginExportSelection()
        assert controller.acceptExportFile(QUrl.fromLocalFile(str(destination)), choice)
        assert wait(entered.is_set)
        controller.begin_shutdown()
        count = len(notifications)
        release.set()
        controller.shutdown()
        APPLICATION.processEvents()
        assert controller.shutdown_completed
        assert len(notifications) == count
        assert destination.exists() is (stage == "atomic_write")
        assert not controller.beginImportSelection()
    finally:
        release.set()
        controller.shutdown()


def test_close_during_started_import_waits_for_commit_and_suppresses_publication(tmp_path, monkeypatch):
    source, target, shared, controller = setup_transfer(tmp_path)
    entered, release = Event(), Event()
    original = FavoriteRepository._write_connection
    gui = get_ident()
    @contextmanager
    def held_transaction(repository):
        with original(repository) as connection:
            yield connection
            if get_ident() != gui:
                entered.set()
                assert release.wait(5)
    monkeypatch.setattr(FavoriteRepository, "_write_connection", held_transaction)
    writes = []
    shared.collectionChanged.connect(lambda: writes.append(True))
    try:
        preview(controller, source)
        assert controller.confirmImport(controller.preview["token"])
        assert wait(entered.is_set)
        assert not shared.set_post(post(4), True, guard=lambda: True)
        controller.begin_shutdown()
        shared.shutdown()
        releaser = Thread(target=lambda: (controller._closing_event.wait(5), release.set()))
        releaser.start()
        controller.shutdown()
        releaser.join()
        APPLICATION.processEvents()
        assert controller.shutdown_completed
        assert writes == []
        assert len(FavoriteRepository(target).list_favorites()) == 1
        assert shared._writes.access.external_owner is None
    finally:
        release.set()
        controller.shutdown()


def test_import_invalidates_collection_confirmation_without_gui_identity_reads(tmp_path, monkeypatch):
    from danbooru_multi_search.ui.details import DanbooruDetailController
    from danbooru_multi_search.ui.gallery import DanbooruPostListModel
    from danbooru_multi_search.danbooru.tag_translation import TagTranslationStore
    from danbooru_multi_search.ui.detail_navigation import DetailNavigationController
    from danbooru_multi_search.ui.favorite_collection import FavoriteCollectionController

    source, target, shared, controller = setup_transfer(tmp_path)
    old = shared._repository.add_favorite(FavoriteCandidate.for_post(post(2)))
    navigation = DetailNavigationController(
        DanbooruDetailController("https://example.invalid", TagTranslationStore()),
        DanbooruPostListModel(),
    )
    collection = FavoriteCollectionController(shared._repository, shared, navigation)
    collection.setActive(True)
    collection.selectLoaded()
    assert collection.prepareDelete(collection.selectionRevision, collection.generation)
    old_token = collection.confirmation["token"]
    try:
        controller.setImportMode("replace")
        preview(controller, source)
        def forbid_gui_identity_read(*args):
            raise AssertionError("complete import snapshot must supply confirmed facts")
        monkeypatch.setattr(shared._repository, "get_by_identity", forbid_gui_identity_read)
        assert controller.confirmImport(controller.preview["token"])
        assert wait(lambda: not controller.busy)
        assert controller.errorCode == ""
        assert collection.selectedCount == 0 and collection.totalCount == 1
        assert not collection.confirm(old_token)
        assert not shared.state(old.identity)["saved"]
    finally:
        controller.shutdown()
        collection.shutdown()


def test_cancel_after_preview_success_is_queued_never_restores_confirmation(tmp_path, monkeypatch):
    import danbooru_multi_search.ui.favorite_transfer as ui_transfer

    source, target, shared, controller = setup_transfer(tmp_path)
    completed, allow_emit, queued = Event(), Event(), Event()
    original = ui_transfer.execute_transfer
    def hold_result(request, cancelled):
        result = original(request, cancelled)
        assert result.status == "success"
        completed.set()
        assert allow_emit.wait(5)
        return result
    monkeypatch.setattr(ui_transfer, "execute_transfer", hold_result)
    try:
        choice = controller.beginImportSelection()
        assert controller.acceptImportFile(QUrl.fromLocalFile(str(source)), choice)
        controller._worker.completed.connect(lambda _result: queued.set(), Qt.ConnectionType.DirectConnection)
        assert completed.wait(5)  # Keep the GUI queue unprocessed until success is queued.
        allow_emit.set()
        assert queued.wait(5)
        assert controller.cancel()
        assert not controller.canConfirm
        APPLICATION.processEvents()
        assert not controller.busy
        assert controller.preview["token"] == ""
        assert not controller.canConfirm
        assert FavoriteRepository(target).capture_collection().revision == 0
    finally:
        allow_emit.set()
        controller.shutdown()


def test_reentrant_cancel_before_dispatch_is_idempotent_and_starts_no_work(tmp_path):
    source, target, shared, controller = setup_transfer(tmp_path)
    try:
        choice = controller.beginImportSelection()
        controller.changed.connect(controller.cancel)
        assert not controller.acceptImportFile(QUrl.fromLocalFile(str(source)), choice)
        assert not controller.busy and not controller.canConfirm
        assert controller._thread is None
        assert FavoriteRepository(target).capture_collection().revision == 0
    finally:
        controller.shutdown()


def test_shared_reservation_release_does_not_emit_transfer_updates_after_shutdown(tmp_path):
    source, target, shared, controller = setup_transfer(tmp_path)
    notifications = []
    controller.changed.connect(lambda: notifications.append(True))
    assert shared._writes.access.begin_external_write("other")
    controller.shutdown()
    count = len(notifications)
    shared._writes.access.finish_external_write("other")
    assert len(notifications) == count
