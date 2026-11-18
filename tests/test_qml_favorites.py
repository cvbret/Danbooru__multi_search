from __future__ import annotations

from PySide6.QtCore import QObject, QMetaObject, QPoint, QPointF, Qt
from PySide6.QtTest import QTest

from danbooru_multi_search.app.bootstrap import initialize_runtime
from danbooru_multi_search.core.favorites import FavoriteError, FavoriteIdentity, FavoriteObjectType
from danbooru_multi_search.storage.favorites import FavoriteRepository
from danbooru_multi_search.ui.favorites import post_identity
from tests.m2_qml_harness import create_m2_qml_test_engine, close_m2_qml_test_engine
from tests.test_favorite_controller import APPLICATION, post


class NoNetwork:
    def __init__(self):
        self.requests = []

    def get(self, request):
        self.requests.append(request)
        raise AssertionError("local favorite UI must not request remote data")


def wait(predicate, timeout=2000):
    elapsed = 0
    while elapsed < timeout:
        APPLICATION.processEvents()
        if predicate():
            return True
        QTest.qWait(10)
        elapsed += 10
    return predicate()


def find(root, name):
    pending = [root.contentItem()]
    while pending:
        candidate = pending.pop()
        if candidate.objectName() == name:
            return candidate
        pending.extend(candidate.childItems())
    return root.findChild(QObject, name)


def item(root, name):
    found = find(root, name)
    assert found is not None, name
    return found


def click(root, control, point=None):
    if point is None:
        point = QPointF(control.width() / 2, control.height() / 2)
    scene = control.mapToScene(point)
    QTest.mouseClick(root, Qt.MouseButton.LeftButton, pos=QPoint(round(scene.x()), round(scene.y())))
    APPLICATION.processEvents()


def invoke(control, method):
    assert QMetaObject.invokeMethod(control, method, Qt.ConnectionType.DirectConnection)


def reveal_detail_tag(root, control):
    viewport = item(root, "detailInformationScroll").property("contentItem")
    center = control.mapToScene(QPointF(control.width() / 2, control.height() / 2))
    middle = viewport.mapToScene(QPointF(0, viewport.height() / 2))
    target = viewport.property("contentY") + center.y() - middle.y()
    maximum = max(0, viewport.property("contentHeight") - viewport.height())
    viewport.setProperty("contentY", max(0, min(target, maximum)))
    APPLICATION.processEvents()


def window(tmp_path, posts=None):
    runtime = initialize_runtime(tmp_path)
    network = NoNetwork()
    engine, bridge = create_m2_qml_test_engine(
        APPLICATION, danbooru_base_url="https://example.invalid",
        runtime=runtime, network_manager=network,
    )
    root = engine.rootObjects()[0]
    bridge.postsModel.set_posts(posts or (post(1), post(2)))
    if not wait(lambda: find(root, "postFavorite_1") is not None):
        close_m2_qml_test_engine(engine)
        raise AssertionError("local favorite card was not created")
    root.requestActivate()
    assert QTest.qWaitForWindowExposed(root)
    return engine, root, bridge, bridge.parent(), network


def test_real_card_and_selection_clicks_share_detail_state_without_query(tmp_path):
    engine, root, bridge, session, network = window(tmp_path)
    repository = session.favorite_controller._repository
    original_posts, revision = bridge.postsModel.posts, bridge.postsModel.revision
    try:
        click(root, item(root, "postFavorite_1"))
        assert wait(lambda: repository.get_by_identity(post_identity("1")) is not None)
        assert not session.detail_controller.isOpen
        click(root, item(root, "postSelect_2"))
        assert session.gallery_selection.selectedCount == 1
        assert not session.detail_controller.isOpen
        click(root, item(root, "saveSelectedFavoritesButton"))
        assert repository.get_by_identity(post_identity("2")) is not None
        click(root, item(root, "postCard_1"), QPointF(100, 100))
        assert wait(lambda: session.detail_controller.isOpen)
        assert session.detail_controller.current_post.id == 1
        button = item(root, "detailFavoriteButton")
        assert button.property("text") == "取消本地收藏"
        click(root, button)
        assert repository.get_by_identity(post_identity("1")) is None
        invoke(root, "returnToGallery")
        assert wait(lambda: not session.detail_controller.isOpen)
        assert item(root, "postFavorite_1").property("text") == "☆"
        assert bridge.postsModel.posts is original_posts
        assert bridge.postsModel.revision == revision
        assert bridge.postsModel.posts[0].fav_count == 17
        assert session.history_repository.list_queries() == ()
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


