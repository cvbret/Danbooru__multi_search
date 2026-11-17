import base64
import json

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest

from tests.m2_qml_harness import create_m2_qml_test_engine, close_m2_qml_test_engine
from tests.test_favorite_controller import APPLICATION, IMAGE, post
from tests.test_pool_requests import FakeManager, FakeReply
from tests.test_pool_parsing import pool_mapping
from tests.test_pool_post_parsing import raw_post
from tests.test_qml_favorites import click, item, find, wait, invoke


def window():
    network = FakeManager()
    engine, bridge = create_m2_qml_test_engine(
        APPLICATION, danbooru_base_url="https://example.invalid", network_manager=network,
    )
    root = engine.rootObjects()[0]
    root.requestActivate()
    assert QTest.qWaitForWindowExposed(root)
    return engine, root, bridge, bridge.parent(), network


def finish_request(network, reply, count):
    assert wait(lambda: len(network.requests) == count)
    reply.finish()
    APPLICATION.processEvents()


@pytest.mark.parametrize("width,category", [(1280, "series"), (820, "collection")])
def test_real_detail_pool_page_and_source_return_clicks(width, category):
    engine, root, bridge, session, network = window()
    try:
        root.setWidth(width)
        root.setHeight(820 if width > 820 else 600)
        bridge.postsModel.set_posts((post(10),))
        assert wait(lambda: find(root, "postCard_10") is not None)
        original_posts = bridge.postsModel.posts
        click(root, item(root, "postCard_10"), QPointF(100, 100))
        assert wait(lambda: session.detail_controller.isOpen)
        membership = FakeReply(json.dumps([pool_mapping(7, post_ids=(10, 2), category=category)]).encode())
        pool = FakeReply(json.dumps(pool_mapping(7, post_ids=(10, 2), category=category)).encode())
        rows = [raw_post(identifier, preview_file_url=IMAGE, large_file_url=IMAGE, file_url=IMAGE) for identifier in (10, 2)]
        pages = FakeReply(json.dumps(rows).encode())
        network.replies.extend([membership, pool, pages])
        click(root, item(root, "detailPoolsButton"))
        finish_request(network, membership, 1)
        assert wait(lambda: find(root, "poolMembership_7") is not None)
        click(root, item(root, "poolMembership_7"))
        finish_request(network, pool, 2)
        finish_request(network, pages, 3)
        assert wait(lambda: root.property("activePage") == "pool" and find(root, "postCard_2") is not None)
        assert session.pool_browser.poolInfo["category"] == category
        assert item(root, "poolFavoriteButton").property("visible") == (category == "series")
        card = item(root, "postCard_2")
        taps = []
        card.detailRequested.connect(lambda *args: taps.append(args))
        grid = item(root, "poolPageGrid")
        assert grid.height() >= 120
        scene = card.mapToScene(QPointF(100, 100))
        assert 0 <= scene.x() < root.width() and 0 <= scene.y() < root.height()
        click(root, card, QPointF(100, 100))
        assert wait(lambda: session.detail_controller.isOpen and session.detail_controller.current_post.id == 2)
        assert len(taps) == 1
        click(root, item(root, "detailBackButton"))
        assert wait(lambda: not session.detail_controller.isOpen and root.property("activePage") == "pool")
        click(root, item(root, "poolReturnSourceButton"))
        assert wait(lambda: session.detail_controller.isOpen and session.detail_controller.current_post.id == 10)
        click(root, item(root, "detailBackButton"))
        assert wait(lambda: not session.detail_controller.isOpen and root.property("activePage") == "browse")
        assert bridge.postsModel.posts is original_posts
        assert session.history_repository.list_queries() == () and len(network.requests) == 3
    finally:
        close_m2_qml_test_engine(engine)


def test_hidden_pool_page_never_creates_remote_image_requests(local_http_server):
    image = local_http_server(base64.b64decode(IMAGE.split(",", 1)[1]))
    engine, root, bridge, session, network = window()
    try:
        assert not network.requests and image.requests == []
        pool = FakeReply(json.dumps(pool_mapping(7, post_ids=(2,))).encode())
        url = image.base_url + "/local.png"
        pages = FakeReply(json.dumps([raw_post(2, preview_file_url=url, large_file_url=url, file_url=url)]).encode())
        network.replies.extend([pool, pages])
        session.pool_browser.open_pool(7)
        finish_request(network, pool, 1)
        finish_request(network, pages, 2)
        QTest.qWait(200)
        assert image.requests == []
        root.setProperty("activePage", "pool")
        assert wait(lambda: bool(image.requests))
    finally:
        close_m2_qml_test_engine(engine)
