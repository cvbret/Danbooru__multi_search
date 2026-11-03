"""Validated paged searches and a QML-facing Danbooru gallery model."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Callable

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QCoreApplication,
    QModelIndex,
    QObject,
    Property,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtNetwork import QNetworkAccessManager

from danbooru_multi_search.danbooru.client import (
    DEFAULT_TIMEOUT_MS,
    QtPostsRequestAttempt,
    RequestErrorKind,
    RequestState,
    build_posts_request,
    select_preferred_tls_backend,
)
from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.tasks import (
    BoundedTaskQueue,
    QtScheduler,
    SystemClock,
    TaskAttemptResult,
    TaskFinalResult,
    TaskKind,
    TaskQueueConfig,
    TaskQueueFullError,
    TaskQueueShutdownError,
    TaskRecord,
    TaskState,
)


DEFAULT_GALLERY_PAGE_SIZE = 40
MAX_GALLERY_PAGE_SIZE = 200
_COMPLEX_OPERATORS = frozenset({"and", "or", "not"})
_BASE_METATAG_NAMES = frozenset(
    {
        "age",
        "ai",
        "appealer",
        "approver",
        "artcomm",
        "child",
        "comm",
        "comment",
        "commentary",
        "commentaryupdater",
        "commenter",
        "date",
        "disapproved",
        "downvote",
        "downvotes",
        "duration",
        "embedded",
        "exif",
        "fav",
        "favcount",
        "favgroup",
        "filetype",
        "filesize",
        "flagger",
        "height",
        "has",
        "id",
        "is",
        "limit",
        "md5",
        "mpixels",
        "note",
        "noter",
        "noteupdater",
        "order",
        "ordfav",
        "ordfavgroup",
        "ordpool",
        "ordvote",
        "parent",
        "pixelhash",
        "pixiv_id",
        "pixiv",
        "pool",
        "random",
        "rating",
        "reacted",
        "ratio",
        "score",
        "search",
        "source",
        "status",
        "tagcount",
        "unaliased",
        "upvote",
        "upvotes",
        "user",
        "width",
    }
)
_COUNT_METATAG_NAMES = frozenset(
    {
        "comment_count",
        "deleted_comment_count",
        "active_comment_count",
        "note_count",
        "deleted_note_count",
        "active_note_count",
        "flag_count",
        "child_count",
        "deleted_child_count",
        "active_child_count",
        "pool_count",
        "deleted_pool_count",
        "active_pool_count",
        "series_pool_count",
        "collection_pool_count",
        "appeal_count",
        "approval_count",
        "replacement_count",
        "disapproval_count",
    }
)
_COUNT_METATAG_SYNONYMS = frozenset(
    {
        "comments",
        "deleted_comments",
        "active_comments",
        "notes",
        "deleted_notes",
        "active_notes",
        "flags",
        "children",
        "deleted_children",
        "active_children",
        "pools",
        "deleted_pools",
        "active_pools",
        "series_pools",
        "collection_pools",
        "appeals",
        "approvals",
        "replacements",
        "disapprovals",
    }
)
_CATEGORY_COUNT_METATAG_NAMES = frozenset(
    {"gentags", "arttags", "copytags", "chartags", "metatags"}
)
# Mirrors PostQueryBuilder::METATAGS. Rails-derived groups are expanded here
# so the desktop client does not need ActiveSupport inflection rules.
_METATAG_NAMES = (
    _BASE_METATAG_NAMES
    | _COUNT_METATAG_NAMES
    | _COUNT_METATAG_SYNONYMS
    | _CATEGORY_COUNT_METATAG_NAMES
)


class ContentRating(StrEnum):
    ALL = "all"
    GENERAL = "general"
    SENSITIVE = "sensitive"
    QUESTIONABLE = "questionable"
    EXPLICIT = "explicit"


_RATING_ALIASES = {
    "": ContentRating.ALL,
    "all": ContentRating.ALL,
    "g": ContentRating.GENERAL,
    "general": ContentRating.GENERAL,
    "s": ContentRating.SENSITIVE,
    "sensitive": ContentRating.SENSITIVE,
    "q": ContentRating.QUESTIONABLE,
    "questionable": ContentRating.QUESTIONABLE,
    "e": ContentRating.EXPLICIT,
    "explicit": ContentRating.EXPLICIT,
}
_RATING_METATAGS = {
    ContentRating.GENERAL: "rating:g",
    ContentRating.SENSITIVE: "rating:s",
    ContentRating.QUESTIONABLE: "rating:q",
    ContentRating.EXPLICIT: "rating:e",
}
_RATING_LABELS = {
    "g": "General",
    "s": "Sensitive",
    "q": "Questionable",
    "e": "Explicit",
}


class GalleryQueryError(ValueError):
    """Raised before networking when an M1 gallery query is unsupported."""


@dataclass(frozen=True, slots=True)
class GalleryRequestSpec:
    tags: tuple[str, ...]
    rating: ContentRating
    page: int
    limit: int

    @property
    def charged_search_term_count(self) -> int:
        """Count terms using the current rule where ``rating`` is free."""

        return len(self.tags)

    @property
    def api_tags(self) -> str:
        parts = list(self.tags)
        rating_metatag = _RATING_METATAGS.get(self.rating)
        if rating_metatag is not None:
            parts.append(rating_metatag)
        return " ".join(parts)


def build_gallery_request_spec(
    raw_tags: str,
    rating: str = ContentRating.ALL.value,
    *,
    page: int = 1,
    limit: int = DEFAULT_GALLERY_PAGE_SIZE,
) -> GalleryRequestSpec:
    """Validate the one-or-two-tag M1 query supported by the gallery."""

    if not isinstance(raw_tags, str):
        raise GalleryQueryError("标签查询必须是文本。")
    if not raw_tags.isascii():
        raise GalleryQueryError("当前阶段只支持 ASCII 英文标签。")
    stripped = raw_tags.strip()
    if not stripped:
        raise GalleryQueryError("请输入一个或两个 Danbooru 英文标签。")
    tokens = stripped.split()
    if any(token.casefold() in _COMPLEX_OPERATORS for token in tokens):
        raise GalleryQueryError("当前阶段不支持 AND、OR 或 NOT 复杂查询。")
    if len(tokens) > 2:
        raise GalleryQueryError("当前阶段最多支持两个普通标签。")

    normalized_tags: list[str] = []
    for token in tokens:
        normalized = token.casefold()
        if token.startswith(("-", "~")):
            raise GalleryQueryError(
                "当前阶段不支持排除标签或 OR 前缀。"
            )
        if "*" in token:
            raise GalleryQueryError("当前阶段不支持通配符查询。")
        if any(ord(character) < 33 or ord(character) > 126 for character in token):
            raise GalleryQueryError("标签包含不支持的控制字符。")
        if not _has_valid_tag_parentheses(token):
            raise GalleryQueryError("当前阶段不支持括号查询表达式。")
        metatag_name, separator, _value = normalized.partition(":")
        if separator and metatag_name in _METATAG_NAMES:
            raise GalleryQueryError(f"当前阶段不支持元标签 {metatag_name}:。")
        normalized_tags.append(normalized)

    if not isinstance(rating, str):
        raise GalleryQueryError("内容分级必须是文本。")
    normalized_rating = _RATING_ALIASES.get(rating.strip().casefold())
    if normalized_rating is None:
        raise GalleryQueryError("内容分级必须是全部、General、Sensitive、Questionable 或 Explicit。")
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise GalleryQueryError("页码必须是大于零的整数。")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_GALLERY_PAGE_SIZE
    ):
        raise GalleryQueryError(
            f"每页数量必须在 1 到 {MAX_GALLERY_PAGE_SIZE} 之间。"
        )

    return GalleryRequestSpec(
        tags=tuple(normalized_tags),
        rating=normalized_rating,
        page=page,
        limit=limit,
    )


def _has_valid_tag_parentheses(token: str) -> bool:
    if token.startswith("("):
        return False
    depth = 0
    content_at_depth = 0
    for character in token:
        if character == "(":
            depth += 1
            if depth > 1:
                return False
            content_at_depth = 0
        elif character == ")":
            if depth != 1 or content_at_depth == 0:
                return False
            depth = 0
        elif depth == 1:
            content_at_depth += 1
    return depth == 0


class PostRole(IntEnum):
    POST_ID = int(Qt.ItemDataRole.UserRole) + 1
    PREVIEW_URL = POST_ID + 1
    SCORE_TEXT = POST_ID + 2
    FAVORITE_TEXT = POST_ID + 3
    RATING_CODE = POST_ID + 4
    RATING_TEXT = POST_ID + 5
    WIDTH = POST_ID + 6
    HEIGHT = POST_ID + 7
    DIMENSIONS_TEXT = POST_ID + 8
    FILE_EXTENSION = POST_ID + 9
    MEDIA_KIND = POST_ID + 10
    MEDIA_LABEL = POST_ID + 11


class DanbooruPostListModel(QAbstractListModel):
    """Expose shared ``DanbooruPost`` objects through stable QML roles."""

    countChanged = Signal()

    _ROLE_NAMES = {
        PostRole.POST_ID: QByteArray(b"postId"),
        PostRole.PREVIEW_URL: QByteArray(b"previewUrl"),
        PostRole.SCORE_TEXT: QByteArray(b"scoreText"),
        PostRole.FAVORITE_TEXT: QByteArray(b"favoriteText"),
        PostRole.RATING_CODE: QByteArray(b"ratingCode"),
        PostRole.RATING_TEXT: QByteArray(b"ratingText"),
        PostRole.WIDTH: QByteArray(b"imageWidth"),
        PostRole.HEIGHT: QByteArray(b"imageHeight"),
        PostRole.DIMENSIONS_TEXT: QByteArray(b"dimensionsText"),
        PostRole.FILE_EXTENSION: QByteArray(b"fileExtension"),
        PostRole.MEDIA_KIND: QByteArray(b"mediaKind"),
        PostRole.MEDIA_LABEL: QByteArray(b"mediaLabel"),
    }

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._posts: tuple[DanbooruPost, ...] = ()

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._posts)

    @property
    def posts(self) -> tuple[DanbooruPost, ...]:
        return self._posts

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._posts)

    def roleNames(self) -> dict[int, QByteArray]:
        return {int(role): name for role, name in self._ROLE_NAMES.items()}

    def data(self, index: QModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)):
        if not index.isValid() or not 0 <= index.row() < len(self._posts):
            return None
        post = self._posts[index.row()]
        try:
            post_role = PostRole(role)
        except ValueError:
            return None

        values = {
            PostRole.POST_ID: post.id,
            PostRole.PREVIEW_URL: post.display_url,
            PostRole.SCORE_TEXT: (
                str(post.score) if post.score is not None else "暂无数据"
            ),
            PostRole.FAVORITE_TEXT: (
                str(post.fav_count) if post.fav_count is not None else "暂无数据"
            ),
            PostRole.RATING_CODE: post.rating,
            PostRole.RATING_TEXT: _RATING_LABELS[post.rating],
            PostRole.WIDTH: post.width,
            PostRole.HEIGHT: post.height,
            PostRole.DIMENSIONS_TEXT: f"{post.width} × {post.height}",
            PostRole.FILE_EXTENSION: post.file_ext.upper(),
            PostRole.MEDIA_KIND: post.media_kind,
            PostRole.MEDIA_LABEL: {
                "video": "视频",
                "animation": "动画",
            }.get(post.media_kind, ""),
        }
        return values[post_role]

    def set_posts(self, posts: tuple[DanbooruPost, ...]) -> None:
        self.beginResetModel()
        self._posts = posts
        self.endResetModel()
        self.countChanged.emit()

    def clear(self) -> None:
        if self._posts:
            self.set_posts(())


@dataclass(frozen=True, slots=True)
class GalleryPageOutcome:
    state: str
    message: str
    page: int
    posts: tuple[DanbooruPost, ...] = ()
    error_kind: str | None = None
    http_status: int | None = None


class DanbooruGalleryController(QObject):
    """Manage validated paged requests without allowing stale result updates."""

    stateChanged = Signal()
    statusTextChanged = Signal()
    errorTextChanged = Signal()
    navigationChanged = Signal()
    pageRequestCompleted = Signal(object)

    def __init__(
        self,
        base_url: str,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        network_manager: QNetworkAccessManager | None = None,
        task_queue: BoundedTaskQueue | None = None,
        queue_config: TaskQueueConfig | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        select_preferred_tls_backend()
        self._base_url = base_url
        self._timeout_ms = timeout_ms
        self._network_manager = network_manager or QNetworkAccessManager(self)
        self._owns_queue = task_queue is None
        if task_queue is None:
            scheduler = QtScheduler(self)
            task_queue = BoundedTaskQueue(
                queue_config or TaskQueueConfig(),
                clock=SystemClock(),
                scheduler=scheduler,
            )
        self._task_queue = task_queue
        self._posts_model = DanbooruPostListModel(self)
        self._active_task_id: str | None = None
        self._request_generation = 0
        self._shutdown = False
        self._state = RequestState.IDLE
        self._status_text = "请输入一个或两个 Danbooru 英文标签。"
        self._error_text = ""
        self._committed_spec: GalleryRequestSpec | None = None
        self._has_next_page = False
        self._failed_spec: GalleryRequestSpec | None = None

        application = QCoreApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self.shutdown)

    @Property(QObject, constant=True)
    def postsModel(self) -> QObject:
        return self._posts_model

    @Property(str, notify=stateChanged)
    def state(self) -> str:
        return self._state

    @Property(bool, notify=stateChanged)
    def busy(self) -> bool:
        return not self._requests_disabled and self._state in {
            RequestState.WAITING,
            RequestState.LOADING,
            RequestState.RETRY_WAIT,
        }

    @Property(str, notify=statusTextChanged)
    def statusText(self) -> str:
        return self._status_text

    @Property(str, notify=errorTextChanged)
    def errorText(self) -> str:
        return self._error_text

    @Property(int, notify=navigationChanged)
    def currentPage(self) -> int:
        return self._committed_spec.page if self._committed_spec is not None else 1

    @Property(int, notify=navigationChanged)
    def pageSize(self) -> int:
        return (
            self._committed_spec.limit
            if self._committed_spec is not None
            else DEFAULT_GALLERY_PAGE_SIZE
        )

    @Property(bool, notify=navigationChanged)
    def canPrevious(self) -> bool:
        return (
            self._committed_spec is not None
            and not self._requests_disabled
            and not self.busy
            and self._committed_spec.page > 1
        )

    @Property(bool, notify=navigationChanged)
    def canNext(self) -> bool:
        return (
            self._committed_spec is not None
            and not self._requests_disabled
            and not self.busy
            and self._has_next_page
        )

    @Property(bool, notify=navigationChanged)
    def canRefresh(self) -> bool:
        return (
            self._committed_spec is not None
            and not self._requests_disabled
            and not self.busy
        )

    @Property(bool, notify=navigationChanged)
    def canRetry(self) -> bool:
        return (
            self._failed_spec is not None
            and not self._requests_disabled
            and not self.busy
        )

    @Slot(str)
    @Slot(str, str, int)
    def search(
        self,
        raw_tags: str,
        rating: str = ContentRating.ALL.value,
        page_size: int = DEFAULT_GALLERY_PAGE_SIZE,
    ) -> None:
        if self._requests_disabled:
            self._finish_immediate_error(
                "应用或任务队列正在关闭，不能开始新请求。",
                RequestErrorKind.INVALID_REQUEST,
            )
            return
        try:
            spec = build_gallery_request_spec(
                raw_tags,
                rating,
                page=1,
                limit=page_size,
            )
        except GalleryQueryError as exc:
            self._invalidate_active_request()
            self._failed_spec = None
            self._finish_immediate_error(
                str(exc),
                RequestErrorKind.INVALID_REQUEST,
            )
            return

        self._failed_spec = None
        self._request_page(spec)

    @Slot()
    def previousPage(self) -> None:
        if self.canPrevious:
            self._request_page(self._spec_for_page(self.currentPage - 1))

    @Slot()
    def nextPage(self) -> None:
        if self.canNext:
            self._request_page(self._spec_for_page(self.currentPage + 1))

    @Slot()
    def refresh(self) -> None:
        if self.canRefresh and self._committed_spec is not None:
            self._request_page(self._committed_spec)

    @Slot()
    def retry(self) -> None:
        if self.canRetry and self._failed_spec is not None:
            self._request_page(self._failed_spec)

    @Slot()
    def cancelCurrentRequest(self) -> None:
        task_id = self._active_task_id
        if task_id is not None:
            self._task_queue.cancel(task_id, "用户取消了请求。")

    @Slot()
    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self._request_generation += 1
        task_id = self._active_task_id
        self._active_task_id = None
        if self._owns_queue:
            self._task_queue.shutdown()
        elif task_id is not None:
            self._task_queue.cancel(task_id, "Application is shutting down.")
        self._failed_spec = None
        self._state = RequestState.CANCELLED
        self._status_text = "应用正在关闭，活动请求已取消。"
        self._error_text = ""
        self._emit_view_changes()

    def _spec_for_page(self, page: int) -> GalleryRequestSpec:
        if self._committed_spec is None:
            raise RuntimeError("The gallery does not have a committed query.")
        return GalleryRequestSpec(
            tags=self._committed_spec.tags,
            rating=self._committed_spec.rating,
            page=page,
            limit=self._committed_spec.limit,
        )

    def _invalidate_active_request(self) -> int:
        self._request_generation += 1
        task_id = self._active_task_id
        self._active_task_id = None
        if task_id is not None:
            self._task_queue.cancel(task_id, "A newer gallery request replaced this one.")
        return self._request_generation

    def _request_page(self, spec: GalleryRequestSpec) -> None:
        if self._requests_disabled:
            self._finish_immediate_error(
                "应用或任务队列正在关闭，不能开始新请求。",
                RequestErrorKind.INVALID_REQUEST,
                page=spec.page,
            )
            return
        generation = self._invalidate_active_request()
        try:
            request = build_posts_request(
                self._base_url,
                spec.api_tags,
                page=spec.page,
                limit=spec.limit,
                timeout_ms=self._timeout_ms,
            )
        except ValueError as exc:
            self._failed_spec = None
            self._finish_immediate_error(
                str(exc),
                RequestErrorKind.INVALID_REQUEST,
                page=spec.page,
            )
            return

        self._failed_spec = None
        self._state = RequestState.WAITING
        self._status_text = f"第 {spec.page} 页请求正在等待执行。"
        self._error_text = ""
        self._emit_view_changes()
        if generation != self._request_generation or self._requests_disabled:
            return

        def starter(
            attempt_number: int,
            completion: Callable[[TaskAttemptResult], None],
        ) -> QtPostsRequestAttempt:
            return QtPostsRequestAttempt(
                self._network_manager,
                request,
                completion,
                self,
            )

        def state_changed(record: TaskRecord) -> None:
            self._task_state_changed(generation, spec, record)

        def completed(result: TaskFinalResult) -> None:
            self._task_completed(generation, spec, result)

        try:
            record = self._task_queue.submit(
                TaskKind.METADATA,
                starter,
                completed,
                state_listener=state_changed,
            )
        except (TaskQueueFullError, TaskQueueShutdownError) as exc:
            if generation == self._request_generation and not self._shutdown:
                self._failed_spec = None
                self._finish_immediate_error(
                    str(exc),
                    RequestErrorKind.INVALID_REQUEST,
                    page=spec.page,
                )
            return
        if generation != self._request_generation or self._requests_disabled:
            self._task_queue.cancel(
                record.identifier,
                "A newer gallery request replaced this queued task.",
            )
            return
        if not record.terminal and self.busy:
            self._active_task_id = record.identifier

    def _task_state_changed(
        self,
        generation: int,
        spec: GalleryRequestSpec,
        record: TaskRecord,
    ) -> None:
        if generation != self._request_generation or record.terminal:
            return
        if record.state == TaskState.WAITING:
            self._state = RequestState.WAITING
            self._status_text = f"第 {spec.page} 页请求正在等待执行。"
        elif record.state == TaskState.RUNNING:
            self._state = RequestState.LOADING
            self._status_text = (
                f"正在加载第 {spec.page} 页（第 {record.attempts} 次请求）……"
            )
        elif record.state == TaskState.RETRY_WAIT:
            self._state = RequestState.RETRY_WAIT
            if record.message and "429" in record.message:
                self._status_text = "访问频率受到限制，正在等待重试。"
            elif record.message and "503" in record.message:
                self._status_text = "Danbooru 服务暂时不可用，正在等待重试。"
            else:
                self._status_text = "请求暂时失败，正在等待重试。"
        self._error_text = ""
        self._emit_view_changes()

    def _task_completed(
        self,
        generation: int,
        spec: GalleryRequestSpec,
        result: TaskFinalResult,
    ) -> None:
        if generation != self._request_generation:
            return
        if self._active_task_id is not None and result.task_id != self._active_task_id:
            return
        self._active_task_id = None

        if result.state == TaskState.CANCELLED:
            self._failed_spec = None
            self._complete(
                GalleryPageOutcome(
                    state=RequestState.CANCELLED,
                    message="请求已取消，当前画廊内容保持不变。",
                    page=spec.page,
                    error_kind=RequestErrorKind.CANCELLED,
                )
            )
            return

        if result.state == TaskState.FAILED:
            self._failed_spec = spec
            message = self._failure_message(result)
            self._complete(
                GalleryPageOutcome(
                    state=RequestState.ERROR,
                    message=message,
                    page=spec.page,
                    error_kind=result.error_code,
                    http_status=result.http_status,
                )
            )
            return

        posts = result.value
        if not isinstance(posts, tuple) or not all(
            isinstance(post, DanbooruPost) for post in posts
        ):
            self._failed_spec = spec
            self._complete(
                GalleryPageOutcome(
                    state=RequestState.ERROR,
                    message="Danbooru 返回了无法识别的作品列表。",
                    page=spec.page,
                    error_kind=RequestErrorKind.INVALID_RESPONSE,
                    http_status=result.http_status,
                )
            )
            return

        self._failed_spec = None
        if posts:
            self._committed_spec = spec
            self._has_next_page = len(posts) == spec.limit
            self._posts_model.set_posts(posts)
            if generation != self._request_generation or self._requests_disabled:
                return
            self._complete(
                GalleryPageOutcome(
                    state=RequestState.SUCCESS,
                    message=f"第 {spec.page} 页已加载 {len(posts)} 项作品。",
                    page=spec.page,
                    posts=posts,
                    http_status=result.http_status,
                ),
                generation=generation,
            )
        elif self._is_empty_next_page(spec):
            self._has_next_page = False
            self._complete(
                GalleryPageOutcome(
                    state=RequestState.EMPTY,
                    message=(
                        f"第 {spec.page} 页没有作品，已保留第 {self.currentPage} 页。"
                    ),
                    page=spec.page,
                    posts=(),
                    http_status=result.http_status,
                ),
                generation=generation,
            )
        else:
            self._committed_spec = spec
            self._has_next_page = False
            self._posts_model.set_posts(())
            if generation != self._request_generation or self._requests_disabled:
                return
            self._complete(
                GalleryPageOutcome(
                    state=RequestState.EMPTY,
                    message=f"第 {spec.page} 页没有符合条件的作品。",
                    page=spec.page,
                    posts=(),
                    http_status=result.http_status,
                ),
                generation=generation,
            )

    def _is_empty_next_page(self, spec: GalleryRequestSpec) -> bool:
        committed = self._committed_spec
        return (
            committed is not None
            and committed.tags == spec.tags
            and committed.rating == spec.rating
            and committed.limit == spec.limit
            and spec.page > committed.page
        )

    @staticmethod
    def _failure_message(result: TaskFinalResult) -> str:
        if (
            result.error_code == RequestErrorKind.HTTP
            and result.http_status == 429
        ):
            return "访问频率受到限制（HTTP 429），请稍后重试。"
        if (
            result.error_code == RequestErrorKind.HTTP
            and result.http_status is not None
        ):
            return f"Danbooru 返回 HTTP {result.http_status}，请重试。"
        if result.error_code == RequestErrorKind.TIMEOUT:
            return "Danbooru 请求超时，请重试。"
        if result.error_code == RequestErrorKind.NETWORK:
            return "Danbooru 网络连接失败，请检查网络后重试。"
        if result.error_code == RequestErrorKind.INVALID_RESPONSE:
            return result.error or "Danbooru 返回了无效数据。"
        return result.error or "Danbooru 请求失败，请重试。"

    @property
    def _requests_disabled(self) -> bool:
        return self._shutdown or self._task_queue.is_shutdown

    def _finish_immediate_error(
        self,
        message: str,
        error_kind: str,
        *,
        page: int = 1,
    ) -> None:
        self._complete(
            GalleryPageOutcome(
                state=RequestState.ERROR,
                message=message,
                page=page,
                error_kind=error_kind,
            ),
            generation=self._request_generation,
        )

    def _complete(
        self,
        outcome: GalleryPageOutcome,
        *,
        generation: int | None = None,
    ) -> None:
        expected_generation = (
            self._request_generation if generation is None else generation
        )
        if expected_generation != self._request_generation:
            return
        self._state = outcome.state
        self._status_text = outcome.message
        self._error_text = (
            outcome.message if outcome.state == RequestState.ERROR else ""
        )
        self._emit_view_changes()
        if (
            expected_generation == self._request_generation
            and not self._requests_disabled
        ):
            self.pageRequestCompleted.emit(outcome)

    def _emit_view_changes(self) -> None:
        self.stateChanged.emit()
        self.statusTextChanged.emit()
        self.errorTextChanged.emit()
        self.navigationChanged.emit()
