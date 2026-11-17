from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QUrl

from danbooru_multi_search.core.downloads import DownloadError, DownloadTaskState
from danbooru_multi_search.core.settings import AppSettings, SettingsController, SettingsError, SettingsStore
from danbooru_multi_search.danbooru.details import DanbooruDetailController
from danbooru_multi_search.danbooru.gallery import DanbooruPostListModel
from danbooru_multi_search.danbooru.tag_translation import TagTranslationStore
from danbooru_multi_search.storage.favorites import FavoriteRepository
from danbooru_multi_search.ui.favorites import FavoriteController
from danbooru_multi_search.ui.gallery_selection import GallerySelectionController
from danbooru_multi_search.ui.downloads import DownloadUiController
from danbooru_multi_search.app.download_submission import DownloadSubmissionService
from tests.test_download_manager import setup, works
from tests.test_download_repository import request


@pytest.fixture
def env(tmp_path):
    repository, manager, queue, scheduler, network = setup(tmp_path)
    default = tmp_path / "默认 下载"
    default.mkdir()
    store = SettingsStore(tmp_path / "settings.json")
    initial = AppSettings(download_directory=str(default))
    store.save(initial)
    settings = SettingsController(store, initial)
    gallery = DanbooruPostListModel()
    favorites = FavoriteController(FavoriteRepository(repository._database))
    selection = GallerySelectionController(gallery, favorites)
    detail = DanbooruDetailController("https://example.invalid", TagTranslationStore())
    ui = DownloadUiController(manager, repository, settings, gallery, selection, detail, default,
        submission=DownloadSubmissionService(settings))
    gallery.set_posts(works())
    yield ui, repository, manager, scheduler, network, gallery, selection, detail, settings
    ui.shutdown()
    manager.shutdown()
    detail.shutdown()


def pending(ui, gallery):
    token = ui.captureGalleryPost(str(gallery.posts[0].id), gallery.revision)
    assert token and ui.openRequest(token)
    return token


def test_snapshot_guard_survives_directory_selection_and_final_confirmation(env, tmp_path):
    ui, repository, _manager, _scheduler, network, gallery, _selection, _detail, settings = env
    source = {"current": True}
    work = gallery.posts[0]
    token = ui.capture_snapshot(work, guard=lambda: source["current"])
    assert token and ui.openRequest(token)
    choice = ui.beginDirectorySelection("request")
    source["current"] = False
    assert not ui.acceptDirectory(QUrl.fromLocalFile(str(tmp_path)), choice)
    assert not ui.confirmRequest(token, "skip", False)
    assert repository.list_batches() == () and network.requests == []
    source["current"] = True
    token = ui.capture_snapshot(work, guard=lambda: source["current"])
    assert ui.confirmRequest(token, "skip", False)
    assert repository.get_batch(repository.list_batches()[0]).items[0].request.post_id == work.id


def seed(repository, tmp_path, count=2):
    return tuple(repository.create_batch((request(index + 10),), tmp_path) for index in range(count))


def test_explicit_link_opens_batch_older_than_visible_first_page_without_loading_all(env, tmp_path, monkeypatch):
    ui, repository, _manager, _scheduler, _network, *_rest = env
    records = seed(repository, tmp_path, 51)
    ui.setActive(True)
    visible = {row.batch_id for row in ui.batchesModel.items}
    target = next(record for record in records if record.batch_id not in visible)
    assert len(visible) == 50 and not ui.selectBatch(target.batch_id)
    def forbidden(*args, **kwargs):
        raise AssertionError('explicit link must not scan history')
    monkeypatch.setattr(repository, 'read_batch_summaries', forbidden)
    assert ui.openPersistedBatch(target.batch_id)
    assert ui.selectedBatchKey == target.batch_id and ui.itemsModel.items == target.items
    assert len(ui.batchesModel.items) == 50


def test_folder_rejection_and_request_cancel_write_no_settings_tasks_or_requests(env, tmp_path):
    ui, repository, _manager, _scheduler, network, gallery, _selection, _detail, settings = env
    previous = settings.settings
    token = pending(ui, gallery)
    choice = ui.beginDirectorySelection("request")
    ui.cancelDirectorySelection(choice)
    assert not ui.acceptDirectory(QUrl.fromLocalFile(str(tmp_path)), choice)
    default_choice = ui.beginDirectorySelection("default")
    ui.cancelDirectorySelection(default_choice)
    assert not ui.acceptDirectory(QUrl.fromLocalFile(str(tmp_path)), default_choice)
    ui.cancelRequest(token)
    assert repository.list_batches() == () and network.requests == [] and settings.settings is previous


