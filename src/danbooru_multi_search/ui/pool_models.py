"""Qt projections of the current bounded pool window and membership page."""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QByteArray, QModelIndex, Property, Qt, Signal

from danbooru_multi_search.core.pool_pages import PoolPageSlot
from danbooru_multi_search.core.pools import DanbooruPool


class PoolPageListModel(QAbstractListModel):
    changed = Signal()
    _NAMES = (
        "pageKey", "postKey", "pageNumber", "state", "message", "previewUrl",
        "scoreText", "favoriteText", "ratingCode", "ratingText", "dimensionsText",
        "fileExtension", "mediaLabel",
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.slots: tuple[PoolPageSlot, ...] = ()
        self._revision = 0

    @Property(int, notify=changed)
    def revision(self) -> int:
        return self._revision

    @Property(int, notify=changed)
    def count(self) -> int:
        return len(self.slots)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.slots)

    def roleNames(self):
        return {int(Qt.UserRole) + index + 1: QByteArray(name.encode())
                for index, name in enumerate(self._NAMES)}

    def data(self, index, role=int(Qt.DisplayRole)):
        number = role - int(Qt.UserRole) - 1
        if not index.isValid() or not 0 <= number < len(self._NAMES):
            return None
        return self.mapping(index.row()).get(self._NAMES[number])

    def mapping(self, row: int) -> dict:
        if not 0 <= row < len(self.slots):
            return {}
        slot = self.slots[row]
        post = slot.metadata.post
        return {
            "pageKey": slot.key, "postKey": str(slot.reference.post_id),
            "pageNumber": slot.reference.page_number, "state": slot.metadata.state.value,
            "message": slot.metadata.message, "previewUrl": post.display_url if post else "",
            "scoreText": str(post.score) if post and post.score is not None else "暂无数据",
            "favoriteText": str(post.fav_count) if post and post.fav_count is not None else "暂无数据",
            "ratingCode": post.rating if post else "", "ratingText": post.rating.upper() if post else "",
            "dimensionsText": f"{post.width} × {post.height}" if post else "",
            "fileExtension": post.file_ext.upper() if post else "",
            "mediaLabel": post.media_kind if post and post.media_kind != "image" else "",
        }

    def replace(self, slots: tuple[PoolPageSlot, ...]) -> None:
        self.beginResetModel()
        self.slots = slots
        self._revision += 1
        self.endResetModel()
        self.changed.emit()

    def by_key(self, key: str) -> PoolPageSlot | None:
        return next((slot for slot in self.slots if slot.key == key), None)


def membership_mapping(pool: DanbooruPool) -> dict:
    return {
        "poolKey": str(pool.id), "name": pool.name, "description": pool.description,
        "category": pool.category.value,
        "categoryText": "系列作品池" if pool.category.value == "series" else "主题作品集合",
        "postCount": pool.post_count,
    }
