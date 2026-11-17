"""Virtual original-page model with incrementally updated, bounded metadata."""

from collections import OrderedDict

from PySide6.QtCore import QAbstractListModel, QByteArray, QModelIndex, Property, Qt, Signal

from danbooru_multi_search.core.pool_pages import PoolPageSlot, PoolPageState, PoolPostMetadata
from danbooru_multi_search.core.pool_reader import READER_CACHE_PAGES
from danbooru_multi_search.core.pools import DanbooruPool


class PoolReaderListModel(QAbstractListModel):
    changed = Signal()
    _NAMES = ("pageNumber", "pageKey", "pageState", "pageMessage", "postKey", "previewUrl", "aspectRatio")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.pool: DanbooruPool | None = None
        self.cache: OrderedDict[int, PoolPageSlot] = OrderedDict()
        self._revision = 0

    @Property(int, notify=changed)
    def revision(self) -> int:
        return self._revision

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() or self.pool is None else self.pool.post_count

    def roleNames(self):
        return {int(Qt.UserRole) + index + 1: QByteArray(name.encode())
                for index, name in enumerate(self._NAMES)}

    def data(self, index, role=int(Qt.DisplayRole)):
        number = role - int(Qt.UserRole) - 1
        if not index.isValid() or not 0 <= number < len(self._NAMES):
            return None
        return self.mapping(index.row() + 1).get(self._NAMES[number])

    def set_pool(self, pool: DanbooruPool | None) -> None:
        self.beginResetModel()
        self.pool = pool
        self.cache.clear()
        self._revision += 1
        self.endResetModel()
        self.changed.emit()

    def slot(self, page: int) -> PoolPageSlot | None:
        if self.pool is None or type(page) is not int or not 1 <= page <= self.pool.post_count:
            return None
        ref = self.pool.page_at(page)
        return self.cache.get(page) or (PoolPageSlot(ref, PoolPostMetadata(
            ref.post_id, PoolPageState.WAITING, message="尚未读取这一页。",
        )) if ref else None)

    def mapping(self, page: int) -> dict:
        slot = self.slot(page)
        if slot is None:
            return {}
        post = slot.metadata.post
        return {"pageNumber": page, "pageKey": slot.key,
                "pageState": slot.metadata.state.value, "pageMessage": slot.metadata.message,
                "postKey": str(slot.reference.post_id), "previewUrl": post.display_url if post else "",
                "aspectRatio": post.height / post.width if post else 1.4}

    def update(self, slots: tuple[PoolPageSlot, ...], protected: set[int]) -> None:
        """Emit row changes without resetting the list or moving its scroll anchor."""
        changed = set()
        for slot in slots:
            number = slot.reference.page_number
            if self.pool is None or slot.reference != self.pool.page_at(number):
                continue
            self.cache[number] = slot
            self.cache.move_to_end(number)
            changed.add(number)
        while len(self.cache) > READER_CACHE_PAGES:
            victim = next((number for number in self.cache if number not in protected), None)
            if victim is None:
                break
            del self.cache[victim]
            changed.add(victim)
        self._revision += 1
        for number in sorted(changed):
            self.dataChanged.emit(self.index(number - 1), self.index(number - 1))
        self.changed.emit()
