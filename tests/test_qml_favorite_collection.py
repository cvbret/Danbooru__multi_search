from __future__ import annotations

import math

import pytest
from PySide6.QtCore import QObject, QPointF, QUrl
from PySide6.QtNetwork import QNetworkRequest
from PySide6.QtTest import QTest

from danbooru_multi_search.core.bootstrap import initialize_runtime
from danbooru_multi_search.core.favorites import FavoriteCandidate, FavoriteTagCategory
from danbooru_multi_search.storage.favorites import FavoriteRepository
from danbooru_multi_search.ui.image_cache import ImageNetworkAccessManager
from tests.m2_qml_harness import create_m2_qml_test_engine, close_m2_qml_test_engine
from tests.test_favorite_controller import APPLICATION, IMAGE, post
from tests.test_qml_favorites import NoNetwork, click, wait, item, invoke, reveal_detail_tag


def visible_item(root, name):
    pending = [root]
    while pending:
        candidate = pending.pop()
        if candidate.objectName() == name:
            return candidate
        pending.extend(candidate.childItems())
    return None


def start(tmp_path, candidates, *, narrow=False):
    runtime = initialize_runtime(tmp_path)
    repository = FavoriteRepository(runtime.database)
    repository.add_favorites(tuple(candidates))
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
    root.requestActivate()
    assert QTest.qWaitForWindowExposed(root)
    return engine, root, bridge, session, repository, network


def enter(root, session):
    click(root, item(root, "favoritesNavigationButton"))
    assert wait(lambda: session.favorite_collection.activated)
    assert root.property("activePage") == "favorites"
    return item(root, "favoritesPage")


def reveal_filter(root, control):
    viewport = item(root, "favoriteFilterScroll").property("contentItem")
    center = control.mapToScene(QPointF(control.width() / 2, control.height() / 2))
    middle = viewport.mapToScene(QPointF(0, viewport.height() / 2))
    maximum = max(0, viewport.property("contentHeight") - viewport.height())
    viewport.setProperty("contentY", max(0, min(viewport.property("contentY") + center.y() - middle.y(), maximum)))
    APPLICATION.processEvents()


def test_hidden_collection_does_not_create_remote_image_requests_at_startup(tmp_path, monkeypatch):
    image_requests = []
    original = ImageNetworkAccessManager.createRequest

    def intercept(manager, operation, request, outgoing_data=None):
        prepared = QNetworkRequest(request)
        if request.url().scheme() in {"http", "https"}:
            image_requests.append(request.url().toString())
            prepared.setUrl(QUrl(IMAGE))  # Exercise the actual image factory without external I/O.
        return original(manager, operation, prepared, outgoing_data)

    monkeypatch.setattr(ImageNetworkAccessManager, "createRequest", intercept)
    engine, root, bridge, session, repository, network = start(
        tmp_path, (FavoriteCandidate.for_post(post(1, url="https://images.example.invalid/one.png")),)
    )
    try:
        QTest.qWait(100)
        APPLICATION.processEvents()
        assert image_requests == []
        assert session.favorite_collection.loadedCount == 0
        assert network.requests == []
        enter(root, session)
        assert wait(lambda: bool(image_requests))
        assert session.favorite_collection.loadedCount == 1
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


def test_collection_loads_past_one_thousand_and_selects_loaded_only(tmp_path):
    engine, root, bridge, session, repository, network = start(
        tmp_path, (FavoriteCandidate.for_pool(i, f"Pool {i}") for i in range(1, 1006)), narrow=True
    )
    try:
        enter(root, session)
        controller = session.favorite_collection
        click(root, item(root, "selectLoadedFavoritesButton"))
        assert controller.selectedCount == 200
        while controller.canLoadMore:
            before = controller.loadedCount
            click(root, item(root, "loadMoreFavoritesButton"))
            assert controller.loadedCount > before
            assert controller.selectedCount == 200
        assert controller.loadedCount == 1005
        assert item(root, "selectLoadedFavoritesButton").property("text") == "全选已加载"
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


