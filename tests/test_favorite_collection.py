from __future__ import annotations

from dataclasses import replace

import pytest

from danbooru_multi_search.core.favorites import (
    FavoriteCandidate, FavoriteTagCategory, FavoriteError, FolderDeleteMode,
)
from danbooru_multi_search.danbooru.details import DanbooruDetailController
from danbooru_multi_search.danbooru.gallery import DanbooruPostListModel
from danbooru_multi_search.danbooru.tag_translation import TagTranslationStore
from danbooru_multi_search.storage.favorites import FavoriteRepository
from danbooru_multi_search.ui.detail_navigation import DetailNavigationController
from danbooru_multi_search.ui.favorite_collection import FavoriteCollectionController
from tests.test_favorite_controller import build, post


def collection(tmp_path):
    repository, shared = build(tmp_path)
    detail = DanbooruDetailController("https://example.invalid", TagTranslationStore())
    browse = DanbooruPostListModel()
    browse.set_posts((post(999),))
    navigation = DetailNavigationController(detail, browse)
    controller = FavoriteCollectionController(repository, shared, navigation)
    return repository, shared, controller, navigation, browse


def test_more_than_one_thousand_loads_and_select_loaded_means_loaded_only(tmp_path):
    repository, shared, controller, *_ = collection(tmp_path)
    repository.add_favorites(tuple(FavoriteCandidate.for_pool(i, f"Pool {i}") for i in range(1, 1052)))
    assert controller.loadedCount == 0
    controller.setActive(True)
    assert controller.totalCount == 1051
    assert controller.loadedCount == 200
    controller.selectLoaded()
    controller.loadMore()
    assert controller.selectedCount == 200
    while controller.canLoadMore:
        controller.loadMore()
    assert controller.loadedCount == 1051
    assert len({item.id for item in controller._model.items}) == 1051
    controller.selectLoaded()
    assert controller.selectedCount == 1051
    generation = controller.generation
    shared.retryStates()
    assert controller.generation == generation
    assert controller.selectedCount == 1051


def test_filters_cover_all_saved_kinds_exact_tags_and_existing_sorting(tmp_path):
    repository, _, controller, *_ = collection(tmp_path)
    folder = repository.create_folder("Folder")
    repository.add_favorite(FavoriteCandidate.for_post(post(1)), folder_id=folder.id)
    repository.add_favorite(FavoriteCandidate.for_post(replace(post(2), score=10, rating="q")))
    for category in FavoriteTagCategory:
        repository.add_favorite(FavoriteCandidate.for_tag(category.value, category))
    repository.add_favorite(FavoriteCandidate.for_pool(5, "Pool"))
    controller.setActive(True)
    assert controller.totalCount == 7
    for kind, count in (("post", 2), ("artist", 1), ("character", 1), ("copyright", 1), ("general", 1), ("pool", 1)):
        assert controller.applyFilters({"kind": kind})
        assert controller.totalCount == count
    assert controller.applyFilters({"kind": "post", "exactTag": "cat", "exactArtist": "sample_artist", "rating": "g"})
    assert controller.totalCount == 1
    assert controller.applyFilters({"kind": "all", "exactTag": "", "exactArtist": "", "rating": "all", "scope": "folder", "folderKey": str(folder.id)})
    assert controller.totalCount == 1
    assert controller.applyFilters({"scope": "uncategorized", "folderKey": ""})
    assert controller.totalCount == 6
    assert controller.applyFilters({"scope": "all", "kind": "post", "sortField": "score", "sortDirection": "descending"})
    assert controller._model.items[0].post.id == 2


def test_delete_confirmation_cancel_and_foreign_commit_reject_old_revision(tmp_path):
    repository, _, controller, *_ = collection(tmp_path)
    item = repository.add_favorite(FavoriteCandidate.for_post(post(1)))
    controller.setActive(True)
    controller.selectLoaded()
    assert controller.prepareDelete(controller.selectionRevision, controller.generation)
    token = controller.confirmation["token"]
    assert "1" in controller.confirmation["text"]
    controller.cancelConfirmation(token)
    assert repository.get_favorite(item.id) == item
    assert not controller.confirm(token)
    assert controller.prepareDelete(controller.selectionRevision, controller.generation)
    token = controller.confirmation["token"]
    other = FavoriteRepository(repository._database)
    other.delete_favorite(item.id)
    replacement = other.add_favorite(FavoriteCandidate.for_post(post(2)))
    assert replacement.id == item.id
    assert not controller.confirm(token)
    assert repository.get_favorite(replacement.id) == replacement
    assert "已经变化" in controller.errorText


