from __future__ import annotations

import json
import os
from urllib.parse import parse_qs, urlsplit

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QNetworkReply

from danbooru_multi_search.danbooru import (
    ContentRating,
    DanbooruGalleryController,
    DanbooruPost,
    DanbooruPostListModel,
    GalleryQueryError,
    PostRole,
    RequestErrorKind,
    RequestState,
    build_gallery_request_spec,
    build_posts_request,
)
from danbooru_multi_search.tasks import (
    BoundedTaskQueue,
    QtScheduler,
    SystemClock,
    TaskQueueConfig,
)


APPLICATION = QGuiApplication.instance() or QGuiApplication([])


class FakeReply(QObject):
    finished = Signal()

    def __init__(
        self,
        payload: bytes,
        *,
        status: int = 200,
        network_error=QNetworkReply.NetworkError.NoError,
        finish_on_abort: bool = True,
        retry_after: bytes = b"",
    ) -> None:
        super().__init__()
        self.payload = payload
        self.status = status
        self.network_error = network_error
        self.finish_on_abort = finish_on_abort
        self.retry_after = retry_after
        self.abort_count = 0

    def abort(self) -> None:
        self.abort_count += 1
        self.network_error = QNetworkReply.NetworkError.OperationCanceledError
        if self.finish_on_abort:
            self.finished.emit()

    def finish(self) -> None:
        self.finished.emit()

    def readAll(self) -> bytes:
        return self.payload

    def error(self):
        return self.network_error

    def errorString(self) -> str:
        return "simulated network error" if self.network_error else ""

    def attribute(self, attribute):
        return self.status

    def rawHeader(self, header_name: str) -> bytes:
        return self.retry_after if header_name == "Retry-After" else b""


class FakeNetworkManager:
    def __init__(self, replies: list[FakeReply]) -> None:
        self.replies = replies
        self.requests = []

    def get(self, request):
        self.requests.append(request)
        return self.replies.pop(0)


def _post_mapping(
    post_id: int,
    *,
    score: int | None = 19,
    fav_count: int | None = 8,
    file_ext: str = "jpg",
) -> dict[str, object]:
    return {
        "id": post_id,
        "preview_file_url": f"https://cdn.example/{post_id}-preview.jpg",
        "large_file_url": f"https://cdn.example/{post_id}-large.jpg",
        "file_url": f"https://cdn.example/{post_id}.{file_ext}",
        "score": score,
        "fav_count": fav_count,
        "rating": "g",
        "image_width": 1200,
        "image_height": 800,
        "file_ext": file_ext,
    }


def _payload(*posts: dict[str, object]) -> bytes:
    return json.dumps(list(posts)).encode("utf-8")


def _dispatch() -> None:
    for _ in range(3):
        APPLICATION.processEvents()


def _request_query(request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(request.url().toString()).query)


@pytest.mark.parametrize(
    ("raw_tags", "rating", "expected_tags", "api_tags"),
    [
        ("  Cat   ", "all", ("cat",), "cat"),
        (
            "artist-name_(artist) 1GIRL",
            "explicit",
            ("artist-name_(artist)", "1girl"),
            "artist-name_(artist) 1girl rating:e",
        ),
        ("blue_sky", "s", ("blue_sky",), "blue_sky rating:s"),
        ("fate/grand_order", "all", ("fate/grand_order",), "fate/grand_order"),
        (
            "girls'_frontline",
            "all",
            ("girls'_frontline",),
            "girls'_frontline",
        ),
        (
            "honkai:_star_rail",
            "all",
            ("honkai:_star_rail",),
            "honkai:_star_rail",
        ),
        ("love_live!", "all", ("love_live!",), "love_live!"),
    ],
)
def test_gallery_query_normalizes_tags_and_applies_free_rating_rule(
    raw_tags,
    rating,
    expected_tags,
    api_tags,
):
    spec = build_gallery_request_spec(raw_tags, rating, page=3, limit=200)

    assert spec.tags == expected_tags
    assert spec.api_tags == api_tags
    assert spec.rating in ContentRating
    assert spec.charged_search_term_count == len(expected_tags)
    assert spec.page == 3
    assert spec.limit == 200


