import json
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QSignalSpy

from danbooru_multi_search.core.pool_pages import PoolPageSlot, PoolPageState, PoolPostMetadata
from danbooru_multi_search.core.pool_reader import reader_range
from danbooru_multi_search.core.pools import DanbooruPool, PoolCategory, PoolPageReference
from danbooru_multi_search.ui.pool_reader import PoolReaderController
from danbooru_multi_search.ui.pool_reader_models import PoolReaderListModel
from danbooru_multi_search.danbooru.tag_translation import TagTranslationStore
from tests.test_favorite_controller import build, post
from tests.test_pool_page_requests import page_harness
from tests.test_pool_requests import FakeReply
from tests.test_pool_post_parsing import raw_post


class Downloads:
    def capture_snapshot(self, post, *, guard):
        self.post, self.guard = post, guard
        return "snapshot" if guard() else ""


def pool(count=200, *, ids=None, category=PoolCategory.SERIES):
    return DanbooruPool(7, "series", category, ids or tuple(range(1, count + 1)))


def slots(value, numbers):
    return tuple(PoolPageSlot(value.page_at(number), PoolPostMetadata(
        value.page_at(number).post_id, PoolPageState.AVAILABLE, post(value.page_at(number).post_id),
    )) for number in numbers)


@pytest.fixture
def reader(tmp_path):
    value = page_harness()
    repository, favorites = build(tmp_path)
    downloads = Downloads()
    controller = PoolReaderController(value.service, favorites, downloads,
                                      TagTranslationStore(), "https://example.invalid")
    value.controller, value.favorites = controller, favorites
    value.repository, value.downloads = repository, downloads
    yield value
    controller.shutdown()
    favorites.shutdown()
    value.queue.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_virtual_model_keeps_original_rows_without_reset_and_bounds_cache():
    model = PoolReaderListModel()
    value = pool(25000)
    model.set_pool(value)
    reset = QSignalSpy(model.modelReset)
    changed = QSignalSpy(model.dataChanged)
    model.update(slots(value, range(1, 121)), {1})
    model.update(slots(value, range(121, 161)), {1})
    assert model.rowCount() == 25000 and len(model.cache) == 120
    assert 1 in model.cache and reset.count() == 0 and changed.count() > 0
    assert model.mapping(25000)["pageKey"] == "7:25000"
    assert reader_range(1, 25000) == (1, 2, 3)


def test_reader_batches_and_directory_are_bounded_and_independent(reader):
    controller = reader.controller
    controller.open_pool(pool())
    controller.setActive(True)
    first = FakeReply(json.dumps([raw_post(number) for number in (1, 2, 3)]).encode())
    reader.manager.replies.append(first)
    reader.scheduler.run_ready()
    controller.setDirectoryOpen(True)
    assert controller.showDirectory(101, controller.revision)
    assert controller.currentPage == 1 and len(reader.manager.requests) == 1
    first.finish()
    directory = FakeReply(json.dumps([raw_post(number) for number in range(101, 106)]).encode())
    reader.manager.replies.append(directory)
    reader.scheduler.run_ready()
    directory.finish()
    assert controller.currentPage == 1
    assert [row["pageNumber"] for row in controller.directoryRows] == list(range(101, 106))
    assert len(reader.manager.requests) == 2 and len(controller._media) <= 3
    assert len(controller._model.cache) <= 120


def test_mode_zoom_duplicate_positions_and_stale_current_page_actions(reader):
    controller = reader.controller
    value = pool(ids=(9, 9, 3, 4, 5))
    initial = slots(value, range(1, 6))
    controller.open_pool(value, initial)
    controller.setActive(True)
    assert controller.setZoom(1.7, controller.revision)
    old_revision = controller.revision
    assert controller.goToPage(2, old_revision)
    assert controller.current["postKey"] == "9" and controller.current["pageKey"] == "7:2"
    assert controller.setMode("single", controller.revision)
    assert controller.currentPage == 2 and controller.zoom == 1.7
    assert not controller.prepareDownload("7:1", old_revision)
    assert controller.prepareDownload("7:2", controller.revision)
    guard = reader.downloads.guard
    assert guard()
    controller.goToPage(3, controller.revision)
    assert not guard()
    assert controller.setMode("continuous", controller.revision)
    assert controller.currentPage == 3 and controller.zoom == 1.7


def test_hidden_reader_releases_media_and_ignores_late_metadata(reader):
    controller = reader.controller
    value = pool()
    controller.open_pool(value, slots(value, (1, 2)))
    controller.setActive(True)
    reply = FakeReply(json.dumps([raw_post(3)]).encode())
    reader.manager.replies.append(reply)
    reader.scheduler.run_ready()
    assert len(controller._media) == 2
    controller.setActive(False)
    before = tuple(controller._model.cache)
    reply.finish()
    assert not controller._media and tuple(controller._model.cache) == before
    assert reply.abort_count == 1 and not reader.queue.has_work


@pytest.mark.parametrize("action", ["hide", "close"])
def test_media_shutdown_notification_cannot_recreate_old_pages(reader, action):
    controller = reader.controller
    value = pool()
    controller.open_pool(value, slots(value, range(1, 6)))
    controller.setActive(True)
    controller._media[1].openChanged.connect(
        lambda: controller.shutdown() if action == "close" else controller.setActive(False)
    )
    assert not controller.goToPage(5, controller.revision)
    assert not controller._media and not controller.active
    assert not reader.manager.requests


def test_mode_notification_hide_suppresses_stale_position_signal(reader):
    controller = reader.controller
    value = pool()
    controller.open_pool(value, slots(value, range(1, 6)))
    controller.setActive(True)
    positions = QSignalSpy(controller.positionRequested)
    controller.changed.connect(lambda: controller.setActive(False))
    assert not controller.setMode("single", controller.revision)
    assert positions.count() == 0 and not controller._media


def test_scroll_observation_updates_page_without_requesting_position(reader):
    controller = reader.controller
    value = pool()
    controller.open_pool(value, slots(value, range(1, 6)))
    controller.setActive(True)
    positions = QSignalSpy(controller.positionRequested)
    controller.observePage(2)
    assert controller.currentPage == 2 and positions.count() == 0
    assert controller.goToPage(3, controller.revision)
    assert positions.count() == 1


def test_collection_is_rejected_and_single_page_retry_retains_other_slot(reader):
    controller = reader.controller
    assert not controller.open_pool(pool(category=PoolCategory.COLLECTION))
    value = pool(ids=(9, 9, 3, 4, 5))
    controller.open_pool(value, slots(value, range(1, 6)))
    controller.setActive(True)
    before = controller._model.cache[2]
    reply = FakeReply(json.dumps([raw_post(9)]).encode())
    reader.manager.replies.append(reply)
    assert controller.retryPage("7:1", controller.revision)
    reader.scheduler.run_ready()
    reply.finish()
    assert controller._model.cache[2] is before
    assert len(reader.manager.requests) == 1