def test_identifier_reuse_before_preview_is_rejected(tmp_path):
    repository, _, controller, *_ = collection(tmp_path)
    item = repository.add_favorite(FavoriteCandidate.for_post(post(1)))
    controller.setActive(True)
    controller.selectLoaded()
    repository.delete_favorite(item.id)
    replacement = repository.add_favorite(FavoriteCandidate.for_post(post(2)))
    assert replacement.id == item.id
    assert not controller.prepareDelete(controller.selectionRevision, controller.generation)
    assert not controller.confirmation["token"]
    assert repository.get_favorite(replacement.id) == replacement


@pytest.mark.parametrize("delete_contents", [False, True])
def test_folder_delete_counts_all_contents_and_clears_selection(tmp_path, delete_contents):
    repository, shared, controller, *_ = collection(tmp_path)
    folder = repository.create_folder("Folder")
    saved = repository.add_favorites(tuple(FavoriteCandidate.for_pool(i, f"Pool {i}") for i in range(1, 204)), folder_id=folder.id)
    controller.setActive(True)
    controller.selectLoaded()
    assert controller.prepareDeleteFolder(str(folder.id), delete_contents, controller.generation)
    assert "203" in controller.confirmation["text"]
    assert controller.confirm(controller.confirmation["token"])
    assert repository.list_folders() == ()
    assert controller.selectedCount == 0
    if delete_contents:
        assert repository.list_favorites() == ()
        assert all(not shared.state(item.identity)["saved"] for item in saved)
    else:
        assert all(item.folder_id is None for item in repository.list_favorites())


def test_move_preview_only_counts_changed_rows_and_rename_uses_shared_writes(tmp_path):
    repository, shared, controller, *_ = collection(tmp_path)
    folder = repository.create_folder("Folder")
    first = repository.add_favorite(FavoriteCandidate.for_post(post(1)), folder_id=folder.id)
    second = repository.add_favorite(FavoriteCandidate.for_post(post(2)))
    controller.setActive(True)
    controller.selectLoaded()
    assert controller.prepareMove(str(folder.id), controller.selectionRevision, controller.generation)
    assert "其中 1 项" in controller.confirmation["text"]
    assert controller.confirm(controller.confirmation["token"])
    assert repository.get_favorite(second.id).folder_id == folder.id
    assert repository.get_favorite(first.id).favorited_at == first.favorited_at
    assert controller.saveFolder(str(folder.id), controller.generation, "Renamed")
    assert repository.get_folder(folder.id).name == "Renamed"
    assert controller.saveFolder("", controller.generation, "New")
    assert len(repository.list_folders()) == 2


def test_refresh_and_filter_changes_expire_pending_and_selection(tmp_path):
    repository, shared, controller, *_ = collection(tmp_path)
    repository.add_favorite(FavoriteCandidate.for_post(post(1)))
    controller.setActive(True)
    controller.selectLoaded()
    assert controller.prepareDelete(controller.selectionRevision, controller.generation)
    token = controller.confirmation["token"]
    assert controller.applyFilters({"sortDirection": "ascending"})
    assert controller.selectedCount == 0
    assert not controller.confirm(token)
    controller.selectLoaded()
    old_generation = controller.generation
    shared.busyChanged.connect(lambda: controller.reload() if shared.busy else None)
    assert controller.prepareDelete(controller.selectionRevision, controller.generation)
    assert not controller.confirm(controller.confirmation["token"])
    assert controller.generation > old_generation
    assert repository.capture_collection().revision == 1


def test_detail_uses_saved_snapshot_and_preserves_source_and_browse(tmp_path):
    repository, _, controller, navigation, browse = collection(tmp_path)
    saved = repository.add_favorite(FavoriteCandidate.for_post(post(1)))
    controller.setActive(True)
    controller.selectLoaded()
    old_posts, old_revision = browse.posts, browse.revision
    assert controller.openItem(str(saved.id), controller.generation, 120, 0, 800, 400)
    assert navigation.sourcePage == "favorites"
    assert navigation.detail_controller.current_post == saved.post
    assert navigation.closeDetail()
    plan = controller.takeRestorePlan(800, 400, 0, 2000)
    assert plan["available"] and plan["exact"]
    assert plan["index"] == 0 and plan["contentY"] == 120
    assert controller.restorePlanCurrent(plan["generation"], plan["modelRevision"])
    assert controller.selectedCount == 1
    assert browse.posts is old_posts and browse.revision == old_revision