@pytest.mark.parametrize(
    "raw_tags",
    [
        "",
        "cat dog bird",
        "cat OR dog",
        "cat and dog",
        "NOT cat",
        "(cat)",
        "cat (dog)",
        "猫",
        "rating:g",
        "order:score",
        "has:commentary",
        "-cat",
        "~cat",
        "cat*",
        "ſ",
        "Straße",
    ],
)
def test_gallery_query_rejects_unsupported_input(raw_tags):
    with pytest.raises(GalleryQueryError):
        build_gallery_request_spec(raw_tags)


@pytest.mark.parametrize(
    "raw_tags",
    [
        "commentary:any",
        "random:1",
        "filesize:>1m",
        "pool_count:>0",
    ],
)
def test_gallery_query_rejects_current_official_metatags(raw_tags):
    with pytest.raises(GalleryQueryError, match="元标签"):
        build_gallery_request_spec(raw_tags)


def test_gallery_query_keeps_colon_when_it_is_part_of_a_real_tag_name():
    spec = build_gallery_request_spec("honkai:_star_rail")

    assert spec.tags == ("honkai:_star_rail",)
    assert spec.api_tags == "honkai:_star_rail"


@pytest.mark.parametrize(
    ("page", "limit"),
    [(0, 40), (True, 40), (1, 0), (1, 201), (1, True)],
)
def test_gallery_query_rejects_invalid_page_or_limit(page, limit):
    with pytest.raises(GalleryQueryError):
        build_gallery_request_spec("cat", page=page, limit=limit)


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


def test_controller_requests_a_page_and_commits_the_result():
    reply = FakeReply(_payload(_post_mapping(1), _post_mapping(2)))
    manager = FakeNetworkManager([reply])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )

    controller.search("Cat 1girl", "questionable", 2)
    assert controller.state == RequestState.WAITING
    _dispatch()

    assert controller.state == RequestState.LOADING
    assert _request_query(manager.requests[0]) == {
        "tags": ["cat 1girl rating:q"],
        "page": ["1"],
        "limit": ["2"],
    }

    reply.finish()

    assert controller.state == RequestState.SUCCESS
    assert controller.currentPage == 1
    assert controller.pageSize == 2
    assert controller.postsModel.count == 2
    assert controller.postsModel.posts[0].id == 1
    assert controller.canNext is True
    assert controller.canPrevious is False


@pytest.mark.parametrize(
    ("tags", "rating", "limit"),
    [
        ("", "all", 40),
        ("cat dog bird", "all", 40),
        ("cat OR dog", "all", 40),
        ("猫", "all", 40),
        ("rating:g", "all", 40),
        ("cat", "unknown", 40),
        ("cat", "all", 0),
        ("cat", "all", 201),
    ],
)
def test_controller_rejects_invalid_queries_without_network(tags, rating, limit):
    manager = FakeNetworkManager([])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )

    controller.search(tags, rating, limit)
    _dispatch()

    assert controller.state == RequestState.ERROR
    assert controller.errorText
    assert manager.requests == []


def test_empty_next_page_keeps_committed_gallery_and_marks_end():
    first_reply = FakeReply(_payload(_post_mapping(1), _post_mapping(2)))
    empty_reply = FakeReply(b"[]")
    manager = FakeNetworkManager([first_reply, empty_reply])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )

    controller.search("cat", "all", 2)
    _dispatch()
    first_reply.finish()
    controller.nextPage()
    controller.nextPage()
    _dispatch()

    assert controller.currentPage == 1
    assert [post.id for post in controller.postsModel.posts] == [1, 2]
    assert len(manager.requests) == 2
    assert _request_query(manager.requests[1])["page"] == ["2"]

    empty_reply.finish()

    assert controller.state == RequestState.EMPTY
    assert controller.currentPage == 1
    assert [post.id for post in controller.postsModel.posts] == [1, 2]
    assert controller.canNext is False
    assert "保留第 1 页" in controller.statusText


def test_empty_first_page_commits_an_empty_gallery():
    reply = FakeReply(b"[]")
    manager = FakeNetworkManager([reply])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )

    controller.search("cat", "general", 40)
    _dispatch()
    reply.finish()

    assert controller.state == RequestState.EMPTY
    assert controller.currentPage == 1
    assert controller.pageSize == 40
    assert controller.postsModel.count == 0
    assert controller.canRefresh is True
    assert controller.canNext is False


