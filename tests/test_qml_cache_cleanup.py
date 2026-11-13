from __future__ import annotations

from dataclasses import replace

import pytest
from PySide6.QtCore import QUrl, Qt
from PySide6.QtTest import QTest

from danbooru_multi_search.core.favorites import FavoriteCandidate
from danbooru_multi_search.danbooru.query_definition import GalleryQueryDefinition, QueryMode
from danbooru_multi_search.danbooru.query_planning import QueryRating
from danbooru_multi_search.danbooru.query_types import QueryExecutionLimits
from danbooru_multi_search.danbooru.result_processing import ResultFilterSpec, ResultSortSpec
from danbooru_multi_search.tasks import TaskAttemptResult
from tests.m2_qml_harness import close_m2_qml_test_engine
from tests.test_favorite_controller import post
from tests.test_qml_checkpoints import start
from tests.test_qml_favorites import APPLICATION, item, click, wait
from tests.test_protected_image_cache import insert


@pytest.mark.parametrize("kind", ["history", "checkpoints"])
def test_narrow_record_cleanup_actual_confirmation_cancel_and_commit(tmp_path, kind):
    engine, root, session, bridge, factory, network, runtime = start(tmp_path)
    root.setWidth(820)
    root.setHeight(600)
    try:
        definition = GalleryQueryDefinition(QueryMode.STANDARD, "a", QueryRating.ALL, ResultFilterSpec(), ResultSortSpec(), QueryExecutionLimits(page_size=1))
        bridge.loadDraftDefinition(definition)
        assert bridge.executeStandard()
        assert wait(lambda: len(factory.attempts) == 1)
        factory.attempts[0].completion(TaskAttemptResult.success((replace(post(42), general_tags=("a",)),)))
        assert wait(lambda: len(factory.attempts) == 2)
        before_posts = bridge.postsModel.posts
        if kind == "history":
            click(root, item(root, "openQueryDrawerButton"))
            assert wait(lambda: item(root, "queryDrawer").property("opened"))
            click(root, item(root, "historyButton"))
            QTest.qWait(50)
            control = "clearHistoryButton"
        else:
            click(root, item(root, "imageCacheButton"))
            assert wait(lambda: item(root, "imageCachePopup").property("opened"))
            control = "clearCheckpointCacheButton"
        click(root, item(root, control))
        assert wait(lambda: item(root, "cacheCleanupDialog").property("opened"))
        assert session.cache_cleanup.confirmation["count"] == 1
        assert "1 条" in item(root, "cacheCleanupDescription").property("text")
        assert item(root, "cacheCleanupDialog").property("width") <= 788
        click(root, item(root, "cancelCacheCleanupButton"))
        assert session.query_checkpoints._repository.capture_cleanup()
        click(root, item(root, control))
        assert wait(lambda: item(root, "cacheCleanupDialog").property("opened"))
        click(root, item(root, "confirmCacheCleanupButton"))
        assert wait(lambda: not item(root, "cacheCleanupDialog").property("opened"))
        assert session.query_checkpoints._repository.capture_cleanup() == ()
        assert bridge.postsModel.posts is before_posts and bridge.queryState == "partial_cancelled"
        assert bool(session.history_repository.list_queries()) == (kind == "checkpoints")
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


def test_image_confirmation_uses_latest_favorite_and_preserves_active_query(tmp_path):
    engine, root, session, bridge, factory, network, runtime = start(tmp_path)
    try:
        bridge.applyAdvancedText("a")
        assert bridge.executeStandard()
        assert wait(lambda: len(factory.attempts) == 1)
        favorite, ordinary = "https://example.invalid/saved.jpg", "https://example.invalid/ordinary.jpg"
        cache = session.image_cache_controller._ensure_cache()
        insert(cache, favorite)
        insert(cache, ordinary)
        click(root, item(root, "imageCacheButton"))
        assert wait(lambda: item(root, "imageCachePopup").property("opened"))
        click(root, item(root, "clearImageCacheButton"))
        assert wait(lambda: item(root, "cacheCleanupDialog").property("opened"))
        session.favorite_controller._repository.add_favorite(FavoriteCandidate.for_post(replace(post(42), preview_url=favorite, file_url=None)))
        click(root, item(root, "confirmCacheCleanupButton"))
        assert wait(lambda: not item(root, "cacheCleanupDialog").property("opened"))
        assert cache.metaData(QUrl(favorite)).isValid() and not cache.metaData(QUrl(ordinary)).isValid()
        assert bridge.canCancel and network.requests == []
        assert not factory.attempts[0].abortable.abort_count
    finally:
        close_m2_qml_test_engine(engine)
