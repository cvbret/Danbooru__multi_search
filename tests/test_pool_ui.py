import json
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent

from danbooru_multi_search.danbooru.pools import PoolRequestService
from danbooru_multi_search.ui.pools import PoolBrowserController
from tests.test_favorite_controller import build
from tests.test_pool_page_requests import page_harness
from tests.test_pool_requests import FakeReply
from tests.test_pool_parsing import pool_mapping
from tests.test_pool_post_parsing import raw_post


def browser_harness(tmp_path):
    value = page_harness()
    repository, favorites = build(tmp_path)
    pools = PoolRequestService("https://example.invalid", task_queue=value.queue, network_manager=value.manager)
    memberships = PoolRequestService("https://example.invalid", task_queue=value.queue, network_manager=value.manager)
    browser = PoolBrowserController(pools, memberships, value.service, favorites)
    return SimpleNamespace(**vars(value), repository=repository, favorites=favorites,
                           pools=pools, memberships=memberships, browser=browser)


def pool_replies(value, pool_id=7, ids=(1, 2, 1), *, rows=None, category="series"):
    pool = FakeReply(json.dumps(pool_mapping(pool_id, post_ids=ids, category=category)).encode())
    posts = FakeReply(json.dumps(rows if rows is not None else [raw_post(identifier) for identifier in dict.fromkeys(ids[:40])]).encode())
    value.manager.replies.extend([pool, posts])
    return pool, posts


def finish_pool(value, replies):
    value.scheduler.run_ready()
    replies[0].finish()
    value.scheduler.run_ready()
    if value.browser.model.count:
        replies[1].finish()


@pytest.fixture
def browser(tmp_path):
    value = browser_harness(tmp_path)
    yield value
    value.browser.shutdown()
    value.favorites.shutdown()
    value.queue.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_window_is_bounded_and_preserves_original_positions(browser):
    replies = pool_replies(browser, ids=tuple(range(1, 102)))
    browser.browser.open_pool(7)
    finish_pool(browser, replies)
    assert browser.browser.model.count == 40
    assert browser.browser.poolInfo["postCount"] == 101
    assert [slot.reference.page_number for slot in browser.browser.model.slots] == list(range(1, 41))
    assert len(browser.manager.requests) == 2
    reply = FakeReply(json.dumps([raw_post(41)]).encode())
    browser.manager.replies.append(reply)
    assert browser.browser.showWindow(41, browser.browser.generation)
    browser.scheduler.run_ready()
    reply.finish()
    assert browser.browser.model.slots[0].reference.page_number == 41
    assert browser.browser.model.slots[1].metadata.state.value == "not_returned"
    assert len(browser.manager.requests) == 3


def test_retry_one_duplicate_page_keeps_every_other_slot(browser):
    replies = pool_replies(browser, rows=[raw_post(2)])
    browser.browser.open_pool(7)
    finish_pool(browser, replies)
    before = browser.browser.model.slots
    revision = browser.browser.model.revision
    retry = FakeReply(json.dumps([raw_post(1)]).encode())
    browser.manager.replies.append(retry)
    assert browser.browser.retryPage("7:1", revision)
    browser.scheduler.run_ready()
    retry.finish()
    after = browser.browser.model.slots
    assert after[0].metadata.state.value == "available"
    assert after[1] is before[1] and after[2] is before[2]
    assert after[2].metadata.state.value == "not_returned"
    assert not browser.browser.retryPage("7:1", revision)
    assert "id:1 status:any" in browser.manager.requests[-1].url().toString()


def test_series_pool_favorite_uses_shared_confirmed_facts(browser):
    replies = pool_replies(browser)
    browser.browser.open_pool(7)
    finish_pool(browser, replies)
    assert browser.browser.poolFavoriteState["enabled"]
    assert browser.browser.setPoolFavorite(browser.browser.generation, True)
    assert browser.browser.poolFavoriteState["saved"]
    assert browser.repository.list_favorites()[0].identity.object_key == "7"
    assert browser.browser.setPoolFavorite(browser.browser.generation, False)
    assert not browser.browser.poolFavoriteState["saved"]


def test_collection_pool_has_no_series_favorite_action(browser):
    replies = pool_replies(browser, category="collection")
    browser.browser.open_pool(7)
    finish_pool(browser, replies)
    assert browser.browser.poolInfo["category"] == "collection"
    assert not browser.browser.setPoolFavorite(browser.browser.generation, True)
    assert not browser.repository.list_favorites()


def test_model_notification_replaces_window_before_old_request_submission(browser):
    replies = pool_replies(browser, ids=tuple(range(1, 82)))
    browser.browser.open_pool(7)
    finish_pool(browser, replies)
    fired = False

    def replace():
        nonlocal fired
        if not fired:
            fired = True
            browser.browser.showWindow(81, browser.browser.generation)

    browser.browser.model.changed.connect(replace)
    reply = FakeReply(json.dumps([raw_post(81)]).encode())
    browser.manager.replies.append(reply)
    browser.browser.showWindow(41, browser.browser.generation)
    browser.scheduler.run_ready()
    reply.finish()
    assert browser.browser.firstPage == 81
    assert len(browser.manager.requests) == 3


def test_member_read_failure_and_empty_list_are_visible(browser):
    reply = FakeReply(b"invalid")
    browser.manager.replies.append(reply)
    browser.browser.fetch_memberships(1)
    browser.scheduler.run_ready()
    reply.finish()
    assert browser.browser.membershipError
    empty = FakeReply(b"[]")
    browser.manager.replies.append(empty)
    browser.browser.fetch_memberships(1)
    browser.scheduler.run_ready()
    empty.finish()
    assert not browser.browser.membershipError and not browser.browser.memberships


def test_cancel_prevents_late_page_result_and_retains_retryable_slots(browser):
    replies = pool_replies(browser)
    browser.browser.open_pool(7)
    browser.scheduler.run_ready()
    replies[0].finish()
    browser.scheduler.run_ready()
    browser.browser.cancel()
    replies[1].finish()
    assert all(slot.metadata.state.value == "failed" for slot in browser.browser.model.slots)
    assert not browser.browser.busy


@pytest.mark.parametrize("operation", ["open_pool", "show_window"])
def test_cancel_in_model_notification_prevents_old_submission(browser, operation):
    if operation == "show_window":
        replies = pool_replies(browser)
        browser.browser.open_pool(7)
        finish_pool(browser, replies)
    before = len(browser.manager.requests)
    fired = False

    def cancel():
        nonlocal fired
        if not fired:
            fired = True
            browser.browser.cancel()

    browser.browser.model.changed.connect(cancel)
    if operation == "show_window":
        browser.browser.showWindow(1, browser.browser.generation)
    else:
        browser.browser.open_pool(7)
    browser.scheduler.run_ready()
    assert fired and len(browser.manager.requests) == before
    assert not browser.browser.busy