def test_failed_page_and_exact_retry_preserve_then_commit():
    first_reply = FakeReply(_payload(_post_mapping(1), _post_mapping(2)))
    failed_reply = FakeReply(b"server error", status=500)
    retry_reply = FakeReply(_payload(_post_mapping(3)))
    previous_reply = FakeReply(
        _payload(_post_mapping(1), _post_mapping(2))
    )
    manager = FakeNetworkManager(
        [first_reply, failed_reply, retry_reply, previous_reply]
    )
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(
            minimum_interval_seconds=0,
            max_retries=0,
        ),
    )

    controller.search("cat", "sensitive", 2)
    _dispatch()
    first_reply.finish()
    controller.nextPage()
    _dispatch()
    failed_query = _request_query(manager.requests[1])
    failed_reply.finish()

    assert controller.state == RequestState.ERROR
    assert controller.currentPage == 1
    assert [post.id for post in controller.postsModel.posts] == [1, 2]
    assert controller.canRetry is True

    controller.retry()
    _dispatch()

    assert _request_query(manager.requests[2]) == failed_query
    retry_reply.finish()
    assert controller.currentPage == 2
    assert [post.id for post in controller.postsModel.posts] == [3]
    assert controller.canPrevious is True
    assert controller.canNext is False

    controller.previousPage()
    _dispatch()
    assert _request_query(manager.requests[3])["page"] == ["1"]
    previous_reply.finish()
    assert controller.currentPage == 1
    assert [post.id for post in controller.postsModel.posts] == [1, 2]


def test_new_query_and_failed_retry_preserve_committed_snapshot():
    first_reply = FakeReply(_payload(_post_mapping(1)))
    failed_reply = FakeReply(b"server error", status=500)
    failed_retry = FakeReply(b"server error again", status=500)
    manager = FakeNetworkManager([first_reply, failed_reply, failed_retry])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(
            minimum_interval_seconds=0,
            max_retries=0,
        ),
    )

    controller.search("cat", "general", 2)
    _dispatch()
    first_reply.finish()
    controller.search("dog", "explicit", 3)
    _dispatch()

    assert [post.id for post in controller.postsModel.posts] == [1]
    assert controller.currentPage == 1
    assert controller.pageSize == 2
    failed_request = _request_query(manager.requests[1])
    failed_reply.finish()
    assert controller.canRetry is True

    controller.retry()
    _dispatch()
    assert _request_query(manager.requests[2]) == failed_request
    failed_retry.finish()

    assert controller.state == RequestState.ERROR
    assert controller.currentPage == 1
    assert controller.pageSize == 2
    assert [post.id for post in controller.postsModel.posts] == [1]
    assert controller.canRetry is True


def test_refresh_failure_uses_committed_snapshot_and_preserves_gallery():
    first_reply = FakeReply(_payload(_post_mapping(4)))
    refresh_reply = FakeReply(b"server error", status=500)
    manager = FakeNetworkManager([first_reply, refresh_reply])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(
            minimum_interval_seconds=0,
            max_retries=0,
        ),
    )

    controller.search("blue_sky", "explicit", 3)
    _dispatch()
    first_reply.finish()
    committed_query = _request_query(manager.requests[0])
    controller.refresh()
    _dispatch()

    assert _request_query(manager.requests[1]) == committed_query
    refresh_reply.finish()
    assert controller.currentPage == 1
    assert [post.id for post in controller.postsModel.posts] == [4]
    assert controller.canRetry is True


def test_cancelled_page_and_late_reply_cannot_replace_gallery():
    first_reply = FakeReply(_payload(_post_mapping(1), _post_mapping(2)))
    cancelled_reply = FakeReply(
        _payload(_post_mapping(3)),
        finish_on_abort=False,
    )
    manager = FakeNetworkManager([first_reply, cancelled_reply])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )

    controller.search("cat", "all", 2)
    _dispatch()
    first_reply.finish()
    controller.nextPage()
    _dispatch()
    controller.cancelCurrentRequest()

    assert cancelled_reply.abort_count == 1
    assert controller.state == RequestState.CANCELLED
    assert controller.currentPage == 1
    assert [post.id for post in controller.postsModel.posts] == [1, 2]

    cancelled_reply.finish()
    assert controller.currentPage == 1
    assert [post.id for post in controller.postsModel.posts] == [1, 2]


