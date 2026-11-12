"""Paged collection browsing, stable selection, confirmation, and detail return."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from uuid import uuid4

from PySide6.QtCore import QObject, Property, Signal, Slot

from danbooru_multi_search.core.favorites import (
    FavoriteError, FavoriteFilter, FavoriteFolderScope, FavoriteObjectType,
    FavoriteSort, FavoriteSortDirection, FavoriteSortField, FavoriteTagCategory,
    FolderDeleteMode,
)
from danbooru_multi_search.ui.favorite_models import FavoriteListModel
from danbooru_multi_search.ui.favorites import FavoriteController, post_identity
from danbooru_multi_search.ui.detail_navigation import DetailNavigationController


@dataclass(frozen=True, slots=True)
class _Pending:
    token: str
    generation: int
    operation: str
    snapshot: object
    mode: FolderDeleteMode | None
    text: str
    selection_revision: int | None


class FavoriteCollectionController(QObject):
    """Own collection-page state while the shared controller owns saved facts."""

    changed = Signal()
    confirmationChanged = Signal()
    PAGE_SIZE = 200

    def __init__(self, repository, favorites: FavoriteController,
                 navigation: DetailNavigationController, parent=None):
        super().__init__(parent)
        self._repository = repository
        self._favorites = favorites
        self._navigation = navigation
        self._model = FavoriteListModel(self)
        self._folders = ()
        self._selected: set[str] = set()
        self._generation = 0
        self._selection_revision = 0
        self._database_revision = 0
        self._total = 0
        self._activated = False
        self._closed = False
        self._error = ""
        self._pending: _Pending | None = None
        self._return_snapshot = None
        self._restore_application = None
        self._filters = FavoriteFilter()
        self._sorting = FavoriteSort()
        self._form = {"scope": "all", "folderKey": "", "kind": "all", "exactTag": "", "exactArtist": "", "rating": "all", "sortField": "favorited_at", "sortDirection": "descending"}
        favorites.collectionChanged.connect(self._collection_changed)
        favorites.stateChanged.connect(self.changed)
        favorites.busyChanged.connect(self.changed)
        favorites.noticeChanged.connect(self.changed)

    @Property(QObject, constant=True)
    def model(self):
        return self._model

    @Property(int, notify=changed)
    def generation(self):
        return self._generation

    @Property(int, notify=changed)
    def selectionRevision(self):
        return self._selection_revision

    @Property(int, notify=changed)
    def selectedCount(self):
        return len(self._selected)

    @Property(int, notify=changed)
    def loadedCount(self):
        return len(self._model.items)

    @Property(int, notify=changed)
    def totalCount(self):
        return self._total

    @Property(bool, notify=changed)
    def activated(self):
        return self._activated

    @Property(bool, notify=changed)
    def canLoadMore(self):
        return not self._closed and not self._error and self.loadedCount < self._total

    @Property(str, notify=changed)
    def errorText(self):
        return self._error or self._favorites.noticeText

    @Property("QVariantList", notify=changed)
    def folders(self):
        return [{"key": str(folder.id), "name": folder.name} for folder in self._folders]

    @Property("QVariantMap", notify=changed)
    def formValues(self):
        return dict(self._form)

    @Property("QVariantMap", notify=confirmationChanged)
    def confirmation(self):
        value = self._pending
        return {"token": value.token, "text": value.text} if value else {"token": "", "text": ""}

    @Slot(bool)
    def setActive(self, active: bool):
        if active and not self._closed and not self._activated:
            self._activated = True
            self.reload()

    def _invalidate(self):
        self._generation += 1
        self._selection_revision += 1
        self._selected.clear()
        self._pending = None
        self._restore_application = None
        token = self._generation
        self.confirmationChanged.emit()
        self.changed.emit()
        return token

    @Slot()
    def reload(self):
        if self._closed or not self._activated:
            return
        generation = self._invalidate()
        if self._closed or generation != self._generation:
            return
        self._model.replace(())
        self._load_page(generation, append=False)

    @Slot()
    def loadMore(self):
        if self.canLoadMore:
            self._load_page(self._generation, append=True)

    def _load_page(self, generation, *, append):
        try:
            page = self._repository.read_favorite_page(
                self._filters, self._sorting, limit=self.PAGE_SIZE,
                offset=self.loadedCount if append else 0,
            )
            if self._closed or generation != self._generation:
                return
            if append and page.revision != self._database_revision:
                self.reload()
                return
            self._database_revision = page.revision
            self._folders = page.folders
            self._total = page.total_count
            self._error = ""
            self._model.replace(page.items, append=append)
            if self._closed or generation != self._generation:
                return
            self._favorites.observe_items(page.items)
        except (FavoriteError, TypeError, ValueError) as exc:
            if not self._closed and generation == self._generation:
                self._error = f"收藏列表读取失败：{exc}"
        if not self._closed and generation == self._generation:
            self.changed.emit()

    @Slot("QVariantMap", result=bool)
    def applyFilters(self, values):
        if self._closed:
            return False
        try:
            source = dict(self._form)
            if set(values) - set(source):
                raise FavoriteError("invalid_filter", "收藏筛选字段无效。")
            source.update(values)
            folder = self._folder(source["folderKey"]) if source["scope"] == "folder" else None
            kind = source["kind"]
            category = FavoriteTagCategory(kind) if kind in {item.value for item in FavoriteTagCategory} else None
            if kind not in {"all", "post", "pool"} and category is None:
                raise FavoriteError("invalid_filter", "收藏类型无效。")
            filters = FavoriteFilter(
                folder_scope=FavoriteFolderScope(source["scope"]),
                folder_id=folder.id if folder else None,
                object_type=FavoriteObjectType.TAG if category else (None if kind == "all" else FavoriteObjectType(kind)),
                tag_category=category, exact_tag=source["exactTag"] or None,
                exact_artist=source["exactArtist"] or None,
                rating=None if source["rating"] == "all" else source["rating"],
            )
            sorting = FavoriteSort(FavoriteSortField(source["sortField"]), FavoriteSortDirection(source["sortDirection"]))
        except (FavoriteError, TypeError, ValueError) as exc:
            self._error = f"收藏筛选无效：{exc}"
            self.changed.emit()
            return False
        self._form, self._filters, self._sorting = source, filters, sorting
        self.reload()
        return not bool(self._error)

    @Slot(int, result="QVariantMap")
    def rowAt(self, row: int):
        return self._model.mapping(row)

    @Slot(str, result=bool)
    def isSelected(self, key: str):
        return key in self._selected

    @Slot(str, bool, int, result=bool)
    def setSelected(self, key: str, desired: bool, generation: int):
        if not self._current(generation) or self._model.by_key(key) is None or type(desired) is not bool:
            return False
        if desired:
            self._selected.add(key)
        else:
            self._selected.discard(key)
        self._selection_revision += 1
        self.changed.emit()
        return True

    @Slot()
    @Slot(int)
    def selectLoaded(self, generation: int | None = None):
        if self._closed or (generation is not None and not self._current(generation)):
            return
        self._selected = {str(item.id) for item in self._model.items}
        self._selection_revision += 1
        self.changed.emit()

    @Slot()
    def clearSelection(self):
        if not self._closed:
            self._selected.clear()
            self._selection_revision += 1
            self.changed.emit()

    @Slot(str, result="QVariantMap")
    def postState(self, key: str):
        return self._favorites.state(post_identity(key))

    @Slot(str, bool, int, result=bool)
    def setPostFavorite(self, key: str, desired: bool, generation: int):
        item = self._model.by_key(key)
        if not self._current(generation) or item is None or item.post is None:
            return False
        return self._favorites.set_post(item.post, desired, guard=lambda: self._current(generation) and self._model.by_key(key) == item)

    def _folder(self, key):
        folder = next((folder for folder in self._folders if str(folder.id) == key), None)
        if folder is None:
            raise FavoriteError("unknown_folder", "请重新选择有效的收藏文件夹。")
        return folder

    @Slot(str, int, str, result=bool)
    def saveFolder(self, key: str, generation: int, name: str):
        if not self._current(generation):
            return False
        guard = lambda: self._current(generation)
        try:
            if not key:
                return self._favorites.create_folder(name, guard=guard)
            folder = self._folder(key)
            return self._favorites.rename_folder(folder.id, name, self._database_revision, guard=guard)
        except FavoriteError as exc:
            self._error = str(exc)
            self.changed.emit()
            return False

    @Slot(str, int, int, result=bool)
    def prepareMove(self, target: str, selection_revision: int, generation: int):
        return self._prepare("move", generation, selection_revision, target=target)

    @Slot(int, int, result=bool)
    def prepareDelete(self, selection_revision: int, generation: int):
        return self._prepare("delete", generation, selection_revision)

    @Slot(str, bool, int, result=bool)
    def prepareDeleteFolder(self, key: str, delete_contents: bool, generation: int):
        return self._prepare("folder", generation, folder=key, delete_contents=delete_contents)

    def _prepare(self, operation, generation, selection_revision=None, *, target="", folder="", delete_contents=False):
        if not self._current(generation) or self._favorites.busy:
            return False
        if selection_revision is not None and selection_revision != self._selection_revision:
            return False
        try:
            mode = None
            if operation == "folder":
                snapshot = self._repository.preview_folder(self._folder(folder))
                mode = FolderDeleteMode.DELETE_FAVORITES if delete_contents else FolderDeleteMode.MOVE_TO_UNCATEGORIZED
                count = len(snapshot.items)
                text = f'删除文件夹“{snapshot.folder.name}”，并' + (f"删除其中 {count} 项本地收藏。" if delete_contents else f"把其中 {count} 项收藏移入未分类。")
            else:
                items = tuple(item for item in self._model.items if str(item.id) in self._selected)
                snapshot = self._repository.preview_items(items, target_folder=self._folder(target) if target else None)
                if operation == "move":
                    target_id = snapshot.target_folder.id if snapshot.target_folder else None
                    count = sum(item.folder_id != target_id for item in snapshot.items)
                    text = f"已选择 {len(items)} 项，其中 {count} 项将移动到“{snapshot.target_folder.name if snapshot.target_folder else '未分类'}”。"
                else:
                    text = f"取消所选 {len(snapshot.items)} 项本地收藏。此操作不会修改原站数据。"
            if not self._current(generation) or (selection_revision is not None and selection_revision != self._selection_revision):
                return False
            self._pending = _Pending(uuid4().hex, generation, operation, snapshot, mode, text, selection_revision)
            self.confirmationChanged.emit()
            return True
        except (FavoriteError, TypeError, ValueError) as exc:
            if self._current(generation):
                self._error = f"无法确认收藏操作：{exc}"
                self.changed.emit()
            return False

    @Slot(str)
    def cancelConfirmation(self, token: str):
        if self._pending and self._pending.token == token:
            self._pending = None
            self.confirmationChanged.emit()

    @Slot(str, result=bool)
    def confirm(self, token: str):
        pending = self._pending
        if pending is None or token != pending.token or not self._current(pending.generation):
            return False
        guard = lambda: (
            self._current(pending.generation) and self._pending is pending
            and (pending.selection_revision is None or pending.selection_revision == self._selection_revision)
        )
        if pending.operation == "move":
            result = self._favorites.move_items(pending.snapshot, guard=guard)
        elif pending.operation == "delete":
            result = self._favorites.delete_items(pending.snapshot, guard=guard)
        else:
            result = self._favorites.delete_folder(pending.snapshot, pending.mode, guard=guard)
        if self._pending is pending:
            self._pending = None
            self.confirmationChanged.emit()
        return result

    @Slot(str, int, float, int, float, float, result=bool)
    @Slot(str, int, float, int, float, float, float, result=bool)
    def openItem(self, key: str, generation: int, content_y: float, index: int, width: float, height: float, anchor_offset: float = 0.0):
        item = self._model.by_key(key)
        if not self._current(generation) or item is None or item.post is None or not all(math.isfinite(value) for value in (content_y, width, height, anchor_offset)):
            return False
        self._return_snapshot = (key, generation, content_y, index, width, height, frozenset(self._selected), anchor_offset)
        return self._navigation.open_saved_post(item.post, source_page="favorites")

    @Slot(float, float, float, float, result="QVariantMap")
    def takeRestorePlan(self, width: float, height: float, origin: float, content_height: float):
        snapshot, self._return_snapshot = self._return_snapshot, None
        if self._closed or snapshot is None:
            return {"available": False}
        key, generation, position, index, old_width, old_height, selection, anchor_offset = snapshot
        current_generation = self._generation
        model_revision = self._model.revision
        exact = generation == self._generation and width == old_width and height == old_height
        if exact:
            self._selected = set(selection)
            self._selection_revision += 1
            self.changed.emit()
        else:
            index = next((i for i, item in enumerate(self._model.items) if str(item.id) == key), min(index, self.loadedCount - 1))
        if not self._current(current_generation) or model_revision != self._model.revision:
            return {"available": False}
        self._restore_application = (current_generation, model_revision)
        maximum = max(origin, origin + content_height - height)
        return {
            "available": True, "exact": exact, "index": index,
            "contentY": max(origin, min(position, maximum)), "anchorOffset": anchor_offset,
            "generation": current_generation, "modelRevision": model_revision,
        }

    @Slot(int, int, result=bool)
    def restorePlanCurrent(self, generation: int, model_revision: int):
        return (
            self._current(generation) and self._model.revision == model_revision
            and self._restore_application == (generation, model_revision)
        )

    @Slot(int)
    def finishRestorePlan(self, generation: int):
        if self._restore_application and self._restore_application[0] == generation:
            self._restore_application = None

    def _current(self, generation):
        return not self._closed and generation == self._generation

    @Slot()
    def _collection_changed(self):
        if self._activated and not self._closed:
            # A removed active folder becomes the unclassified view.
            if self._filters.folder_id is not None:
                try:
                    self._repository.get_folder(self._filters.folder_id)
                except FavoriteError:
                    self._form.update(scope="uncategorized", folderKey="")
                    self._filters = replace(self._filters, folder_scope=FavoriteFolderScope.UNCATEGORIZED, folder_id=None)
            self.reload()

    def shutdown(self):
        self._closed = True
        self._generation += 1
        self._pending = None