def test_request_uses_its_own_directory_and_explicit_policy_without_changing_default(env, tmp_path):
    ui, repository, _manager, _scheduler, network, gallery, selection, _detail, settings = env
    selection.setSelected("3", True, gallery.revision)
    selection.setSelected("1", True, gallery.revision)
    token = ui.captureGallerySelection(selection.revision, gallery.revision)
    chosen = tmp_path / "本次 目录"
    chosen.mkdir()
    assert ui.acceptDirectory(QUrl.fromLocalFile(str(chosen)), ui.beginDirectorySelection("request"))
    assert repository.list_batches() == ()
    previous = settings.settings.download_directory
    assert ui.confirmRequest(token, "auto_number", False)
    batch = repository.get_batch(repository.list_batches()[0])
    assert [item.request.post_id for item in batch.items] == [1, 3]
    assert all(item.directory == chosen and item.policy.value == "auto_number" for item in batch.items)
    assert settings.settings.download_directory == previous and selection.selectedCount == 2
    assert network.requests == []


def test_setting_save_failure_keeps_old_directory_and_creates_no_download(env, tmp_path, monkeypatch):
    ui, repository, _manager, _scheduler, network, gallery, _selection, _detail, settings = env
    previous = settings.settings
    token = pending(ui, gallery)
    assert ui.acceptDirectory(QUrl.fromLocalFile(str(tmp_path)), ui.beginDirectorySelection("request"))
    def fail(_value):
        raise SettingsError("simulated failure")
    monkeypatch.setattr(settings._store, "save", fail)
    assert not ui.confirmRequest(token, "skip", True)
    assert settings.settings is previous and "原设置保持" in ui.errorText
    assert repository.list_batches() == () and network.requests == []
    assert ui.openRequest(token)


def test_source_change_during_default_save_prevents_old_download(env, tmp_path, monkeypatch):
    ui, repository, _manager, _scheduler, network, gallery, _selection, _detail, settings = env
    token = pending(ui, gallery)
    original = settings.set_download_directory
    def save(path):
        result = original(path)
        gallery.set_posts(works(1, 99))
        return result
    monkeypatch.setattr(settings, "set_download_directory", save)
    assert not ui.confirmRequest(token, "overwrite", True)
    assert repository.list_batches() == () and network.requests == []


def test_old_gallery_and_detail_same_id_reopen_actions_are_rejected(env):
    ui, repository, _manager, _scheduler, _network, gallery, _selection, detail, _settings = env
    token = pending(ui, gallery)
    choice = ui.beginDirectorySelection("request")
    gallery.set_posts(tuple(reversed(gallery.posts)))
    assert not ui.openRequest(token) and not ui.confirmRequest(token, "skip", False)
    assert not ui.acceptDirectory(ui.defaultDirectoryUrl, choice)
    work = works(1, 77)[0]
    assert detail.open_post(work)
    detail_token = ui.captureDetailPost("77", ui.detailRevision)
    assert detail.open_post(work)
    assert not ui.confirmRequest(detail_token, "skip", False)
    assert repository.list_batches() == ()


def test_favorite_snapshot_detail_downloads_its_own_post_and_never_uses_preview(env):
    ui, repository, _manager, _scheduler, _network, gallery, _selection, detail, _settings = env
    snapshot = works(1, 2**60 + 1)[0]
    assert detail.open_post(snapshot)
    token = ui.captureDetailPost(str(snapshot.id), ui.detailRevision)
    assert ui.confirmRequest(token, "skip", False)
    batch = repository.get_batch(repository.list_batches()[0])
    assert batch.items[0].request.post_id == snapshot.id != gallery.posts[0].id
    missing = replace(snapshot, file_url=None, preview_url="https://example.invalid/preview.jpg")
    detail.open_post(missing)
    assert not ui.detailDownloadAvailable
    assert not ui.captureDetailPost(str(missing.id), ui.detailRevision)
    assert "原文件地址" in ui.errorText and len(repository.list_batches()) == 1