def test_thumbnail_retry_and_old_pressed_card_do_not_open_or_save_new_post(tmp_path):
    engine, root, bridge, session, network = window(tmp_path, (post(1, url="qrc:/missing-local-favorite.png"), post(2)))
    try:
        card = item(root, "postCard_1")
        assert wait(lambda: card.property("thumbnailFailed"))
        token = card.property("thumbnailRebuildToken")
        click(root, item(root, "thumbnailRetry_1"))
        assert not session.detail_controller.isOpen
        assert card.property("thumbnailRebuildToken") > token
        button = item(root, "postFavorite_1")
        point = button.mapToScene(QPointF(button.width() / 2, button.height() / 2))
        position = QPoint(round(point.x()), round(point.y()))
        QTest.mousePress(root, Qt.MouseButton.LeftButton, pos=position)
        bridge.postsModel.set_posts((post(2),))
        APPLICATION.processEvents()
        QTest.mouseRelease(root, Qt.MouseButton.LeftButton, pos=position)
        APPLICATION.processEvents()
        assert session.favorite_controller._repository.list_favorites() == ()
        assert not session.detail_controller.isOpen
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


def test_detail_tag_menus_save_four_categories_and_keep_metadata_query_only(tmp_path):
    engine, root, bridge, session, network = window(tmp_path)
    try:
        click(root, item(root, "postCard_1"), QPointF(100, 100))
        assert wait(lambda: session.detail_controller.isOpen)
        for category, name in (
            ("artist", "sample_artist"), ("character", "sample_character"),
            ("copyright", "sample_series"), ("general", "cat"),
        ):
            tag_button = item(root, "detailTag_" + name)
            reveal_detail_tag(root, tag_button)
            click(root, tag_button)
            assert wait(lambda: find(root, "detailTagFavorite_" + name) is not None)
            action = item(root, "detailTagFavorite_" + name)
            assert action.property("visible")
            assert action.property("enabled")
            click(root, action)
            identity = FavoriteIdentity(FavoriteObjectType.TAG, name)
            assert session.favorite_controller._repository.get_by_identity(identity).tag_category.value == category
            assert session.detail_controller.isOpen
        tag_button = item(root, "detailTag_highres")
        reveal_detail_tag(root, tag_button)
        click(root, tag_button)
        metadata_action = find(root, "detailTagFavorite_highres")
        assert metadata_action is None or not metadata_action.property("visible")
        assert item(root, "detailTagNewQuery_highres").property("visible")
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


def test_failed_batch_keeps_confirmed_stars_selection_and_remote_counts(tmp_path, monkeypatch):
    engine, root, bridge, session, network = window(tmp_path)
    try:
        click(root, item(root, "postFavorite_1"))
        click(root, item(root, "postSelect_1"))
        click(root, item(root, "postSelect_2"))

        def fail(*args, **kwargs):
            raise FavoriteError("injected", "injected batch failure")

        monkeypatch.setattr(session.favorite_controller._repository, "add_favorites", fail)
        click(root, item(root, "saveSelectedFavoritesButton"))
        assert session.gallery_selection.selectedCount == 2
        assert item(root, "postFavorite_1").property("text") == "★"
        assert item(root, "postFavorite_2").property("text") == "☆"
        assert "injected batch failure" in item(root, "favoriteNotice").property("text")
        assert tuple(work.fav_count for work in bridge.postsModel.posts) == (17, 17)
        assert not session.detail_controller.isOpen
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


def test_unknown_read_state_is_visible_disabled_and_can_be_retried(tmp_path, monkeypatch):
    original = FavoriteRepository.get_by_identity

    def fail(*args, **kwargs):
        raise FavoriteError("injected", "injected read failure")

    monkeypatch.setattr(FavoriteRepository, "get_by_identity", fail)
    engine, root, bridge, session, network = window(tmp_path)
    try:
        assert item(root, "postFavorite_1").property("text") == "?"
        assert not item(root, "postFavorite_1").property("enabled")
        assert "injected read failure" in item(root, "favoriteNotice").property("text")
        monkeypatch.setattr(FavoriteRepository, "get_by_identity", original)
        click(root, item(root, "retryFavoriteStatesButton"))
        assert item(root, "postFavorite_1").property("text") == "☆"
        assert item(root, "postFavorite_1").property("enabled")
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


def test_qml_card_uses_exact_text_key_above_javascript_integer_range(tmp_path):
    identifier = 2**60 + 1
    engine, root, bridge, session, network = window(tmp_path, (post(1), post(identifier)))
    try:
        card = item(root, "postCard_" + str(identifier))
        assert card.property("postKey") == str(identifier)
        click(root, item(root, "postFavorite_" + str(identifier)))
        saved = session.favorite_controller._repository.list_favorites()
        assert len(saved) == 1
        assert saved[0].post.id == identifier
        assert saved[0].identity.object_key == str(identifier)
        assert not session.detail_controller.isOpen
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)
