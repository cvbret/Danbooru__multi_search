"""Detail source and gallery-return behavior independent of media presentation."""

from __future__ import annotations

from datetime import timezone

import pytest

from danbooru_multi_search.danbooru import (
    DanbooruDetailController,
    DanbooruPost,
    DanbooruPostListModel,
    TagTranslationStore,
)
from danbooru_multi_search.ui.detail_navigation import DetailNavigationController


def _post(post_id: int = 10, **changes) -> DanbooruPost:
    values = {
        "id": post_id,
        "preview_url": "https://cdn.invalid/preview.jpg",
        "large_file_url": "https://cdn.invalid/large.jpg",
        "file_url": "https://cdn.invalid/original.jpg",
        "score": -2,
        "fav_count": 0,
        "rating": "g",
        "width": 1600,
        "height": 1200,
        "file_ext": "jpg",
        "artist_tags": ("sample_artist",),
    }
    values.update(changes)
    return DanbooruPost(**values)


def _navigation(*posts):
    model = DanbooruPostListModel()
    model.set_posts(posts)
    store = TagTranslationStore()
    detail = DanbooruDetailController(
        "https://example.invalid", store, local_timezone=timezone.utc
    )
    return DetailNavigationController(detail, model), model, store


def test_invalid_browse_rows_keep_current_work_and_return_position():
    navigation, model, _store = _navigation(_post())
    detail = navigation.detail_controller
    assert navigation.openBrowsePost(0, 125.5, 0, 800, 600, 1280, 820)
    snapshot = navigation.gallery_snapshot
    generation = detail.mediaGeneration
    assert navigation.openBrowsePost(-1, 0, -1, 1, 1, 1, 1) is False
    assert navigation.openBrowsePost(8, 0, -1, 1, 1, 1, 1) is False
    assert navigation.openBrowsePost(0, float("nan"), 0, 1, 1, 1, 1) is False
    assert detail.current_post is model.posts[0]
    assert detail.mediaGeneration == generation
    assert navigation.gallery_snapshot is snapshot


def test_navigation_snapshot_signal_close_supersedes_open():
    navigation, _model, _store = _navigation(_post())
    navigation.gallerySnapshotChanged.connect(navigation.closeDetail)
    assert navigation.openBrowsePost(0, 0, 0, 800, 600, 1280, 820) is False
    assert navigation.detail_controller.isOpen is False
    assert navigation.detail_controller.mediaUrl == ""


def test_saved_snapshot_keeps_source_browse_results_and_history(tmp_path):
    from danbooru_multi_search.storage import Database, HistoryRepository

    navigation, browse, _store = _navigation(_post(10))
    database = Database(tmp_path / "history.sqlite3")
    database.initialize()
    history = HistoryRepository(database)
    history.record_accepted_query(
        display_text="cat",
        normalized_tags=("cat",),
        rating="general",
        page_size=40,
        page=1,
        count_tag_usage=False,
    )
    original_history = history.list_queries()
    original_posts = browse.posts
    original_revision = browse.revision
    saved_post = _post(99)
    assert navigation.openBrowsePost(0, 40, 0, 800, 600, 1280, 820)
    assert navigation.open_saved_post(saved_post, source_page="favorites")
    detail = navigation.detail_controller
    assert navigation.sourcePage == "favorites"
    assert navigation.gallery_snapshot is None
    assert detail.current_post is saved_post
    assert not hasattr(detail, "_gallery_model")
    assert not hasattr(detail, "_gallery_snapshot")
    assert browse.posts is original_posts
    assert browse.revision == original_revision
    assert history.list_queries() == original_history
    assert navigation.closeDetail()
    assert navigation.sourcePage == "favorites"
    assert navigation.takeGalleryRestorePlan(
        browse, 800, 600, 1280, 820, 0, 1000
    )["available"] is False
    assert history.list_queries() == original_history


def test_saved_snapshot_invalid_input_does_not_replace_current_navigation():
    navigation, _model, _store = _navigation(_post())
    assert navigation.openBrowsePost(0, 40, 0, 800, 600, 1280, 820)
    snapshot = navigation.gallery_snapshot
    post = navigation.detail_controller.current_post
    assert navigation.open_saved_post(None, source_page="favorites") is False
    assert navigation.open_saved_post(_post(99), source_page=" ") is False
    assert navigation.gallery_snapshot is snapshot
    assert navigation.sourcePage == "browse"
    assert navigation.detail_controller.current_post is post


def test_exact_gallery_restore_and_revision_based_post_id_restore():
    first = _post(10)
    second = _post(20)
    controller, model, _store = _navigation(first, second)
    controller.openBrowsePost(1, 221.25, 1, 800, 600, 1280, 820)
    assert controller.closeDetail()

    exact = controller.takeGalleryRestorePlan(
        model,
        800,
        600,
        1280,
        820,
        0,
        2000,
    )
    assert exact == {
        "available": True,
        "exact": True,
        "index": 1,
        "contentY": 221.25,
    }

    controller.openBrowsePost(1, 500, 1, 800, 600, 1280, 820)
    opened_post = controller.detail_controller.current_post
    model.set_posts((_post(20), _post(30)))
    assert controller.detail_controller.current_post is opened_post
    assert controller.closeDetail()
    changed = controller.takeGalleryRestorePlan(
        model,
        800,
        600,
        1280,
        820,
        10,
        650,
    )
    assert changed["exact"] is False
    assert changed["index"] == 0
    assert changed["contentY"] == 60