def test_more_than_one_thousand_selected_works_is_explicitly_rejected(env, monkeypatch):
    ui, repository, _manager, _scheduler, _network, gallery, selection, _detail, _settings = env
    monkeypatch.setattr(selection._favorites, "ensure", lambda *_args, **_kwargs: None)
    gallery.set_posts(works(1001))
    for work in gallery.posts:
        selection.setSelected(str(work.id), True, gallery.revision)
    assert not ui.captureGallerySelection(selection.revision, gallery.revision)
    assert "1,000" in ui.errorText and repository.list_batches() == ()


def test_page_reads_paginated_summaries_and_only_selected_batch_items(env, tmp_path, monkeypatch):
    ui, repository, _manager, _scheduler, network, _gallery, _selection, _detail, _settings = env
    batches = seed(repository, tmp_path, 3)
    calls = []
    original = repository.get_batch
    def get(key):
        calls.append(key)
        return original(key)
    monkeypatch.setattr(repository, "get_batch", get)
    ui.PAGE_SIZE = 2
    ui.setActive(True)
    assert ui.batchesModel.count == 2 and ui.itemsModel.count == 1 and len(calls) == 1
    assert ui.loadMoreBatches() and ui.batchesModel.count == 3 and len(calls) == 1
    assert network.requests == []


@pytest.mark.parametrize("phase", ["batches", "items", "more"])
def test_synchronous_model_notification_shutdown_emits_no_late_ui_updates(env, tmp_path, phase):
    ui, repository, _manager, _scheduler, _network, _gallery, _selection, _detail, _settings = env
    seed(repository, tmp_path, 2)
    ui.PAGE_SIZE = 1
    if phase == "more":
        ui.setActive(True)
    model = ui.itemsModel if phase == "items" else ui.batchesModel
    model.changed.connect(ui.shutdown)
    seen = []
    ui.changed.connect(lambda: seen.append(True))
    if phase == "more":
        assert not ui.loadMoreBatches()
    else:
        ui.setActive(True)
    assert ui._closed and seen == []


def test_late_batch_read_and_other_batch_progress_do_not_replace_current_items(env, tmp_path, monkeypatch):
    ui, repository, manager, _scheduler, _network, _gallery, _selection, _detail, _settings = env
    first, second = seed(repository, tmp_path)
    ui.setActive(True)
    original = repository.get_batch
    entered = False
    def get(key):
        nonlocal entered
        if key == first.batch_id and not entered:
            entered = True
            ui.selectBatch(second.batch_id)
        return original(key)
    monkeypatch.setattr(repository, "get_batch", get)
    assert not ui.selectBatch(first.batch_id)
    assert ui.selectedBatchKey == second.batch_id and ui.itemsModel.items == second.items
    manager.batchChanged.emit(first)
    assert ui.selectedBatchKey == second.batch_id and ui.itemsModel.items == second.items


@pytest.mark.parametrize("kind", ["item", "batch"])
def test_successful_terminal_action_notifies_that_previous_error_was_cleared(env, tmp_path, kind):
    ui, repository, _manager, _scheduler, _network, _gallery, _selection, _detail, _settings = env
    batch = seed(repository, tmp_path, 1)[0]
    ui.setActive(True)
    ui._fail(DownloadError("stale_download", "old error"))
    messages = []
    ui.changed.connect(lambda: messages.append(ui.errorText))
    if kind == "item":
        assert ui.cancelItem(batch.items[0].item_id, "0")
    else:
        assert ui.cancelBatch(ui.captureBatchAction(batch.batch_id, "cancel"))
    assert messages[-1] == "" and ui.errorText == ""


def test_missing_saved_directory_is_created_only_after_explicit_confirmation(env, tmp_path):
    ui, repository, _manager, scheduler, network, gallery, _selection, _detail, settings = env
    destination = tmp_path / "saved-directory-removed"
    settings.set_download_directory(destination)
    token = pending(ui, gallery)
    assert not destination.exists() and repository.list_batches() == ()
    assert ui.confirmRequest(token, "skip", False)
    assert destination.is_dir()
    scheduler.run_ready()
    assert len(network.requests) == 1


