"""Shared QML-facing Danbooru post model."""

from __future__ import annotations

from enum import IntEnum

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    Property,
    Qt,
    Signal,
    Slot,
)

from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.ui.post_labels import RATING_LABELS
from danbooru_multi_search.danbooru.result_processing import ProcessedPost


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
    revisionChanged = Signal()
    viewReordering = Signal()

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
        self._processed_posts: tuple[ProcessedPost, ...] = ()
        self._display_operation_id: int | None = None
        self._revision = 0

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._posts)

    @Property(int, notify=revisionChanged)
    def revision(self) -> int:
        return self._revision

    @property
    def posts(self) -> tuple[DanbooruPost, ...]:
        return self._posts

    @property
    def processed_posts(self) -> tuple[ProcessedPost, ...]:
        return self._processed_posts

    @property
    def display_operation_id(self) -> int | None:
        return self._display_operation_id

    @Property(int, notify=revisionChanged)
    def displayOperationId(self) -> int:
        return self._display_operation_id or 0

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
            PostRole.RATING_TEXT: RATING_LABELS[post.rating],
            PostRole.WIDTH: post.width,
            PostRole.HEIGHT: post.height,
            PostRole.DIMENSIONS_TEXT: f"{post.width} × {post.height}",
            PostRole.FILE_EXTENSION: post.file_ext.upper(),
            PostRole.MEDIA_KIND: post.media_kind,
            PostRole.MEDIA_LABEL: {
                "video": "视频",
                "gif": "GIF",
                "ugoira": "Ugoira",
                "flash": "Flash",
                "unknown": "未知格式",
            }.get(post.media_kind, ""),
        }
        return values[post_role]

    def set_posts(self, posts: tuple[DanbooruPost, ...]) -> None:
        """Replace all posts and reset the model, including its view identity."""

        processed = tuple(ProcessedPost(post, None) for post in posts)
        self._replace_posts(processed, display_operation_id=None)

    def set_processed_posts(
        self,
        display_operation_id: int,
        items: tuple[ProcessedPost, ...],
    ) -> bool:
        """Commit one M2 view, skipping only an identical same-operation view."""

        if (
            not isinstance(display_operation_id, int)
            or isinstance(display_operation_id, bool)
            or display_operation_id < 1
        ):
            raise ValueError("display_operation_id must be a positive integer")
        if not isinstance(items, tuple) or not all(
            isinstance(item, ProcessedPost) for item in items
        ):
            raise TypeError("items must be a tuple of ProcessedPost values")
        old_count = len(self._processed_posts)
        if (
            self._display_operation_id == display_operation_id and old_count <= len(items)
            and all(
                old.post is new.post
                and old.session_rank == new.session_rank
                for old, new in zip(self._processed_posts, items)
            )
        ):
            if old_count == len(items):
                return False
            self.beginInsertRows(QModelIndex(), old_count, len(items) - 1)
            self._processed_posts = items
            self._posts += tuple(item.post for item in items[old_count:])
            self._revision += 1
            self.endInsertRows()
            self.countChanged.emit()
            self.revisionChanged.emit()
            return True
        if self._display_operation_id == display_operation_id:
            revision = self._revision
            self.viewReordering.emit()
            if self._revision != revision:
                return False
        self._replace_posts(items, display_operation_id=display_operation_id)
        return True

    def _replace_posts(
        self,
        items: tuple[ProcessedPost, ...],
        *,
        display_operation_id: int | None,
    ) -> None:
        self.beginResetModel()
        self._processed_posts = items
        self._posts = tuple(item.post for item in items)
        self._display_operation_id = display_operation_id
        self._revision += 1
        self.endResetModel()
        self.countChanged.emit()
        self.revisionChanged.emit()

    def post_at(self, row: int) -> DanbooruPost | None:
        if not isinstance(row, int) or isinstance(row, bool):
            return None
        if not 0 <= row < len(self._posts):
            return None
        return self._posts[row]

    def row_for_post_id(self, post_id: int) -> int:
        if not isinstance(post_id, int) or isinstance(post_id, bool):
            return -1
        return next(
            (
                row
                for row, post in enumerate(self._posts)
                if post.id == post_id
            ),
            -1,
        )

    @Slot(int, result=int)
    def rowForPostId(self, post_id: int) -> int:
        return self.row_for_post_id(post_id)

    @Slot(str, result=int)
    def rowForPostKey(self, key: str) -> int:
        return self.row_for_post_id(int(key)) if key.isascii() and key.isdecimal() and len(key) <= 19 else -1

    @Slot(int, result=int)
    def postIdAt(self, row: int) -> int:
        post = self.post_at(row)
        return post.id if post is not None else -1

    def clear(self) -> None:
        if self._posts or self._display_operation_id is not None:
            self.set_posts(())