@pytest.mark.parametrize("action", ["filter", "close"])
def test_return_plan_rejects_synchronous_selection_notification_reentry(tmp_path, action):
    repository, _, controller, navigation, _ = collection(tmp_path)
    saved = repository.add_favorite(FavoriteCandidate.for_post(post(1)))
    controller.setActive(True)
    assert controller.openItem(str(saved.id), controller.generation, 120, 0, 800, 400)
    navigation.closeDetail()
    once = False
    def reenter():
        nonlocal once
        if once:
            return
        once = True
        if action == "filter":
            controller.applyFilters({"kind": "pool"})
        else:
            controller.shutdown()
    controller.changed.connect(reenter)
    assert controller.takeRestorePlan(800, 400, 0, 2000) == {"available": False}


def test_resize_return_plan_tracks_favorite_and_preserves_card_offset(tmp_path):
    repository, _, controller, navigation, _ = collection(tmp_path)
    saved = repository.add_favorites(tuple(FavoriteCandidate.for_post(post(i)) for i in range(1, 31)))
    controller.setActive(True)
    item = controller._model.items[6]
    assert controller.openItem(str(item.id), controller.generation, 275, 6, 1200, 400, 65)
    navigation.closeDetail()
    plan = controller.takeRestorePlan(780, 300, 0, 3400)
    assert plan["available"] and not plan["exact"]
    assert plan["index"] == 6
    assert plan["anchorOffset"] == 65


def test_clearing_selection_in_busy_callback_aborts_confirmed_delete(tmp_path):
    repository, shared, controller, *_ = collection(tmp_path)
    repository.add_favorite(FavoriteCandidate.for_post(post(1)))
    controller.setActive(True)
    controller.selectLoaded()
    assert controller.prepareDelete(controller.selectionRevision, controller.generation)
    token = controller.confirmation["token"]
    shared.busyChanged.connect(lambda: controller.clearSelection() if shared.busy else None)
    assert not controller.confirm(token)
    assert repository.capture_collection().revision == 1
    assert len(repository.list_favorites()) == 1


def test_folder_identifier_reuse_after_preview_cannot_delete_new_folder(tmp_path):
    repository, _, controller, *_ = collection(tmp_path)
    folder = repository.create_folder("Old")
    controller.setActive(True)
    assert controller.prepareDeleteFolder(str(folder.id), True, controller.generation)
    token = controller.confirmation["token"]
    other = FavoriteRepository(repository._database)
    other.delete_folder(folder.id, FolderDeleteMode.MOVE_TO_UNCATEGORIZED)
    new_folder = other.create_folder("New")
    assert new_folder.id == folder.id
    assert not controller.confirm(token)
    assert repository.get_folder(new_folder.id) == new_folder


def test_maximum_valid_utc_time_renders_with_a_local_conversion_fallback(tmp_path):
    from datetime import datetime, timezone
    from danbooru_multi_search.storage.favorite_codec import encode_favorite_timestamp

    repository, _, controller, *_ = collection(tmp_path)
    maximum = datetime.max.replace(tzinfo=timezone.utc)
    saved = repository.add_favorite(FavoriteCandidate.for_post(post(1)), favorited_at=maximum)
    controller.setActive(True)
    row = controller.rowAt(0)
    assert row["favoriteKey"] == str(saved.id)
    assert row["timeText"].startswith("9999-12-31")
    assert repository.get_favorite(saved.id).favorited_at == maximum
    assert encode_favorite_timestamp(maximum) == "9999-12-31T23:59:59.999999Z"

    class UnavailableLocalTime(datetime):
        def astimezone(self, tz=None):
            if tz is None:
                raise OSError("local time is unavailable")
            return super().astimezone(tz)

    unavailable = UnavailableLocalTime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    controller._model.replace((replace(saved, favorited_at=unavailable),))
    assert controller.rowAt(0)["timeText"] == "9999-12-31 23:59 UTC"