def test_changed_gallery_restore_clamps_missing_post_and_empty_model():
    controller, model, _store = _navigation(_post(10), _post(20), _post(30))
    controller.openBrowsePost(2, 900, 2, 800, 600, 1280, 820)
    assert controller.closeDetail()
    replacement = DanbooruPostListModel()
    replacement.set_posts((_post(40), _post(50)))

    changed = controller.takeGalleryRestorePlan(
        replacement,
        700,
        500,
        1200,
        760,
        -15,
        620,
    )

    assert changed == {
        "available": True,
        "exact": False,
        "index": 1,
        "contentY": 105.0,
    }

    controller.openBrowsePost(0, 200, 0, 800, 600, 1280, 820)
    assert controller.closeDetail()
    empty = DanbooruPostListModel()
    empty.set_posts(())
    empty_plan = controller.takeGalleryRestorePlan(
        empty,
        800,
        600,
        1280,
        820,
        12,
        0,
    )
    assert empty_plan == {
        "available": True,
        "exact": False,
        "index": -1,
        "contentY": 12.0,
    }


def test_restore_plan_is_invalid_when_signal_synchronously_opens_new_post():
    first = _post(10)
    second = _post(20)
    controller, model, _store = _navigation(first, second)
    assert controller.openBrowsePost(0, 125, 0, 800, 600, 1280, 820)
    assert controller.closeDetail()
    redirected: list[bool] = []

    def open_second_once() -> None:
        if redirected:
            return
        redirected.append(True)
        assert controller.openBrowsePost(1, 325, 1, 800, 600, 1280, 820)

    controller.gallerySnapshotChanged.connect(open_second_once)

    plan = controller.takeGalleryRestorePlan(
        model,
        800,
        600,
        1280,
        820,
        10,
        1000,
    )

    assert plan == {"available": False, "index": -1, "contentY": 10.0}
    assert controller.detail_controller.isOpen is True
    assert controller.detail_controller.current_post is second
    assert controller.gallery_snapshot is not None
    assert controller.gallery_snapshot.post_id == 20


def test_restore_plan_is_invalid_when_signal_synchronously_shuts_down():
    controller, model, _store = _navigation(_post())
    assert controller.openBrowsePost(0, 125, 0, 800, 600, 1280, 820)
    assert controller.closeDetail()
    controller.gallerySnapshotChanged.connect(controller.shutdown)

    plan = controller.takeGalleryRestorePlan(
        model,
        800,
        600,
        1280,
        820,
        -5,
        1000,
    )

    assert plan == {"available": False, "index": -1, "contentY": -5.0}
    assert controller.detail_controller.isOpen is False
    assert controller.gallery_snapshot is None


def test_tag_activation_discards_snapshot_and_emits_once():
    controller, _model, _store = _navigation(_post())
    requested: list[str] = []
    controller.tagSearchRequested.connect(requested.append)
    controller.openBrowsePost(0, 50, 0, 800, 600, 1280, 820)
    generation = controller.detail_controller.mediaGeneration

    assert controller.detail_controller.activateTag("sample_artist") is True
    assert requested == ["sample_artist"]
    assert controller.detail_controller.isOpen is False
    assert controller.gallery_snapshot is None
    assert controller.detail_controller.mediaGeneration == generation + 1
    assert controller.detail_controller.activateTag("sample_artist") is False
    assert requested == ["sample_artist"]


def test_synchronous_new_open_supersedes_old_open_without_old_signals():
    first = _post(10, large_file_url="https://cdn.invalid/first.jpg")
    second = _post(20, large_file_url="https://cdn.invalid/second.jpg")
    controller, _model, _store = _navigation(first, second)
    redirected: list[bool] = []

    def open_second_once() -> None:
        if redirected:
            return
        redirected.append(True)
        assert controller.openBrowsePost(1, 0, 1, 800, 600, 1280, 820)

    controller.gallerySnapshotChanged.connect(open_second_once)

    assert controller.openBrowsePost(0, 0, 0, 800, 600, 1280, 820) is False
    assert controller.detail_controller.current_post is second
    assert controller.detail_controller.postIdText == "20"
    assert controller.detail_controller.mediaUrl.endswith("second.jpg")


@pytest.mark.parametrize("signal_name", ["mediaChanged", "openChanged"])
def test_close_does_not_restore_over_synchronous_new_open(signal_name):
    first = _post(10, large_file_url="https://cdn.invalid/first.jpg")
    second = _post(20, large_file_url="https://cdn.invalid/second.jpg")
    controller, _model, _store = _navigation(first, second)
    assert controller.openBrowsePost(0, 125, 0, 800, 600, 1280, 820)
    redirected: list[bool] = []

    def open_second_once() -> None:
        if redirected:
            return
        redirected.append(True)
        assert controller.openBrowsePost(1, 325, 1, 800, 600, 1280, 820)

    getattr(controller.detail_controller, signal_name).connect(open_second_once)

    assert controller.closeDetail() is False
    assert controller.detail_controller.isOpen is True
    assert controller.detail_controller.current_post is second
    assert controller.gallery_snapshot is not None
    assert controller.gallery_snapshot.post_id == 20
    assert controller.gallery_snapshot.content_y == 325
    assert controller.detail_controller.mediaUrl.endswith("second.jpg")
