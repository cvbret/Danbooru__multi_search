"""Qt row projection for immutable saved favorites."""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import QAbstractListModel, QByteArray, QModelIndex, Property, Qt, Signal

from danbooru_multi_search.core.favorites import FavoriteItem
from danbooru_multi_search.ui.post_labels import RATING_LABELS


class FavoriteListModel(QAbstractListModel):
    changed = Signal()
    _NAMES = (
        "favoriteKey", "objectType", "objectKey", "displayName", "typeText",
        "postKey", "previewUrl", "scoreText", "favoriteText", "ratingCode",
        "ratingText", "dimensionsText", "fileExtension", "mediaLabel", "timeText",
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: tuple[FavoriteItem, ...] = ()
        self._revision = 0

    @Property(int, notify=changed)
    def count(self) -> int:
        return len(self.items)

    @Property(int, notify=changed)
    def revision(self) -> int:
        return self._revision

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def roleNames(self):
        return {int(Qt.UserRole) + index + 1: QByteArray(name.encode()) for index, name in enumerate(self._NAMES)}

    def data(self, index, role=int(Qt.DisplayRole)):
        number = role - int(Qt.UserRole) - 1
        if not index.isValid() or not 0 <= index.row() < len(self.items) or not 0 <= number < len(self._NAMES):
            return None
        return self.mapping(index.row()).get(self._NAMES[number])

    def mapping(self, row: int) -> dict:
        if not 0 <= row < len(self.items):
            return {}
        item = self.items[row]
        post = item.post
        kind = item.tag_category.value if item.tag_category else item.identity.object_type.value
        return {
            "favoriteKey": str(item.id), "objectType": item.identity.object_type.value,
            "objectKey": item.identity.object_key, "displayName": item.display_name,
            "typeText": {"post": "图片", "artist": "画师", "character": "角色", "copyright": "作品系列", "general": "普通标签", "pool": "系列作品池"}.get(kind, kind),
            "postKey": str(post.id) if post else "", "previewUrl": post.display_url if post else "",
            "scoreText": str(post.score) if post and post.score is not None else "暂无数据",
            "favoriteText": str(post.fav_count) if post and post.fav_count is not None else "暂无数据",
            "ratingCode": post.rating if post else "", "ratingText": RATING_LABELS.get(post.rating, "") if post else "",
            "dimensionsText": f"{post.width} × {post.height}" if post else "",
            "fileExtension": post.file_ext.upper() if post else "",
            "mediaLabel": post.media_kind if post and post.media_kind != "image" else "",
            "timeText": _favorite_time_text(item.favorited_at),
        }

    def replace(self, items: tuple[FavoriteItem, ...], *, append=False) -> None:
        if append and items:
            start = len(self.items)
            self.beginInsertRows(QModelIndex(), start, start + len(items) - 1)
            self.items += items
            self.endInsertRows()
        elif not append:
            self.beginResetModel()
            self.items = items
            self.endResetModel()
        self._revision += 1
        self.changed.emit()

    def by_key(self, key: str) -> FavoriteItem | None:
        return next((item for item in self.items if str(item.id) == key), None)


def _favorite_time_text(value: datetime) -> str:
    try:
        return value.astimezone().strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        # Windows local-time conversion has a narrower range than stored UTC.
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") + " UTC"
