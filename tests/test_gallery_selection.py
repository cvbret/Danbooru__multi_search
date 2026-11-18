from __future__ import annotations

from danbooru_multi_search.ui.gallery import DanbooruPostListModel
from danbooru_multi_search.danbooru.result_processing import ProcessedPost
from danbooru_multi_search.ui.gallery_selection import GallerySelectionController
from danbooru_multi_search.ui.favorites import post_identity
from tests.test_favorite_controller import build, post


def test_selection_follows_identifiers_and_old_model_action_is_rejected(tmp_path):
    repository, favorites = build(tmp_path)
    model = DanbooruPostListModel()
    model.set_posts((post(1), post(2)))
    selection = GallerySelectionController(model, favorites)
    old_revision = model.revision
    assert selection.setSelected("1", True, old_revision)
    model.set_posts((post(2), post(1)))
    assert selection.postState("1")["selected"]
    assert not selection.setPostFavorite("1", True, old_revision)
    assert not selection.saveSelected(selection.revision, old_revision)
    assert selection.saveSelected(selection.revision, model.revision)
    assert repository.get_by_identity(post_identity("1")) is not None
    assert repository.get_by_identity(post_identity("2")) is None
    model.set_posts((post(2),))
    assert selection.selectedCount == 0


def test_operation_change_clears_selection_and_large_keys_remain_exact(tmp_path):
    repository, favorites = build(tmp_path)
    model = DanbooruPostListModel()
    identifier = 2**60 + 1
    work = post(identifier)
    model.set_processed_posts(1, (ProcessedPost(work, None),))
    selection = GallerySelectionController(model, favorites)
    key = selection.postKeyAt(0, model.revision)
    assert key == str(identifier)
    assert selection.setSelected(key, True, model.revision)
    assert selection.setPostFavorite(key, True, model.revision)
    assert repository.get_by_identity(post_identity(key)).post is not None
    model.set_processed_posts(2, (ProcessedPost(work, None),))
    assert selection.selectedCount == 0


def test_busy_callback_model_replacement_prevents_old_row_write(tmp_path):
    repository, favorites = build(tmp_path)
    model = DanbooruPostListModel()
    model.set_posts((post(1),))
    selection = GallerySelectionController(model, favorites)
    revision = model.revision

    def replace_model():
        if favorites.busy:
            model.set_posts((post(2),))

    favorites.busyChanged.connect(replace_model)
    assert not selection.setPostFavorite("1", True, revision)
    assert repository.capture_collection().revision == 0


def test_busy_callback_clear_selection_prevents_batch_write(tmp_path):
    repository, favorites = build(tmp_path)
    model = DanbooruPostListModel()
    model.set_posts((post(1), post(2)))
    selection = GallerySelectionController(model, favorites)
    selection.setSelected("1", True, model.revision)
    selection.setSelected("2", True, model.revision)
    favorites.busyChanged.connect(lambda: selection.clearSelection() if favorites.busy else None)
    assert not selection.saveSelected(selection.revision, model.revision)
    assert repository.capture_collection().revision == 0


def test_selected_posts_exposes_stable_order_only_for_the_captured_source(tmp_path):
    repository, favorites = build(tmp_path)
    model = DanbooruPostListModel()
    works = (post(2**60 + 1), post(2), post(3))
    model.set_posts(works)
    selection = GallerySelectionController(model, favorites)
    selection.setSelected("3", True, model.revision)
    selection.setSelected(str(2**60 + 1), True, model.revision)
    revision = selection.revision
    assert selection.selected_posts(revision, model.revision) == (works[0], works[2])
    selection.clearSelection()
    assert selection.selected_posts(revision, model.revision) is None
