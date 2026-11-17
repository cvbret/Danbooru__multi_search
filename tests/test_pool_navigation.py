import json

import pytest
from PySide6.QtCore import QCoreApplication, QEvent

from danbooru_multi_search.ui.pool_navigation import PoolNavigationController
from tests.test_detail_navigation import _navigation, _post
from tests.test_pool_ui import browser_harness, pool_replies, finish_pool
from tests.test_pool_requests import FakeReply
from tests.test_pool_parsing import pool_mapping


def members(value, navigation, post_id, pool_id=7, ids=(10, 2, 3)):
    reply = FakeReply(json.dumps([pool_mapping(pool_id, post_ids=ids)]).encode())
    value.manager.replies.append(reply)
    assert navigation.requestDetailMemberships(str(post_id), navigation.detailRevision)
    value.scheduler.run_ready()
    reply.finish()


@pytest.fixture
def values(tmp_path):
    value = browser_harness(tmp_path)
    detail_nav, model, store = _navigation(_post(10), _post(20))
    navigation = PoolNavigationController(value.browser, detail_nav)
    value.navigation = navigation
    value.detail_nav, value.model, value.store = detail_nav, model, store
    yield value
    navigation.shutdown()
    value.browser.shutdown()
    detail_nav.shutdown()
    value.favorites.shutdown()
    value.queue.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("source", ["browse", "favorites"])
def test_pool_detail_roundtrip_restores_outer_detail_and_gallery_snapshot(values, source):
    value = values
    detail_nav = value.detail_nav
    original = value.model.post_at(0)
    if source == "browse":
        assert detail_nav.openBrowsePost(0, 371, 1, 800, 600, 1280, 820)
    else:
        assert detail_nav.open_saved_post(original, source_page="favorites")
    snapshot = detail_nav.gallery_snapshot
    media_generation = detail_nav.detail_controller.mediaGeneration
    members(value, value.navigation, 10)
    replies = pool_replies(value, ids=(10, 2, 3))
    assert value.navigation.openMembership("7", value.browser.membershipRevision)
    token = value.navigation.returnToken
    finish_pool(value, replies)
    assert value.navigation.openPage("7:2", value.browser.model.revision)
    assert detail_nav.sourcePage == "pool" and detail_nav.detail_controller.current_post.id == 2
    assert value.navigation.returnToPool()
    assert value.navigation.returnToSource(token)
    assert detail_nav.detail_controller.current_post is original
    assert detail_nav.sourcePage == source and detail_nav.gallery_snapshot is snapshot
    assert detail_nav.detail_controller.mediaGeneration > media_generation
    assert [value.model.post_at(row).id for row in range(value.model.count)] == [10, 20]


def test_another_pool_from_inner_detail_keeps_original_outer_source(values):
    value = values
    value.detail_nav.openBrowsePost(0, 371, 0, 800, 600, 1280, 820)
    original = value.detail_nav.detail_controller.current_post
    members(value, value.navigation, 10)
    replies = pool_replies(value, ids=(10, 2, 3))
    value.navigation.openMembership("7", value.browser.membershipRevision)
    finish_pool(value, replies)
    token = value.navigation.returnToken
    value.navigation.openPage("7:2", value.browser.model.revision)
    members(value, value.navigation, 2, pool_id=8, ids=(2, 9))
    replies = pool_replies(value, pool_id=8, ids=(2, 9))
    assert value.navigation.openMembership("8", value.browser.membershipRevision)
    finish_pool(value, replies)
    assert value.navigation.returnToken == token
    assert value.navigation.returnToSource(token)
    assert value.detail_nav.detail_controller.current_post is original


def test_stale_detail_click_and_invalidated_return_token_are_rejected(values):
    value = values
    value.detail_nav.openBrowsePost(0, 0, 0, 800, 600, 1280, 820)
    revision = value.navigation.detailRevision
    value.detail_nav.openBrowsePost(1, 0, 0, 800, 600, 1280, 820)
    assert not value.navigation.requestDetailMemberships("10", revision)
    assert not value.manager.requests
    members(value, value.navigation, 20, ids=(20, 2))
    replies = pool_replies(value, ids=(20, 2))
    value.navigation.openMembership("7", value.browser.membershipRevision)
    finish_pool(value, replies)
    token = value.navigation.returnToken
    value.navigation.invalidate()
    assert not value.navigation.returnToSource(token)


def test_membership_cancel_keeps_current_detail_and_media_generation(values):
    value = values
    value.detail_nav.openBrowsePost(0, 0, 0, 800, 600, 1280, 820)
    detail = value.detail_nav.detail_controller
    generation = detail.mediaGeneration
    members(value, value.navigation, 10)
    value.navigation.cancelMemberships()
    assert detail.isOpen and detail.current_post.id == 10
    assert detail.mediaGeneration == generation


@pytest.mark.parametrize("stage,external", [
    ("open", "close"), ("open", "open"),
    ("return", "close"), ("return", "open"),
])
def test_nested_external_navigation_revokes_pool_return_ownership(values, stage, external):
    value = values
    value.detail_nav.openBrowsePost(0, 0, 0, 800, 600, 1280, 820)
    members(value, value.navigation, 10)
    replies = pool_replies(value, ids=(10, 2, 3))
    assert value.navigation.openMembership("7", value.browser.membershipRevision)
    finish_pool(value, replies)
    token = value.navigation.returnToken
    if stage == "return":
        assert value.navigation.openPage("7:2", value.browser.model.revision)
    fired = False

    def navigate():
        nonlocal fired
        if fired:
            return
        fired = True
        if external == "close":
            if value.detail_nav.detail_controller.isOpen:
                value.detail_nav.closeDetail()
            else:
                # Open and close in the notification so the final visible state
                # matches the outer close, while the action ownership changes.
                value.detail_nav.openBrowsePost(1, 0, 0, 800, 600, 1280, 820)
                value.detail_nav.closeDetail()
        else:
            value.detail_nav.openBrowsePost(1, 0, 0, 800, 600, 1280, 820)

    value.detail_nav.detail_controller.openChanged.connect(navigate)
    if stage == "open":
        assert not value.navigation.openPage("7:2", value.browser.model.revision)
    else:
        assert not value.navigation.returnToPool()
    assert fired and not value.navigation.returnToken
    assert not value.navigation.returnToSource(token)
    assert value.detail_nav.detail_controller.current_post.id != 10
