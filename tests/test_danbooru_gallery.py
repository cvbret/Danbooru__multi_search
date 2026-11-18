from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication

from danbooru_multi_search.danbooru import (
    DanbooruPost,
    DanbooruPostListModel,
    PostRole,
    ProcessedPost,
    build_posts_request,
)


APPLICATION = QGuiApplication.instance() or QGuiApplication([])


def _request_query(request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(request.url().toString()).query)


def test_posts_request_transmits_page_and_full_allowed_limit():
    request = build_posts_request(
        "https://example.invalid",
        "cat rating:q",
        page=7,
        limit=200,
    )

    assert _request_query(request) == {
        "tags": ["cat rating:q"],
        "page": ["7"],
        "limit": ["200"],
    }


@pytest.mark.parametrize("limit", [0, 201, True])
def test_posts_request_rejects_out_of_range_limits(limit):
    with pytest.raises(ValueError):
        build_posts_request("https://example.invalid", "cat", limit=limit)


def _post_model(
    post_id: int,
    *,
    score: int | None,
    fav_count: int | None,
    file_ext: str = "jpg",
) -> DanbooruPost:
    return DanbooruPost(
        id=post_id,
        preview_url=f"qrc:/thumbnail-{post_id}.jpg",
        large_file_url=None,
        file_url=None,
        score=score,
        fav_count=fav_count,
        rating="g",
        width=640,
        height=480,
        file_ext=file_ext,
    )


def test_list_model_distinguishes_missing_zero_and_negative_popularity():
    model = DanbooruPostListModel()
    model.set_posts(
        (
            _post_model(1, score=None, fav_count=None),
            _post_model(2, score=0, fav_count=0, file_ext="gif"),
            _post_model(3, score=-5, fav_count=2, file_ext="webm"),
        )
    )

    assert model.count == 3
    assert model.data(model.index(0), int(PostRole.SCORE_TEXT)) == "暂无数据"
    assert model.data(model.index(0), int(PostRole.FAVORITE_TEXT)) == "暂无数据"
    assert model.data(model.index(1), int(PostRole.SCORE_TEXT)) == "0"
    assert model.data(model.index(1), int(PostRole.FAVORITE_TEXT)) == "0"
    assert model.data(model.index(1), int(PostRole.MEDIA_LABEL)) == "GIF"
    assert model.data(model.index(2), int(PostRole.SCORE_TEXT)) == "-5"
    assert model.data(model.index(2), int(PostRole.MEDIA_LABEL)) == "视频"


def test_m2_model_skips_only_same_operation_same_objects_and_ranks():
    model = DanbooruPostListModel()
    first = _post_model(1, score=1, fav_count=2)
    items = (ProcessedPost(first, 1),)

    assert model.set_processed_posts(7, items) is True
    revision = model.revision
    assert model.set_processed_posts(7, (ProcessedPost(first, 1),)) is False
    assert model.revision == revision

    assert model.set_processed_posts(7, (ProcessedPost(first, 2),)) is True
    assert model.revision == revision + 1


def test_m2_model_switches_operation_and_equal_valued_post_object():
    model = DanbooruPostListModel()
    first = _post_model(1, score=1, fav_count=2)
    equal_but_new = _post_model(1, score=1, fav_count=2)
    model.set_processed_posts(1, (ProcessedPost(first, None),))
    revision = model.revision

    assert model.set_processed_posts(2, (ProcessedPost(first, None),)) is True
    assert model.revision == revision + 1
    assert model.set_processed_posts(
        2,
        (ProcessedPost(equal_but_new, None),),
    ) is True
    assert model.post_at(0) is equal_but_new


def test_clear_removes_empty_m2_gallery_operation_identity():
    model = DanbooruPostListModel()
    model.set_processed_posts(3, ())
    revision = model.revision

    model.clear()

    assert model.display_operation_id is None
    assert model.revision == revision + 1


def test_rating_labels_share_one_ui_table_and_keep_original_lookup_rules():
    from danbooru_multi_search.core.favorites import (
        FavoriteIdentity, FavoriteItem, FavoriteObjectType,
    )
    from danbooru_multi_search.danbooru import DanbooruDetailController
    from danbooru_multi_search.danbooru.tag_translation import TagTranslationStore
    from danbooru_multi_search.ui import details, favorite_models, gallery

    assert DanbooruPostListModel is gallery.DanbooruPostListModel
    assert PostRole is gallery.PostRole
    assert DanbooruDetailController is details.DanbooruDetailController
    assert gallery.RATING_LABELS is details.RATING_LABELS is favorite_models.RATING_LABELS
    model = DanbooruPostListModel()
    detail = DanbooruDetailController("https://example.invalid", TagTranslationStore())
    favorites = favorite_models.FavoriteListModel()
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def show(post):
        model.set_posts((post,))
        detail.open_post(post)
        favorites.replace((FavoriteItem(
            1, FavoriteIdentity(FavoriteObjectType.POST, str(post.id)),
            "Saved post", None, when, when, post=post,
        ),))

    try:
        post = _post_model(1, score=0, fav_count=0)
        for rating, text in (
            ("g", "General"), ("s", "Sensitive"),
            ("q", "Questionable"), ("e", "Explicit"),
        ):
            show(replace(post, rating=rating))
            assert model.data(model.index(0), int(PostRole.RATING_TEXT)) == text
            assert detail.ratingText == text
            assert favorites.mapping(0)["ratingText"] == text
        show(replace(post, rating="invalid"))
        with pytest.raises(KeyError, match="invalid"):
            model.data(model.index(0), int(PostRole.RATING_TEXT))
        assert detail.ratingText == ""
        assert favorites.mapping(0)["ratingText"] == ""
    finally:
        detail.shutdown()
