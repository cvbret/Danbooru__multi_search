"""Gallery selection and commands guarded by stable post identities."""

from __future__ import annotations

from PySide6.QtCore import QObject, Property, Signal, Slot

from danbooru_multi_search.core.favorites import FavoriteError
from danbooru_multi_search.ui.gallery import DanbooruPostListModel
from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.ui.favorites import FavoriteController, post_identity


class GallerySelectionController(QObject):
    """Bind local selection to post IDs and invalidate actions after replacement."""

    changed = Signal()

    def __init__(
        self, model: DanbooruPostListModel, favorites: FavoriteController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._model = model
        self._favorites = favorites
        self._selected: set[str] = set()
        self._posts = {}
        self._operation = model.display_operation_id
        self._revision = 0
        self._closed = False
        model.revisionChanged.connect(self._model_changed)
        favorites.stateChanged.connect(self.changed)
        favorites.busyChanged.connect(self.changed)
        self._model_changed()

    @Property(int, notify=changed)
    def revision(self) -> int:
        return self._revision

    @Property(int, notify=changed)
    def modelRevision(self) -> int:
        return self._model.revision

    @Property(int, notify=changed)
    def selectedCount(self) -> int:
        return len(self._selected)

    @Property(bool, notify=changed)
    def canSaveSelected(self) -> bool:
        return (
            not self._closed and not self._favorites.busy and bool(self._selected)
            and all(self._favorites.state(post_identity(key))["known"] for key in self._selected)
        )

    @Slot(int, int, result=str)
    def postKeyAt(self, row: int, revision: int) -> str:
        if self._closed or revision != self._model.revision:
            return ""
        post = self._model.post_at(row)
        return str(post.id) if post is not None else ""

    @Slot(str, result="QVariantMap")
    def postState(self, key: str) -> dict:
        try:
            state = self._favorites.state(post_identity(key))
        except FavoriteError:
            state = {"known": False, "saved": False}
        return {
            **state,
            "selected": key in self._selected,
            "enabled": not self._closed and not self._favorites.busy and state["known"],
        }

    @Slot(str, bool, int, result=bool)
    def setSelected(self, key: str, selected: bool, revision: int) -> bool:
        if not self._current(key, revision) or type(selected) is not bool:
            return False
        before = set(self._selected)
        if selected:
            self._selected.add(key)
        else:
            self._selected.discard(key)
        if self._selected != before:
            self._revision += 1
            self.changed.emit()
        return True

    @Slot()
    def clearSelection(self) -> None:
        if self._closed or not self._selected:
            return
        self._selected.clear()
        self._revision += 1
        self.changed.emit()

    @Slot(str, bool, int, result=bool)
    def setPostFavorite(self, key: str, desired: bool, revision: int) -> bool:
        if not self._current(key, revision) or not self.postState(key)["enabled"]:
            return False
        post = self._posts[key]
        return self._favorites.set_post(
            post, desired, guard=lambda: self._current(key, revision)
        )

    @Slot(int, int, result=bool)
    def saveSelected(self, selection_revision: int, model_revision: int) -> bool:
        keys = tuple(key for key in self._posts if key in self._selected)

        def current():
            return (
                not self._closed and self._revision == selection_revision
                and self._model.revision == model_revision
                and all(key in self._posts for key in keys)
            )

        if not current() or not self.canSaveSelected:
            return False
        return self._favorites.save_posts(
            tuple(self._posts[key] for key in keys), guard=current
        )

    def _current(self, key: str, revision: int) -> bool:
        return (
            not self._closed and revision == self._model.revision and key in self._posts
        )

    def selected_posts(self, selection_revision: int, model_revision: int) -> tuple[DanbooruPost, ...] | None:
        """Read the explicitly selected works in current gallery order."""
        if self._closed or self._revision != selection_revision or self._model.revision != model_revision:
            return None
        return tuple(post for key, post in self._posts.items() if key in self._selected)

    @Slot()
    def _model_changed(self) -> None:
        if self._closed:
            return
        operation = self._model.display_operation_id
        self._posts = {str(post.id): post for post in self._model.posts}
        if operation != self._operation:
            self._selected.clear()
        else:
            self._selected.intersection_update(self._posts)
        self._operation = operation
        self._revision += 1
        self._favorites.ensure(post_identity(key) for key in self._posts)
        self.changed.emit()

    def shutdown(self) -> None:
        self._closed = True
        self._selected.clear()