def test_waiting_request_can_be_cancelled_before_network_start():
    reply = FakeReply(_payload(_post_mapping(1)))
    manager = FakeNetworkManager([reply])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
    )
    outcomes = []
    controller.pageRequestCompleted.connect(outcomes.append)

    controller.search("cat", "all", 40)
    controller.cancelCurrentRequest()
    _dispatch()

    assert manager.requests == []
    assert controller.state == RequestState.CANCELLED
    assert len(outcomes) == 1
    assert outcomes[0].error_kind == RequestErrorKind.CANCELLED


def test_retry_wait_can_be_cancelled_without_starting_another_request():
    unavailable_reply = FakeReply(
        b"unavailable",
        status=503,
        retry_after=b"60",
    )
    unused_reply = FakeReply(_payload(_post_mapping(1)))
    manager = FakeNetworkManager([unavailable_reply, unused_reply])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(
            minimum_interval_seconds=0,
            max_retries=1,
        ),
    )

    controller.search("cat", "all", 40)
    _dispatch()
    unavailable_reply.finish()

    assert controller.state == RequestState.RETRY_WAIT
    assert "服务暂时不可用" in controller.statusText
    controller.cancelCurrentRequest()

    assert controller.state == RequestState.CANCELLED
    assert len(manager.requests) == 1


def test_new_search_cancels_and_isolates_old_response():
    old_reply = FakeReply(_payload(_post_mapping(1)), finish_on_abort=False)
    new_reply = FakeReply(_payload(_post_mapping(9)))
    manager = FakeNetworkManager([old_reply, new_reply])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )

    controller.search("old", "all", 1)
    _dispatch()
    controller.search("new", "general", 1)
    _dispatch()

    assert old_reply.abort_count == 1
    assert len(manager.requests) == 2
    new_reply.finish()
    assert [post.id for post in controller.postsModel.posts] == [9]

    old_reply.finish()
    assert [post.id for post in controller.postsModel.posts] == [9]
    assert controller.state == RequestState.SUCCESS


def test_invalid_item_fails_whole_page_and_preserves_committed_gallery():
    first_reply = FakeReply(_payload(_post_mapping(1)))
    invalid_reply = FakeReply(
        _payload(_post_mapping(2), _post_mapping(3, score="invalid"))
    )
    manager = FakeNetworkManager([first_reply, invalid_reply])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )

    controller.search("cat", "all", 3)
    _dispatch()
    first_reply.finish()
    controller.refresh()
    _dispatch()
    invalid_reply.finish()

    assert controller.state == RequestState.ERROR
    assert controller.canRetry is True
    assert [post.id for post in controller.postsModel.posts] == [1]
    assert controller.currentPage == 1


@pytest.mark.parametrize(
    ("status", "network_error", "expected_text"),
    [
        (429, QNetworkReply.NetworkError.NoError, "频率"),
        (500, QNetworkReply.NetworkError.NoError, "HTTP 500"),
        (200, QNetworkReply.NetworkError.TimeoutError, "超时"),
        (200, QNetworkReply.NetworkError.RemoteHostClosedError, "网络连接"),
    ],
)
def test_controller_exposes_distinct_failure_statuses(
    status,
    network_error,
    expected_text,
):
    reply = FakeReply(b"error", status=status, network_error=network_error)
    manager = FakeNetworkManager([reply])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(
            minimum_interval_seconds=0,
            max_retries=0,
        ),
    )

    controller.search("cat", "all", 40)
    _dispatch()
    reply.finish()

    assert controller.state == RequestState.ERROR
    assert expected_text in controller.errorText
    assert controller.canRetry is True


