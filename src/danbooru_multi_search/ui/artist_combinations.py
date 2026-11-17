"""Coordinate saved artist choices and draft-only query-tree operations."""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from PySide6.QtCore import QObject, Property, Signal, Slot

from danbooru_multi_search.app.write_access import SharedWriteAccess
from danbooru_multi_search.core.artist_combinations import (
    ArtistCombination, ArtistCombinationDraft, ArtistCombinationError, ArtistCombinationMode,
)
from danbooru_multi_search.core.favorites import (
    FavoriteError, FavoriteFilter, FavoriteItem, FavoriteObjectType, FavoriteTagCategory,
)
from danbooru_multi_search.storage.artist_combinations import ArtistCombinationRepository
from danbooru_multi_search.storage.favorites import FavoriteRepository
from danbooru_multi_search.ui.favorites import FavoriteController
from danbooru_multi_search.ui.query_state import M2QueryBridgeController


class ArtistCombinationController(QObject):
    """Keep saved combinations separate from the current favorite selection."""

    changed = Signal()
    draftApplied = Signal()

    def __init__(self, repository: ArtistCombinationRepository, favorites: FavoriteController,
                 query: M2QueryBridgeController, parent: QObject | None = None, *,
                 access: SharedWriteAccess, favorite_repository: FavoriteRepository) -> None:
        super().__init__(parent)
        self._repository = repository
        self._access = access
        self._favorite_repository = favorite_repository
        self._query = query
        self._artists: tuple[FavoriteItem, ...] = ()
        self._combinations: tuple[ArtistCombination, ...] = ()
        self._selected: tuple[str, ...] = ()
        self._generation = 0
        self._selection_revision = 0
        self._error = ""
        self._active = False
        self._closed = False
        self._pending: tuple[str, ArtistCombination, int] | None = None
        favorites.collectionChanged.connect(self._collection_changed)
        access.changed.connect(self._busy_changed)

    @Property(int, notify=changed)
    def generation(self) -> int:
        return self._generation

    @Property(int, notify=changed)
    def selectionRevision(self) -> int:
        return self._selection_revision

    @Property(int, notify=changed)
    def selectedCount(self) -> int:
        return len(self._selected)

    @Property(bool, notify=changed)
    def canWrite(self) -> bool:
        return self._active and not self._closed and not self._access.busy

    @Property(str, notify=changed)
    def errorText(self) -> str:
        return self._error

    @Property("QVariantList", notify=changed)
    def artists(self) -> list[dict[str, object]]:
        return [{"key": str(item.id), "name": item.identity.object_key,
                 "displayName": item.display_name, "selected": str(item.id) in self._selected}
                for item in self._artists]

    @Property("QVariantList", notify=changed)
    def combinations(self) -> list[dict[str, object]]:
        return [{"key": str(item.combination_id), "name": item.name,
                 "artists": list(item.artists), "mode": item.mode.value,
                 "modeText": {"all": "全部包含", "any": "任意包含", "exclude": "排除"}[item.mode.value]}
                for item in self._combinations]

    @Property("QVariantMap", notify=changed)
    def deleteConfirmation(self) -> dict[str, str]:
        if self._pending is None:
            return {"token": "", "name": ""}
        return {"token": self._pending[0], "name": self._pending[1].name}

    @Slot(bool)
    def setActive(self, active: bool) -> None:
        if self._closed or self._active == active:
            return
        self._active = active
        self._invalidate()
        if active:
            self.reload()
        else:
            self.changed.emit()

    @Slot(result=bool)
    def reload(self) -> bool:
        if self._closed or not self._active:
            return False
        self._invalidate()
        generation = self._generation
        try:
            values: list[FavoriteItem] = []
            filters = FavoriteFilter(object_type=FavoriteObjectType.TAG, tag_category=FavoriteTagCategory.ARTIST)
            while True:
                page = self._favorite_repository.list_favorites(filters, limit=1000, offset=len(values))
                values.extend(page)
                if len(page) < 1000:
                    break
            combinations = self._repository.list_combinations()
        except (FavoriteError, ArtistCombinationError) as exc:
            return self._fail(exc)
        if not self._current(generation):
            return False
        self._artists = tuple(values)
        self._combinations = combinations
        self._error = ""
        self.changed.emit()
        return self._current(generation)

    @Slot(str, int, bool, result=bool)
    def toggleArtist(self, key: str, generation: int, selected: bool) -> bool:
        if not self._current(generation) or not any(str(item.id) == key for item in self._artists):
            return False
        if selected and key not in self._selected:
            self._selected += (key,)
        elif not selected and key in self._selected:
            self._selected = tuple(value for value in self._selected if value != key)
        else:
            return True
        self._selection_revision += 1
        self.changed.emit()
        return True

    @Slot()
    def clearSelection(self) -> None:
        if self._closed or not self._selected:
            return
        self._selected = ()
        self._selection_revision += 1
        self.changed.emit()

    @Slot(int, int, str, "QVariant", result=bool)
    def applySelection(self, generation: int, revision: int, mode: str, parent_path: object = None) -> bool:
        if not self._selection_current(generation, revision):
            return False
        try:
            artists = self._selected_artists()
            parsed_mode = ArtistCombinationMode(mode)
        except (FavoriteError, ArtistCombinationError, ValueError) as exc:
            return self._fail(exc)
        return self._apply(artists, parsed_mode, parent_path, generation)

    @Slot(int, int, str, str, result=bool)
    def saveSelection(self, generation: int, revision: int, name: str, mode: str) -> bool:
        return self._write(
            generation,
            lambda: self._repository.create_combination(
                ArtistCombinationDraft(name, self._selected_artists(), ArtistCombinationMode(mode))),
            guard=lambda: self._selection_current(generation, revision),
        )

    @Slot(str, int, "QVariant", result=bool)
    def applyCombination(self, key: str, generation: int, parent_path: object = None) -> bool:
        if not self._current(generation):
            return False
        try:
            current = self._combination(key)
            if current not in self._repository.list_combinations():
                raise ArtistCombinationError("stale_combination", "画师组合已改变，请刷新后重新选择。")
        except ArtistCombinationError as exc:
            return self._fail(exc)
        return self._apply(current.artists, current.mode, parent_path, generation)

    @Slot(str, int, str, result=bool)
    def renameCombination(self, key: str, generation: int, name: str) -> bool:
        return self._write(generation, lambda: self._repository.rename_combination(self._combination(key), name))

    @Slot(str, int, result=bool)
    def prepareDeleteCombination(self, key: str, generation: int) -> bool:
        if not self._current(generation):
            return False
        try:
            current = self._combination(key)
        except ArtistCombinationError as exc:
            return self._fail(exc)
        self._pending = (uuid4().hex, current, generation)
        self.changed.emit()
        return self._current(generation) and self._pending is not None

    @Slot(str, result=bool)
    def confirmDelete(self, token: str) -> bool:
        pending = self._pending
        if pending is None or pending[0] != token:
            return False
        return self._write(pending[2], lambda: self._repository.delete_combination(pending[1]),
                           guard=lambda: self._pending == pending)

    @Slot()
    def cancelDelete(self) -> None:
        if not self._closed and self._pending is not None:
            self._pending = None
            self.changed.emit()

    def _apply(self, artists: tuple[str, ...], mode: ArtistCombinationMode,
               parent_path: object, generation: int) -> bool:
        if not self._current(generation):
            return False
        if not self._query.insertArtistGroup(parent_path, [{"name": name, "category": 1} for name in artists], mode.value):
            return self._fail(ArtistCombinationError("query_edit_failed", self._query.errorText))
        if not self._current(generation):
            return False
        self._error = ""
        self.changed.emit()
        if not self._current(generation):
            return False
        self.draftApplied.emit()
        return True

    def _selected_artists(self) -> tuple[str, ...]:
        if not self._selected:
            raise ArtistCombinationError("empty_artists", "请先选择至少一个已收藏画师。")
        by_key = {str(item.id): item for item in self._artists}
        names = []
        for key in self._selected:
            source = by_key.get(key)
            current = self._favorite_repository.get_by_identity(source.identity) if source else None
            if (current is None or current.id != source.id
                    or current.identity.object_type is not FavoriteObjectType.TAG
                    or current.tag_category is not FavoriteTagCategory.ARTIST):
                raise ArtistCombinationError("stale_artist", "所选画师收藏已改变，请刷新后重新选择。")
            names.append(current.identity.object_key)
        return tuple(names)

    def _combination(self, key: str) -> ArtistCombination:
        for item in self._combinations:
            if str(item.combination_id) == key:
                return item
        raise ArtistCombinationError("stale_combination", "画师组合已改变，请刷新后重新选择。")

    def _write(self, generation: int, operation: Callable[[], object], *, guard: Callable[[], bool] = lambda: True) -> bool:
        if not self._current(generation) or not guard() or not self.canWrite:
            return False
        owner = uuid4().hex
        if not self._access.begin_external_write(owner):
            return False
        try:
            if not self._current(generation) or not guard():
                return False
            operation()
            if not self._current(generation):
                return False
            self.reload()
            return True
        except (FavoriteError, ArtistCombinationError, ValueError) as exc:
            return self._fail(exc)
        finally:
            self._access.finish_external_write(owner)

    def _invalidate(self) -> None:
        self._generation += 1
        self._selection_revision += 1
        self._selected = ()
        self._pending = None

    def _current(self, generation: int) -> bool:
        return not self._closed and self._active and self._generation == generation

    def _selection_current(self, generation: int, revision: int) -> bool:
        return self._current(generation) and self._selection_revision == revision

    def _fail(self, error: Exception) -> bool:
        if not self._closed:
            self._error = str(error)
            self.changed.emit()
        return False

    @Slot()
    def _collection_changed(self) -> None:
        if self._closed:
            return
        if self._active:
            self.reload()
        else:
            self._invalidate()

    @Slot()
    def _busy_changed(self) -> None:
        if not self._closed:
            self.changed.emit()

    def shutdown(self) -> None:
        self._closed = True
        self._active = False
        self._invalidate()