@pytest.mark.parametrize("kind", ["item", "batch"])
def test_failed_command_keeps_the_actual_manager_error_details(env, tmp_path, monkeypatch, kind):
    ui, repository, _manager, _scheduler, _network, _gallery, _selection, _detail, _settings = env
    batch = seed(repository, tmp_path, 1)[0]
    ui.setActive(True)
    def fail(*args, **kwargs):
        raise DownloadError("storage_error", "specific database write failure")
    monkeypatch.setattr(repository, "cancel" if kind == "item" else "cancel_batch", fail)
    if kind == "item":
        assert not ui.cancelItem(batch.items[0].item_id, "0")
    else:
        assert not ui.cancelBatch(ui.captureBatchAction(batch.batch_id, "cancel"))
    assert ui.errorDetails == "specific database write failure"
    assert "详细原因" in ui.errorText


def test_guarded_page_resume_recovers_a_storage_halt_without_replacing_committed_file(env, tmp_path, monkeypatch):
    ui, repository, manager, scheduler, network, gallery, _selection, _detail, _settings = env
    token = pending(ui, gallery)
    assert ui.confirmRequest(token, "skip", False)
    ui.setActive(True)
    original = repository.finish
    def fail(item_id, generation, state, **kwargs):
        if state is DownloadTaskState.COMPLETED:
            raise DownloadError("storage_error", "completion write failed")
        return original(item_id, generation, state, **kwargs)
    monkeypatch.setattr(repository, "finish", fail)
    scheduler.run_ready()
    network.replies[0].send(b"committed")
    key = ui.selectedBatchKey
    file_path = Path(ui.defaultDirectory) / "1.jpg"
    assert file_path.read_bytes() == b"committed" and ui.canResumeBatch
    monkeypatch.setattr(repository, "finish", original)
    assert ui.resumeBatch(ui.captureBatchAction(key, "resume"))
    assert repository.get_batch(key).counts["skipped"] == 1
    assert file_path.read_bytes() == b"committed"


def test_load_more_discards_old_page_when_a_read_callback_changes_selected_batch(env, tmp_path, monkeypatch):
    ui, repository, _manager, _scheduler, _network, _gallery, _selection, _detail, _settings = env
    seed(repository, tmp_path, 3)
    ui.PAGE_SIZE = 2
    ui.setActive(True)
    target = ui.batchesModel.items[1].batch_id
    original = repository.read_batch_summaries
    def read(*, limit, offset=0):
        if offset:
            ui.selectBatch(target)
        return original(limit=limit, offset=offset)
    monkeypatch.setattr(repository, "read_batch_summaries", read)
    assert not ui.loadMoreBatches()
    assert ui.batchesModel.count == 2 and ui.selectedBatchKey == target
    assert ui.itemsModel.items == repository.get_batch(target).items


def test_old_execution_snapshot_cannot_replace_selected_items_or_summary(env, tmp_path):
    ui, repository, manager, _scheduler, _network, _gallery, _selection, _detail, _settings = env
    original = seed(repository, tmp_path, 1)[0]
    ui.setActive(True)
    cancelled = repository.cancel_batch(original.batch_id)
    manager.batchChanged.emit(cancelled)
    manager.batchChanged.emit(original)
    assert ui.itemsModel.items == cancelled.items
    assert dict(ui.batchesModel.items[0].state_counts)["cancelled"] == 1


@pytest.mark.parametrize("reselect", [False, True])
def test_batch_model_notification_cannot_overwrite_nested_cancelled_snapshot(env, tmp_path, reselect):
    ui, repository, manager, _scheduler, _network, _gallery, _selection, _detail, _settings = env
    batch = seed(repository, tmp_path, 1)[0]
    ui.setActive(True)
    item = repository.begin_attempt(batch.items[0].item_id, 0)
    running = repository.get_batch(batch.batch_id)
    entered = []
    def cancel_during_model_notification():
        if entered:
            return
        entered.append(True)
        assert manager.cancel(item.item_id)
        if reselect:
            assert ui.selectBatch(batch.batch_id)
    ui.batchesModel.changed.connect(cancel_during_model_notification)
    manager.batchChanged.emit(running)
    persisted = repository.get_batch(batch.batch_id)
    assert entered == [True]
    assert persisted.items[0].state is DownloadTaskState.CANCELLED
    assert persisted.items[0].generation == item.generation + 1
    assert ui._selected_batch == persisted and ui.itemsModel.items == persisted.items
    assert dict(ui.batchesModel.items[0].state_counts)["cancelled"] == 1
    assert not ui.canCancelBatch