def test_shutdown_aborts_active_request_and_late_result_is_ignored():
    reply = FakeReply(_payload(_post_mapping(1)), finish_on_abort=False)
    manager = FakeNetworkManager([reply])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )

    controller.search("cat", "all", 40)
    _dispatch()
    controller.shutdown()
    controller.shutdown()

    assert reply.abort_count == 1
    assert controller.state == RequestState.CANCELLED
    assert controller.busy is False
    assert controller.postsModel.count == 0
    reply.finish()
    assert controller.postsModel.count == 0


def test_waiting_state_signal_shutdown_prevents_request_submission():
    reply = FakeReply(_payload(_post_mapping(1)))
    manager = FakeNetworkManager([reply])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )
    triggered = False

    def shutdown_while_waiting() -> None:
        nonlocal triggered
        if not triggered and controller.state == RequestState.WAITING:
            triggered = True
            controller.shutdown()

    controller.stateChanged.connect(shutdown_while_waiting)
    controller.search("cat", "all", 40)
    _dispatch()

    assert triggered is True
    assert manager.requests == []
    assert controller.state == RequestState.CANCELLED
    assert controller.busy is False


def test_initial_waiting_state_signal_new_search_prevents_old_submission():
    replacement_reply = FakeReply(_payload(_post_mapping(9)))
    unused_old_reply = FakeReply(_payload(_post_mapping(1)))
    manager = FakeNetworkManager([replacement_reply, unused_old_reply])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )
    replaced = False

    def replace_initial_waiting_request() -> None:
        nonlocal replaced
        if not replaced and controller.state == RequestState.WAITING:
            replaced = True
            controller.search("new", "general", 1)

    controller.stateChanged.connect(replace_initial_waiting_request)
    controller.search("old", "all", 1)
    _dispatch()

    assert replaced is True
    assert len(manager.requests) == 1
    assert _request_query(manager.requests[0])["tags"] == ["new rating:g"]
    replacement_reply.finish()
    assert [post.id for post in controller.postsModel.posts] == [9]


def test_submit_returned_after_reentrant_new_search_cancels_stale_task():
    new_reply = FakeReply(_payload(_post_mapping(9)))
    unused_old_reply = FakeReply(_payload(_post_mapping(1)))
    manager = FakeNetworkManager([new_reply, unused_old_reply])
    queue = BoundedTaskQueue(
        TaskQueueConfig(minimum_interval_seconds=0),
        clock=SystemClock(),
        scheduler=QtScheduler(),
    )
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        task_queue=queue,
    )
    waiting_signal_count = 0
    replaced = False

    def replace_during_submit_listener() -> None:
        nonlocal waiting_signal_count, replaced
        if controller.state != RequestState.WAITING:
            return
        waiting_signal_count += 1
        if waiting_signal_count == 2 and not replaced:
            replaced = True
            controller.search("new", "general", 1)

    controller.stateChanged.connect(replace_during_submit_listener)
    controller.search("old", "all", 1)
    _dispatch()

    assert replaced is True
    assert len(manager.requests) == 1
    assert _request_query(manager.requests[0])["tags"] == ["new rating:g"]
    new_reply.finish()
    assert [post.id for post in controller.postsModel.posts] == [9]
    assert queue.has_work is False
    assert queue.waiting_count == 0
    assert queue.active_count == 0


def test_model_reset_new_search_cannot_be_overwritten_by_old_completion():
    old_reply = FakeReply(_payload(_post_mapping(1)))
    new_reply = FakeReply(_payload(_post_mapping(9)))
    manager = FakeNetworkManager([old_reply, new_reply])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )
    outcomes = []
    controller.pageRequestCompleted.connect(outcomes.append)
    replaced = False

    def replace_from_model_reset() -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            controller.search("new", "all", 1)

    controller.postsModel.modelReset.connect(replace_from_model_reset)
    controller.search("old", "all", 1)
    _dispatch()
    old_reply.finish()

    assert replaced is True
    assert outcomes == []
    _dispatch()
    assert len(manager.requests) == 2
    new_reply.finish()

    assert [post.id for post in controller.postsModel.posts] == [9]
    assert controller.state == RequestState.SUCCESS
    assert len(outcomes) == 1
    assert outcomes[0].posts[0].id == 9


