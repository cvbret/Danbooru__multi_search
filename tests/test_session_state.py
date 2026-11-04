from __future__ import annotations

import os
import json
from datetime import datetime, timedelta, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest
from PySide6.QtTest import QTest

from danbooru_multi_search.core.settings import AppSettings
from danbooru_multi_search.danbooru import (
    DanbooruGalleryController,
    DanbooruPost,
    GalleryPageOutcome,
    GalleryRequestAccepted,
    GalleryRequestOrigin,
    RequestState,
    build_gallery_request_spec,
)
from danbooru_multi_search.storage import GalleryPosition, HistoryRepository
from danbooru_multi_search.storage import HistoryError
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.tasks import (
    BoundedTaskQueue,
    SystemClock,
    TaskQueueConfig,
)
from danbooru_multi_search.ui.session_state import BrowserSessionController
from danbooru_multi_search.ui.window_state import ScreenGeometry, WindowGeometry


APPLICATION = QGuiApplication.instance() or QGuiApplication([])
FIXED_NOW = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)


class _NoNetworkManager:
    requests = []

    def get(self, request):
        raise AssertionError("startup restore must not request the network")


def _build(tmp_path, network_manager=None, *, task_queue=None):
    database = Database(tmp_path / "application.sqlite3")
    database.initialize()
    repository = HistoryRepository(
        database,
        clock=lambda: datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    gallery_kwargs = {
        "network_manager": network_manager or _NoNetworkManager(),
    }
    if task_queue is None:
        gallery_kwargs["queue_config"] = TaskQueueConfig(
            minimum_interval_seconds=0
        )
    else:
        gallery_kwargs["task_queue"] = task_queue
    gallery = DanbooruGalleryController(
        "https://example.invalid",
        **gallery_kwargs,
    )
    controller = BrowserSessionController(
        repository,
        gallery,
        AppSettings(),
        (ScreenGeometry(WindowGeometry(0, 0, 1920, 1040), primary=True),),
    )
    return repository, gallery, controller


class _DeferredReply(QObject):
    metaDataChanged = Signal()
    readyRead = Signal()
    finished = Signal()

    def __init__(self, body: bytes, *, status: int = 200) -> None:
        super().__init__()
        self._body = body
        self._status = status
        self.abort_count = 0

    def abort(self) -> None:
        self.abort_count += 1

    def readAll(self) -> bytes:
        return self._body

    def error(self):
        return QNetworkReply.NetworkError.NoError

    def errorString(self) -> str:
        return ""

    def attribute(self, attribute):
        if attribute == QNetworkRequest.Attribute.HttpStatusCodeAttribute:
            return self._status
        return None

    def rawHeader(self, _name) -> bytes:
        return b""

    def deleteLater(self) -> None:
        return

    def finish(self) -> None:
        self.finished.emit()


class _DeferredNetworkManager:
    def __init__(self, replies: list[_DeferredReply]) -> None:
        self._replies = list(replies)
        self.requests: list[QNetworkRequest] = []

    def get(self, request: QNetworkRequest) -> _DeferredReply:
        self.requests.append(request)
        return self._replies.pop(0)


class _CompletedScheduledCall:
    @property
    def active(self) -> bool:
        return False

    def cancel(self) -> None:
        return


class _InlineScheduler:
    def call_later(self, delay_seconds, callback):
        assert delay_seconds == 0
        callback()
        return _CompletedScheduledCall()


def _inline_task_queue() -> BoundedTaskQueue:
    return BoundedTaskQueue(
        TaskQueueConfig(minimum_interval_seconds=0, max_retries=0),
        clock=SystemClock(),
        scheduler=_InlineScheduler(),
    )


def _wait_until(predicate, timeout_ms: int = 2_000) -> bool:
    elapsed = 0
    while elapsed < timeout_ms:
        APPLICATION.processEvents()
        if predicate():
            return True
        QTest.qWait(10)
        elapsed += 10
    return predicate()


def _post_payload(post_id: int) -> bytes:
    return json.dumps(
        [
            {
                "id": post_id,
                "preview_file_url": f"https://example.invalid/{post_id}-p.jpg",
                "large_file_url": f"https://example.invalid/{post_id}-l.jpg",
                "file_url": f"https://example.invalid/{post_id}.jpg",
                "score": 0,
                "fav_count": 0,
                "rating": "g",
                "image_width": 100,
                "image_height": 100,
                "file_ext": "jpg",
            }
        ]
    ).encode("utf-8")


def _post(post_id: int) -> DanbooruPost:
    return DanbooruPost(
        id=post_id,
        preview_url=f"qrc:/post-{post_id}.jpg",
        large_file_url=None,
        file_url=None,
        score=0,
        fav_count=0,
        rating="g",
        width=100,
        height=100,
        file_ext="jpg",
    )


def _store_query(repository, tag: str, used_at: datetime):
    query = repository.record_accepted_query(
        display_text=tag,
        normalized_tags=(tag,),
        rating="all",
        page_size=40,
        page=1,
        count_tag_usage=True,
        used_at=used_at,
    )
    assert query is not None
    assert repository.mark_query_committed(query.id, 1)
    return query


def test_startup_fills_latest_query_without_network(tmp_path):
    repository, gallery, _controller = _build(tmp_path)
    stored = repository.record_accepted_query(
        display_text="cat dog",
        normalized_tags=("cat", "dog"),
        rating="g",
        page_size=80,
        page=3,
        count_tag_usage=True,
    )
    repository.mark_query_committed(stored.id, 2)

    restored = BrowserSessionController(
        repository,
        gallery,
        AppSettings(),
        (ScreenGeometry(WindowGeometry(0, 0, 1920, 1040), primary=True),),
    )

    assert restored.initialTags == "cat dog"
    assert restored.initialRating == "general"
    assert restored.initialPageSize == 80
    assert restored.canRestoreSelected is True
    assert gallery._network_manager.requests == []


def test_clear_history_invalidates_delayed_save_without_recreation(tmp_path):
    repository, _gallery, controller = _build(tmp_path)
    query = repository.record_accepted_query(
        display_text="cat",
        normalized_tags=("cat",),
        rating="all",
        page_size=40,
        page=1,
        count_tag_usage=True,
    )
    repository.mark_query_committed(query.id, 1)
    controller._active_history_id = query.id
    controller.updateGalleryPosition(1, 0, 0.0, 10.0)
    old_generation = controller._scheduled_generation

    controller.clearHistory()
    controller._save_state(old_generation)

    assert repository.list_queries() == ()
    assert repository.load_session().active_query_id is None


def test_maximized_update_preserves_last_normal_geometry(tmp_path):
    repository, _gallery, controller = _build(tmp_path)
    controller.updateWindowState(40, 50, 1000, 700, False, False)
    controller.updateWindowState(0, 0, 1920, 1040, True, False)
    controller.save_final_state()

    session = repository.load_session()
    assert session.is_maximized is True
    assert session.geometry is not None
    assert (session.geometry.x, session.geometry.y) == (40, 50)
    assert (session.geometry.width, session.geometry.height) == (1000, 700)


def test_final_save_failure_does_not_stop_controller_shutdown(tmp_path, monkeypatch):
    _repository, _gallery, controller = _build(tmp_path)

    def fail_save(**kwargs):
        raise HistoryError("simulated failure")

    monkeypatch.setattr(controller._repository, "save_browser_state", fail_save)

    controller.begin_shutdown()

    assert controller._closed is True
    assert "simulated failure" in controller.noticeText


def _accepted_event(
    request_id: int,
    tag: str,
    *,
    origin: GalleryRequestOrigin,
    accepted_at: datetime,
    restore_history_id: int | None = None,
    display_text: str | None = None,
    page: int = 1,
) -> GalleryRequestAccepted:
    return GalleryRequestAccepted(
        request_id=request_id,
        spec=build_gallery_request_spec(tag, "all", page=page, limit=40),
        display_text=display_text or tag,
        origin=origin,
        restore_history_id=restore_history_id,
        preserve_gallery_position=(origin == GalleryRequestOrigin.RESTORE),
        accepted_at=accepted_at,
    )


def test_out_of_order_accepted_events_keep_new_restore_plan_and_recency(tmp_path):
    repository, _gallery, controller = _build(tmp_path)
    newer_time = FIXED_NOW + timedelta(seconds=1)
    controller._request_accepted(
        _accepted_event(
            2,
            "newer",
            origin=GalleryRequestOrigin.RESTORE,
            restore_history_id=90,
            accepted_at=newer_time,
        )
    )
    controller._request_accepted(
        _accepted_event(
            1,
            "older",
            origin=GalleryRequestOrigin.SEARCH,
            accepted_at=FIXED_NOW,
        )
    )

    assert controller._restore_plan is not None
    assert controller._restore_plan.request_id == 2
    assert [query.display_text for query in repository.list_queries()] == [
        "newer",
        "older",
    ]


def test_clear_history_rejects_accepted_callback_already_in_flight(tmp_path):
    repository, gallery, controller = _build(tmp_path)
    gallery._request_generation = 3

    controller.clearHistory()
    controller._request_accepted(
        _accepted_event(
            3,
            "late",
            origin=GalleryRequestOrigin.SEARCH,
            accepted_at=FIXED_NOW,
        )
    )

    assert repository.list_queries() == ()
    assert repository.list_tags() == ()


def test_out_of_order_same_query_does_not_roll_back_newer_metadata(tmp_path):
    repository, _gallery, controller = _build(tmp_path)
    controller._request_accepted(
        _accepted_event(
            2,
            "cat",
            origin=GalleryRequestOrigin.SEARCH,
            accepted_at=FIXED_NOW + timedelta(seconds=1),
            display_text=" CAT ",
            page=2,
        )
    )
    controller._request_accepted(
        _accepted_event(
            1,
            "cat",
            origin=GalleryRequestOrigin.SEARCH,
            accepted_at=FIXED_NOW,
            display_text="cat",
            page=1,
        )
    )

    query = repository.list_queries()[0]
    assert query.display_text == " CAT "
    assert query.last_submitted_page == 2
    assert repository.list_tags()[0].use_count == 2


def test_failed_clear_keeps_session_state_and_delayed_save(tmp_path, monkeypatch):
    repository, _gallery, controller = _build(tmp_path)
    query = repository.record_accepted_query(
        display_text="cat",
        normalized_tags=("cat",),
        rating="all",
        page_size=40,
        page=1,
        count_tag_usage=True,
    )
    controller._active_history_id = query.id
    controller.updateGalleryPosition(1, 0, 0.0, 12.0)
    generation = controller._state_generation
    assert controller._save_timer.isActive()

    def fail_clear() -> None:
        raise HistoryError("simulated clear failure")

    monkeypatch.setattr(repository, "clear_history", fail_clear)
    controller.clearHistory()

    assert controller._state_generation == generation
    assert controller._active_history_id == query.id
    assert controller._save_timer.isActive()
    assert repository.get_query(query.id) is not None


def test_reentrant_history_selection_only_applies_latest_controls(tmp_path):
    repository, _gallery, controller = _build(tmp_path)
    repository.record_accepted_query(
        display_text="first",
        normalized_tags=("first",),
        rating="all",
        page_size=40,
        page=1,
        count_tag_usage=True,
        used_at=FIXED_NOW,
    )
    repository.record_accepted_query(
        display_text="second",
        normalized_tags=("second",),
        rating="all",
        page_size=40,
        page=1,
        count_tag_usage=True,
        used_at=FIXED_NOW + timedelta(seconds=1),
    )
    controller._refresh_history()
    controls: list[tuple[str, str, int]] = []
    nested = False

    def select_older() -> None:
        nonlocal nested
        if not nested:
            nested = True
            controller.selectHistory(1)

    controller.selectionChanged.connect(select_older)
    controller.controlsRequested.connect(
        lambda tags, rating, page_size: controls.append(
            (tags, rating, page_size)
        )
    )

    controller.selectHistory(0)

    assert controls == [("first", "all", 40)]
    assert controller._pending_history_id == controller.historyModel.items[1].id


def test_restore_application_token_is_invalidated_by_edit_or_model_change(tmp_path):
    repository, gallery, controller = _build(tmp_path)
    query = repository.record_accepted_query(
        display_text="cat",
        normalized_tags=("cat",),
        rating="all",
        page_size=40,
        page=1,
        count_tag_usage=True,
    )
    controller._restore_ready = query
    plan = controller.takeGalleryRestorePlan(0.0, 1000.0, 300.0)
    assert controller.galleryRestorePlanCurrent(
        plan["token"],
        plan["modelRevision"],
    )

    controller.controlsEdited()
    assert not controller.galleryRestorePlanCurrent(
        plan["token"],
        plan["modelRevision"],
    )

    controller._restore_ready = query
    replacement = controller.takeGalleryRestorePlan(0.0, 1000.0, 300.0)
    gallery.postsModel.set_posts(())
    assert not controller.galleryRestorePlanCurrent(
        replacement["token"],
        replacement["modelRevision"],
    )


def test_invalid_saved_anchor_index_falls_back_to_absolute_position(tmp_path):
    repository, _gallery, controller = _build(tmp_path)
    query = repository.record_accepted_query(
        display_text="cat",
        normalized_tags=("cat",),
        rating="all",
        page_size=40,
        page=1,
        count_tag_usage=True,
    )
    repository.update_gallery_position(
        GalleryPosition(query.id, 999, 8, -12.0, 240.0)
    )
    controller._restore_ready = repository.get_query(query.id)

    plan = controller.takeGalleryRestorePlan(0.0, 1000.0, 300.0)

    assert plan["available"] is True
    assert plan["index"] == -1
    assert plan["hasAnchor"] is False
    assert plan["absoluteContentY"] == 240.0


@pytest.mark.parametrize(
    "invalidation",
    ["edit", "select", "cancel", "clear"],
)
def test_restore_invalidation_cancels_exact_request_and_ignores_late_success(
    tmp_path,
    invalidation,
):
    reply = _DeferredReply(_post_payload(2))
    manager = _DeferredNetworkManager([reply])
    repository, gallery, controller = _build(tmp_path, manager)
    restored = _store_query(repository, "cat", FIXED_NOW)
    other = _store_query(repository, "dog", FIXED_NOW + timedelta(seconds=1))
    controller._refresh_history()
    gallery.postsModel.set_posts((_post(1),))
    restore_row = next(
        index
        for index, item in enumerate(controller.historyModel.items)
        if item.id == restored.id
    )
    other_row = next(
        index
        for index, item in enumerate(controller.historyModel.items)
        if item.id == other.id
    )
    controller.selectHistory(restore_row)
    controller.restoreSelected()
    assert _wait_until(lambda: len(manager.requests) == 1)
    request_id = gallery.request_generation

    if invalidation == "edit":
        controller.controlsEdited()
    elif invalidation == "select":
        controller.selectHistory(other_row)
    elif invalidation == "cancel":
        controller.cancelRestore()
    else:
        controller.clearHistory()

    assert reply.abort_count == 1
    assert controller._active_restore_request_id is None
    assert request_id not in controller._accepted_history
    assert gallery.canRetry is False

    reply.finish()
    APPLICATION.processEvents()

    assert [post.id for post in gallery.postsModel.posts] == [1]
    assert gallery.canRetry is False


def test_invalidated_restore_failure_clears_retry_snapshot(tmp_path):
    failed_reply = _DeferredReply(b"server error", status=500)
    manager = _DeferredNetworkManager([failed_reply])
    repository, gallery, controller = _build(tmp_path, manager)
    restored = _store_query(repository, "cat", FIXED_NOW)
    controller._refresh_history()
    controller.selectHistory(0)
    controller.restoreSelected()
    assert _wait_until(lambda: len(manager.requests) == 1)
    failed_reply.finish()
    assert _wait_until(lambda: gallery.canRetry)

    controller.controlsEdited()
    request_count = len(manager.requests)
    gallery.retry()
    APPLICATION.processEvents()

    assert restored.id is not None
    assert gallery.canRetry is False
    assert len(manager.requests) == request_count


def test_invalidated_restore_retry_is_aborted_and_late_result_is_ignored(tmp_path):
    failed_reply = _DeferredReply(b"server error", status=500)
    retry_reply = _DeferredReply(_post_payload(3))
    manager = _DeferredNetworkManager([failed_reply, retry_reply])
    repository, gallery, controller = _build(tmp_path, manager)
    _store_query(repository, "cat", FIXED_NOW)
    controller._refresh_history()
    gallery.postsModel.set_posts((_post(1),))
    controller.selectHistory(0)
    controller.restoreSelected()
    assert _wait_until(lambda: len(manager.requests) == 1)
    failed_reply.finish()
    assert _wait_until(lambda: gallery.canRetry)

    gallery.retry()
    assert _wait_until(lambda: len(manager.requests) == 2)
    retry_request_id = gallery.request_generation
    assert controller._active_restore_request_id == retry_request_id
    controller.cancelRestore()

    assert retry_reply.abort_count == 1
    assert gallery.canRetry is False
    retry_reply.finish()
    APPLICATION.processEvents()
    assert [post.id for post in gallery.postsModel.posts] == [1]


def _invalidate_restore_from_waiting(
    controller: BrowserSessionController,
    invalidation: str,
    other_history_id: int,
) -> None:
    if invalidation == "edit":
        controller.controlsEdited()
    elif invalidation == "select":
        other_row = next(
            index
            for index, item in enumerate(controller.historyModel.items)
            if item.id == other_history_id
        )
        controller.selectHistory(other_row)
    elif invalidation == "cancel":
        controller.cancelRestore()
    else:
        controller.clearHistory()


@pytest.mark.parametrize("invalidation", ["edit", "select", "cancel", "clear"])
@pytest.mark.parametrize("late_status", [200, 500])
def test_first_restore_waiting_signal_cancels_bound_active_request(
    tmp_path,
    invalidation,
    late_status,
):
    body = _post_payload(8) if late_status == 200 else b"server error"
    reply = _DeferredReply(body, status=late_status)
    manager = _DeferredNetworkManager([reply])
    repository, gallery, controller = _build(
        tmp_path,
        manager,
        task_queue=_inline_task_queue(),
    )
    restored = _store_query(repository, "cat", FIXED_NOW)
    other = _store_query(repository, "dog", FIXED_NOW + timedelta(seconds=1))
    controller._refresh_history()
    gallery.postsModel.set_posts((_post(1),))
    restore_row = next(
        index
        for index, item in enumerate(controller.historyModel.items)
        if item.id == restored.id
    )
    accepted_request_ids: list[int] = []
    waiting_request_ids: list[int] = []
    invalidated = False

    gallery.requestAccepted.connect(
        lambda event: accepted_request_ids.append(event.request_id)
    )

    def invalidate_on_first_waiting() -> None:
        nonlocal invalidated
        if invalidated or gallery.state != RequestState.WAITING:
            return
        invalidated = True
        waiting_request_ids.append(gallery.request_generation)
        assert controller._active_restore_request_id == gallery.request_generation
        _invalidate_restore_from_waiting(controller, invalidation, other.id)

    gallery.stateChanged.connect(invalidate_on_first_waiting)
    controller.selectHistory(restore_row)
    controller.restoreSelected()
    QTest.qWait(20)

    assert invalidated is True
    assert waiting_request_ids == accepted_request_ids
    assert len(manager.requests) == 1
    assert reply.abort_count == 1
    assert controller._active_restore_request_id is None
    assert controller._accepted_history == {}
    assert gallery._task_queue.has_work is False
    assert gallery.canRetry is False

    reply.finish()
    gallery.retry()
    APPLICATION.processEvents()

    assert len(manager.requests) == 1
    assert [post.id for post in gallery.postsModel.posts] == [1]
    assert gallery.canRetry is False
    if invalidation == "clear":
        assert repository.list_queries() == ()


@pytest.mark.parametrize("invalidation", ["edit", "select", "cancel", "clear"])
@pytest.mark.parametrize("late_status", [200, 500])
def test_restore_retry_waiting_signal_cancels_bound_active_request(
    tmp_path,
    invalidation,
    late_status,
):
    failed_reply = _DeferredReply(b"server error", status=500)
    retry_body = _post_payload(9) if late_status == 200 else b"server error"
    retry_reply = _DeferredReply(retry_body, status=late_status)
    manager = _DeferredNetworkManager([failed_reply, retry_reply])
    repository, gallery, controller = _build(
        tmp_path,
        manager,
        task_queue=_inline_task_queue(),
    )
    restored = _store_query(repository, "cat", FIXED_NOW)
    other = _store_query(repository, "dog", FIXED_NOW + timedelta(seconds=1))
    controller._refresh_history()
    gallery.postsModel.set_posts((_post(1),))
    restore_row = next(
        index
        for index, item in enumerate(controller.historyModel.items)
        if item.id == restored.id
    )
    controller.selectHistory(restore_row)
    controller.restoreSelected()
    assert _wait_until(lambda: len(manager.requests) == 1)
    failed_reply.finish()
    assert _wait_until(lambda: gallery.canRetry)

    accepted_request_ids: list[int] = []
    waiting_request_ids: list[int] = []
    invalidated = False
    gallery.requestAccepted.connect(
        lambda event: accepted_request_ids.append(event.request_id)
    )

    def invalidate_on_retry_waiting() -> None:
        nonlocal invalidated
        if invalidated or gallery.state != RequestState.WAITING:
            return
        invalidated = True
        waiting_request_ids.append(gallery.request_generation)
        assert controller._active_restore_request_id == gallery.request_generation
        _invalidate_restore_from_waiting(controller, invalidation, other.id)

    gallery.stateChanged.connect(invalidate_on_retry_waiting)
    gallery.retry()
    QTest.qWait(20)

    assert invalidated is True
    assert waiting_request_ids == accepted_request_ids
    assert len(manager.requests) == 2
    assert failed_reply.abort_count == 0
    assert retry_reply.abort_count == 1
    assert controller._active_restore_request_id is None
    assert controller._accepted_history == {}
    assert gallery._task_queue.has_work is False
    assert gallery.canRetry is False

    retry_reply.finish()
    gallery.retry()
    APPLICATION.processEvents()

    assert len(manager.requests) == 2
    assert [post.id for post in gallery.postsModel.posts] == [1]
    assert gallery.canRetry is False
    if invalidation == "clear":
        assert repository.list_queries() == ()


@pytest.mark.parametrize(
    "origin",
    [GalleryRequestOrigin.RESTORE, GalleryRequestOrigin.RETRY],
)
def test_invalidated_restore_acceptance_is_cancelled_before_history_write(
    tmp_path,
    origin,
):
    repository, gallery, controller = _build(tmp_path)
    stored = _store_query(repository, "cat", FIXED_NOW)
    original_queries = repository.list_queries()
    original_tags = repository.list_tags()
    gallery._request_generation = 4
    controller._restore_invalidated_through_request_id = 4

    controller._request_accepted(
        _accepted_event(
            4,
            "cat",
            origin=origin,
            restore_history_id=stored.id,
            accepted_at=FIXED_NOW + timedelta(seconds=1),
        )
    )

    assert gallery.request_generation == 5
    assert repository.list_queries() == original_queries
    assert repository.list_tags() == original_tags
    assert controller._accepted_history == {}
    assert controller._active_restore_request_id is None
    assert gallery.canRetry is False


def test_request_accepted_signal_reentry_can_cancel_restore_before_network_start(
    tmp_path,
):
    reply = _DeferredReply(_post_payload(4))
    manager = _DeferredNetworkManager([reply])
    repository, gallery, controller = _build(tmp_path, manager)
    _store_query(repository, "cat", FIXED_NOW)
    controller._refresh_history()
    gallery.postsModel.set_posts((_post(1),))
    reentered = False

    def cancel_from_history_signal() -> None:
        nonlocal reentered
        if not reentered and controller._active_restore_request_id is not None:
            reentered = True
            controller.controlsEdited()

    controller.historyChanged.connect(cancel_from_history_signal)
    controller.selectHistory(0)
    controller.restoreSelected()
    QTest.qWait(20)

    assert reentered is True
    assert manager.requests == []
    assert gallery._task_queue.has_work is False
    assert gallery.canRetry is False
    assert [post.id for post in gallery.postsModel.posts] == [1]


def test_select_history_does_not_overwrite_synchronous_nested_selection(tmp_path):
    reply = _DeferredReply(_post_payload(2))
    manager = _DeferredNetworkManager([reply])
    repository, gallery, controller = _build(tmp_path, manager)
    restoring = _store_query(repository, "restoring", FIXED_NOW)
    first = _store_query(repository, "first", FIXED_NOW + timedelta(seconds=1))
    second = _store_query(repository, "second", FIXED_NOW + timedelta(seconds=2))
    controller._refresh_history()

    def row_for(query_id: int) -> int:
        return next(
            index
            for index, item in enumerate(controller.historyModel.items)
            if item.id == query_id
        )

    controller.selectHistory(row_for(restoring.id))
    controller.restoreSelected()
    assert _wait_until(lambda: len(manager.requests) == 1)
    nested = False
    controls: list[str] = []

    def select_second() -> None:
        nonlocal nested
        if not nested:
            nested = True
            controller.selectHistory(row_for(second.id))

    gallery.stateChanged.connect(select_second)
    controller.controlsRequested.connect(
        lambda tags, _rating, _page_size: controls.append(tags)
    )

    controller.selectHistory(row_for(first.id))

    assert nested is True
    assert controller._pending_history_id == second.id
    assert controls == ["second"]


def test_restore_selected_stops_after_cancel_signal_reentrant_edit(tmp_path):
    first_reply = _DeferredReply(_post_payload(2))
    second_reply = _DeferredReply(_post_payload(3))
    manager = _DeferredNetworkManager([first_reply, second_reply])
    repository, gallery, controller = _build(tmp_path, manager)
    _store_query(repository, "cat", FIXED_NOW)
    controller._refresh_history()
    controller.selectHistory(0)
    controller.restoreSelected()
    assert _wait_until(lambda: len(manager.requests) == 1)
    reentered = False

    def edit_controls() -> None:
        nonlocal reentered
        if not reentered:
            reentered = True
            controller.controlsEdited()

    gallery.stateChanged.connect(edit_controls)
    controller.restoreSelected()
    QTest.qWait(20)

    assert reentered is True
    assert controller._pending_history_id is None
    assert len(manager.requests) == 1
    assert second_reply.abort_count == 0


@pytest.mark.parametrize("cancel_from_notice", [True, False])
def test_restore_history_write_failure_remains_cancellable(
    tmp_path,
    monkeypatch,
    cancel_from_notice,
):
    reply = _DeferredReply(_post_payload(5))
    manager = _DeferredNetworkManager([reply])
    repository, gallery, controller = _build(tmp_path, manager)
    _store_query(repository, "cat", FIXED_NOW)
    controller._refresh_history()
    gallery.postsModel.set_posts((_post(1),))

    def fail_record(**_kwargs):
        raise HistoryError("simulated accepted history failure")

    monkeypatch.setattr(repository, "record_accepted_query", fail_record)
    if cancel_from_notice:
        controller.noticeTextChanged.connect(controller.controlsEdited)
    controller.selectHistory(0)
    controller.restoreSelected()

    if cancel_from_notice:
        QTest.qWait(20)
        assert manager.requests == []
    else:
        assert _wait_until(lambda: len(manager.requests) == 1)
        controller.cancelRestore()
        assert reply.abort_count == 1
    reply.finish()
    APPLICATION.processEvents()

    assert [post.id for post in gallery.postsModel.posts] == [1]
    assert gallery.canRetry is False
    assert controller._active_restore_request_id is None


def test_immediately_evicted_acceptance_has_no_session_page_mapping(tmp_path):
    repository, _gallery, controller = _build(tmp_path)
    for index in range(50):
        query = repository.record_accepted_query(
            display_text=f"tag_{index}",
            normalized_tags=(f"tag_{index}",),
            rating="all",
            page_size=40,
            page=1,
            count_tag_usage=True,
            used_at=FIXED_NOW + timedelta(seconds=index + 1),
        )
        assert query is not None

    controller._request_accepted(
        _accepted_event(
            1,
            "too_old",
            origin=GalleryRequestOrigin.SEARCH,
            accepted_at=FIXED_NOW,
        )
    )

    assert controller._accepted_history == {}
    assert len(repository.list_queries()) == 50
    old_tag = next(
        tag for tag in repository.list_tags() if tag.normalized_tag == "too_old"
    )
    assert old_tag.use_count == 1


def test_clear_history_rejects_search_started_by_cancel_signal_reentry(tmp_path):
    restore_reply = _DeferredReply(_post_payload(2))
    reentrant_reply = _DeferredReply(_post_payload(3))
    manager = _DeferredNetworkManager([restore_reply, reentrant_reply])
    repository, gallery, controller = _build(tmp_path, manager)
    _store_query(repository, "cat", FIXED_NOW)
    controller._refresh_history()
    controller.selectHistory(0)
    controller.restoreSelected()
    assert _wait_until(lambda: len(manager.requests) == 1)
    reentered = False

    def start_search_during_clear() -> None:
        nonlocal reentered
        if not reentered:
            reentered = True
            gallery.search("new")

    gallery.stateChanged.connect(start_search_during_clear)
    controller.clearHistory()
    QTest.qWait(20)

    assert reentered is True
    assert repository.list_queries() == ()
    assert repository.list_tags() == ()
    assert controller.historyModel.count == 0
    assert controller._accepted_history == {}
    assert gallery._task_queue.has_work is False
    assert len(manager.requests) == 1
    restore_reply.finish()
    reentrant_reply.finish()
    APPLICATION.processEvents()
    assert repository.list_queries() == ()
