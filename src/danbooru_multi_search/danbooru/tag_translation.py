"""Session-wide read-only access to the current local tag translations."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from danbooru_multi_search.danbooru.tag_index import TagIndexSnapshot


class TagTranslationStore(QObject):
    """Publish atomic immutable index replacements to interested controllers."""

    snapshotChanged = Signal()

    def __init__(
        self,
        initial_snapshot: TagIndexSnapshot | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._snapshot = initial_snapshot or TagIndexSnapshot.empty()

    @property
    def snapshot(self) -> TagIndexSnapshot:
        return self._snapshot

    def chinese_names(self, tag_name: str) -> tuple[str, ...]:
        if not isinstance(tag_name, str) or not tag_name.isascii():
            return ()
        record = self._snapshot.records_by_name.get(tag_name.casefold())
        return record.chinese_names if record is not None else ()

    def replace_snapshot(self, snapshot: TagIndexSnapshot) -> None:
        if not isinstance(snapshot, TagIndexSnapshot):
            raise TypeError("snapshot must be a TagIndexSnapshot")
        self._snapshot = snapshot
        self.snapshotChanged.emit()