def test_model_reset_shutdown_suppresses_old_success_state_and_outcome():
    reply = FakeReply(_payload(_post_mapping(1)))
    manager = FakeNetworkManager([reply])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )
    outcomes = []
    controller.pageRequestCompleted.connect(outcomes.append)
    controller.postsModel.modelReset.connect(controller.shutdown)

    controller.search("cat", "all", 1)
    _dispatch()
    reply.finish()

    assert controller.state == RequestState.CANCELLED
    assert controller.busy is False
    assert outcomes == []


def test_count_changed_shutdown_suppresses_old_success_state_and_outcome():
    reply = FakeReply(_payload(_post_mapping(1)))
    manager = FakeNetworkManager([reply])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )
    outcomes = []
    controller.pageRequestCompleted.connect(outcomes.append)
    controller.postsModel.countChanged.connect(controller.shutdown)

    controller.search("cat", "all", 1)
    _dispatch()
    reply.finish()

    assert controller.state == RequestState.CANCELLED
    assert controller.busy is False
    assert outcomes == []


def test_count_changed_new_search_cannot_be_overwritten_by_old_completion():
    old_reply = FakeReply(_payload(_post_mapping(1)))
    new_reply = FakeReply(_payload(_post_mapping(9)))
    manager = FakeNetworkManager([old_reply, new_reply])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )
    outcomes = []
    controller.pageRequestCompleted.connect(outcomes.append)
    replaced = False

    def replace_from_count_changed() -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            controller.search("new", "all", 1)

    controller.postsModel.countChanged.connect(replace_from_count_changed)
    controller.search("old", "all", 1)
    _dispatch()
    old_reply.finish()

    assert replaced is True
    assert outcomes == []
    _dispatch()
    assert len(manager.requests) == 2
    new_reply.finish()

    assert [post.id for post in controller.postsModel.posts] == [9]
    assert controller.state == RequestState.SUCCESS
    assert len(outcomes) == 1
    assert outcomes[0].posts[0].id == 9


def test_error_state_signal_new_search_suppresses_old_error_outcome():
    failed_reply = FakeReply(b"server error", status=500)
    new_reply = FakeReply(_payload(_post_mapping(8)))
    manager = FakeNetworkManager([failed_reply, new_reply])
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(
            minimum_interval_seconds=0,
            max_retries=0,
        ),
    )
    outcomes = []
    controller.pageRequestCompleted.connect(outcomes.append)
    replaced = False

    def replace_failed_request() -> None:
        nonlocal replaced
        if not replaced and controller.state == RequestState.ERROR:
            replaced = True
            controller.search("replacement", "all", 1)

    controller.stateChanged.connect(replace_failed_request)
    controller.search("old", "all", 1)
    _dispatch()
    failed_reply.finish()

    assert outcomes == []
    _dispatch()
    new_reply.finish()
    assert len(outcomes) == 1
    assert outcomes[0].posts[0].id == 8


def test_closed_shared_queue_disables_all_request_entry_points():
    page_one = FakeReply(_payload(_post_mapping(1)))
    page_two = FakeReply(_payload(_post_mapping(2)))
    failed_refresh = FakeReply(b"server error", status=500)
    manager = FakeNetworkManager([page_one, page_two, failed_refresh])
    scheduler = QtScheduler()
    queue = BoundedTaskQueue(
        TaskQueueConfig(minimum_interval_seconds=0, max_retries=0),
        clock=SystemClock(),
        scheduler=scheduler,
    )
    controller = DanbooruGalleryController(
        "https://example.invalid",
        network_manager=manager,
        task_queue=queue,
    )

    controller.search("cat", "all", 1)
    _dispatch()
    page_one.finish()
    controller.nextPage()
    _dispatch()
    page_two.finish()
    controller.refresh()
    _dispatch()
    failed_refresh.finish()

    assert controller.canPrevious is True
    assert controller.canNext is True
    assert controller.canRefresh is True
    assert controller.canRetry is True
    request_count = len(manager.requests)

    queue.shutdown()

    assert controller.canPrevious is False
    assert controller.canNext is False
    assert controller.canRefresh is False
    assert controller.canRetry is False
    controller.previousPage()
    controller.nextPage()
    controller.refresh()
    controller.retry()
    controller.search("dog", "all", 1)
    _dispatch()
    assert len(manager.requests) == request_count
