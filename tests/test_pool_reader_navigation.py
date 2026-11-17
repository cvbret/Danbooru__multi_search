import pytest
from PySide6.QtCore import QCoreApplication, QEvent

from danbooru_multi_search.danbooru.pool_pages import PoolPageRequestService
from danbooru_multi_search.ui.pool_reader import PoolReaderController
from tests.test_pool_navigation import values, members
from tests.test_pool_ui import pool_replies, finish_pool
from tests.test_pool_reader import Downloads


@pytest.fixture
def reading(values):
    value = values
    reader = PoolReaderController(
        PoolPageRequestService("https://example.invalid", value.queue, value.manager),
        value.favorites, Downloads(), value.store, "https://example.invalid",
    )
    value.reader = reader
    value.navigation.attach_reader(reader)
    yield value
    reader.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_reader_detail_and_pool_keep_outer_detail_source(reading):
    value = reading
    value.detail_nav.openBrowsePost(0, 123, 0, 800, 600, 1280, 820)
    original = value.detail_nav.detail_controller.current_post
    members(value, value.navigation, 10)
    replies = pool_replies(value, ids=(10, 2, 3))
    value.navigation.openMembership("7", value.browser.membershipRevision)
    finish_pool(value, replies)
    token = value.navigation.returnToken
    assert value.navigation.enterReader(value.browser.generation)
    value.reader.setActive(True)
    assert value.reader.goToPage(2, value.reader.revision)
    assert value.reader.openDetail("7:2", value.reader.revision)
    assert value.detail_nav.detail_controller.current_post.id == 2
    value.reader.setActive(False)
    requested = []
    value.navigation.readerPageRequested.connect(lambda: requested.append(True))
    assert value.navigation.returnToPool()
    assert requested and value.reader.currentPage == 2
    value.navigation.leaveReader()
    assert value.navigation.returnToSource(token)
    assert value.detail_nav.detail_controller.current_post is original
    assert value.detail_nav.gallery_snapshot.content_y == 123


def test_saved_pool_source_returns_to_collection_without_fake_detail(reading):
    value = reading
    replies = pool_replies(value)
    assert value.navigation.openFavoritePool("7")
    finish_pool(value, replies)
    token = value.navigation.returnToken
    assert value.navigation.enterReader(value.browser.generation)
    value.reader.setActive(True)
    value.navigation.leaveReader()
    returned = []
    value.navigation.sourcePageRequested.connect(returned.append)
    assert value.navigation.returnToSource(token)
    assert returned == ["favorites"] and not value.detail_nav.detail_controller.isOpen


def test_old_favorite_click_cannot_replace_sync_new_workspace(reading):
    value = reading
    fired = False

    def replace():
        nonlocal fired
        if not fired:
            fired = True
            value.navigation.openFavoritePool("8")

    value.navigation.changed.connect(replace)
    assert not value.navigation.openFavoritePool("7")
    assert value.browser._pool_target == 8 and value.navigation.returnToken
    assert not value.manager.requests


def test_favorite_return_notification_cannot_cover_new_pool(reading):
    value = reading
    replies = pool_replies(value)
    value.navigation.openFavoritePool("7")
    finish_pool(value, replies)
    value.navigation.enterReader(value.browser.generation)
    value.reader.setActive(True)
    returned = []
    value.navigation.sourcePageRequested.connect(returned.append)
    value.reader.changed.connect(lambda: value.navigation.openFavoritePool("8"))
    token = value.navigation.returnToken
    assert not value.navigation.returnToSource(token)
    assert returned == [] and value.browser._pool_target == 8