@pytest.mark.parametrize("delete_contents", [False, True])
def test_folder_and_batch_buttons_show_counts_allow_cancel_and_apply(tmp_path, delete_contents):
    engine, root, bridge, session, repository, network = start(
        tmp_path, (FavoriteCandidate.for_post(post(i)) for i in (1001, 1002)), narrow=True
    )
    try:
        page = enter(root, session)
        click(root, item(root, "createFavoriteFolderButton"))
        item(root, "favoriteFolderNameInput").setProperty("text", "Folder")
        click(root, item(root, "saveFavoriteFolderButton"))
        assert len(repository.list_folders()) == 1
        folder = repository.list_folders()[0]
        click(root, item(root, "selectLoadedFavoritesButton"))
        target = item(root, "favoriteMoveTarget")
        target.setProperty("currentIndex", 1)
        click(root, item(root, "moveSelectedFavoritesButton"))
        assert wait(lambda: bool(session.favorite_collection.confirmation["token"]))
        assert "2" in item(root, "favoriteConfirmationText").property("text")
        click(root, item(root, "cancelFavoriteOperationButton"))
        assert all(saved.folder_id is None for saved in repository.list_favorites())
        click(root, item(root, "moveSelectedFavoritesButton"))
        click(root, item(root, "confirmFavoriteOperationButton"))
        assert all(saved.folder_id == folder.id for saved in repository.list_favorites())
        folder_filter = item(root, "favoriteFolderFilter")
        folder_filter.setProperty("currentIndex", 2)
        folder_filter.activated.emit(2)
        assert session.favorite_collection.formValues["folderKey"] == str(folder.id)
        button = item(root, "removeFavoriteFolderContentsButton" if delete_contents else "removeFavoriteFolderButton")
        reveal_filter(root, button)
        click(root, button)
        assert "2" in item(root, "favoriteConfirmationText").property("text")
        click(root, item(root, "cancelFavoriteOperationButton"))
        assert repository.list_folders() == (folder,)
        reveal_filter(root, button)
        click(root, button)
        click(root, item(root, "confirmFavoriteOperationButton"))
        assert repository.list_folders() == ()
        assert len(repository.list_favorites()) == (0 if delete_contents else 2)
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


@pytest.mark.parametrize("resize", [False, True])
def test_saved_detail_returns_to_collection_by_favorite_and_keeps_browse(tmp_path, resize):
    engine, root, bridge, session, repository, network = start(
        tmp_path, (FavoriteCandidate.for_post(post(i)) for i in range(1001, 1031))
    )
    try:
        bridge.postsModel.set_posts((post(999),))
        browse_posts, browse_revision = bridge.postsModel.posts, bridge.postsModel.revision
        before_history = session.history_repository.list_queries()
        page = enter(root, session)
        controller = session.favorite_collection
        first = controller._model.items[0]
        checkbox = visible_item(page, "postSelect_" + str(first.id))
        assert checkbox is not None
        click(root, checkbox)
        assert controller._selected == {str(first.id)}
        grid = item(root, "favoriteGrid")
        grid.setProperty("contentY", 275.0)
        APPLICATION.processEvents()
        saved = controller._model.items[6]
        card = visible_item(page, "postCard_" + str(saved.post.id))
        assert card is not None
        offset = card.parentItem().y() - grid.property("contentY")
        click(root, card, QPointF(100, 100))
        assert wait(lambda: session.detail_controller.isOpen)
        assert session.detail_controller.current_post == saved.post
        assert item(root, "detailBackButton").property("text") == "返回收藏"
        if resize:
            root.setWidth(820)
            root.setHeight(600)
            APPLICATION.processEvents()
        click(root, item(root, "detailBackButton"))
        assert wait(lambda: not session.detail_controller.isOpen and root.property("activePage") == "favorites")
        assert wait(lambda: grid.property("currentIndex") == 6)
        assert wait(lambda: controller._return_snapshot is None), controller._return_snapshot
        if resize:
            assert wait(lambda: visible_item(page, "postCard_" + str(saved.post.id)) is not None)
            card = visible_item(page, "postCard_" + str(saved.post.id))
            expected = card.parentItem().y() - offset
            assert wait(lambda: math.isclose(grid.property("contentY"), expected, abs_tol=0.01)), (
                grid.property("contentY"), expected, offset, card.parentItem().y(),
                grid.property("width"), grid.property("height"), controller._restore_application,
            )
            assert not math.isclose(expected, 275.0, abs_tol=0.01)
        else:
            assert wait(lambda: math.isclose(grid.property("contentY"), 275.0, abs_tol=0.01))
        assert controller._selected == {str(first.id)}
        assert bridge.postsModel.posts is browse_posts
        assert bridge.postsModel.revision == browse_revision
        assert session.history_repository.list_queries() == before_history
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


@pytest.mark.parametrize("new_query", [False, True])
def test_collection_detail_tag_actions_expose_the_browse_query_entry(tmp_path, new_query):
    engine, root, bridge, session, repository, network = start(
        tmp_path, (FavoriteCandidate.for_post(post(1001)),)
    )
    try:
        page = enter(root, session)
        card = visible_item(page, "postCard_1001")
        click(root, card, QPointF(100, 100))
        assert wait(lambda: session.detail_controller.isOpen)
        tag = item(root, "detailTag_cat")
        reveal_detail_tag(root, tag)
        click(root, tag)
        action = item(root, "detailTagNewQuery_cat" if new_query else "detailTagAddToQuery_cat")
        click(root, action)
        assert wait(lambda: not session.detail_controller.isOpen)
        assert root.property("activePage") == "browse"
        assert "cat" in item(root, "tagsInput").property("text")
        if new_query:
            assert wait(lambda: len(network.requests) == 1)
        else:
            assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)
