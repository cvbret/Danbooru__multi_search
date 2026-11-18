from __future__ import annotations

from danbooru_multi_search.core.favorites import FavoriteIdentity, FavoriteObjectType
from danbooru_multi_search.ui.details import DanbooruDetailController
from danbooru_multi_search.danbooru.tag_translation import TagTranslationStore
from danbooru_multi_search.ui.detail_favorites import DetailFavoritesController
from tests.test_favorite_controller import build, post


def test_detail_post_and_four_tag_categories_use_shared_confirmed_state(tmp_path):
    repository, favorites = build(tmp_path)
    detail = DanbooruDetailController("https://example.invalid", TagTranslationStore())
    actions = DetailFavoritesController(detail, favorites)
    work = post()
    assert detail.open_post(work)
    assert actions.setPostFavorite(actions.postKey, actions.revision, True)
    assert actions.postState["saved"]
    assert work.fav_count == 17
    for category, name in (
        ("artist", "sample_artist"), ("character", "sample_character"),
        ("copyright", "sample_series"), ("general", "cat"),
    ):
        assert actions.setTagFavorite(actions.postKey, actions.revision, name, category, True)
        identity = FavoriteIdentity(FavoriteObjectType.TAG, name)
        assert repository.get_by_identity(identity).tag_category.value == category
        assert actions.tagState(name, category)["saved"]
    assert not actions.setTagFavorite(actions.postKey, actions.revision, "highres", "meta", True)
    assert not actions.setTagFavorite(actions.postKey, actions.revision, "absent", "general", True)


def test_old_detail_and_same_id_reopen_menu_identities_are_rejected(tmp_path):
    repository, favorites = build(tmp_path)
    detail = DanbooruDetailController("https://example.invalid", TagTranslationStore())
    actions = DetailFavoritesController(detail, favorites)
    first = post(1)
    detail.open_post(first)
    key, revision = actions.postKey, actions.revision
    detail.open_post(post(2))
    assert not actions.setPostFavorite(key, revision, True)
    assert not actions.setTagFavorite(key, revision, "cat", "general", True)
    detail.open_post(first)
    assert not actions.setTagFavorite(key, revision, "cat", "general", True)
    favorites.busyChanged.connect(lambda: detail.closeDetail() if favorites.busy else None)
    assert not actions.setPostFavorite(actions.postKey, actions.revision, True)
    assert repository.capture_collection().revision == 0
