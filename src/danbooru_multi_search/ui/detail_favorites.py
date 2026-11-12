"""Favorite commands bound to the currently open detail and tag groups."""

from __future__ import annotations

from PySide6.QtCore import QObject, Property, Signal, Slot

from danbooru_multi_search.core.favorites import (
    FavoriteIdentity, FavoriteObjectType, FavoriteTagCategory,
)
from danbooru_multi_search.danbooru.details import DanbooruDetailController
from danbooru_multi_search.ui.favorites import FavoriteController, post_identity


class DetailFavoritesController(QObject):
    """Validate detail-source identities without changing media or navigation."""

    changed = Signal()

    def __init__(
        self, detail: DanbooruDetailController, favorites: FavoriteController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._detail = detail
        self._favorites = favorites
        self._revision = 0
        self._closed = False
        detail.openChanged.connect(self._source_changed)
        favorites.stateChanged.connect(self.changed)
        favorites.busyChanged.connect(self.changed)
        self._source_changed()

    @Property(int, notify=changed)
    def revision(self) -> int:
        return self._revision

    @Property(str, notify=changed)
    def postKey(self) -> str:
        post = self._detail.current_post
        return str(post.id) if not self._closed and self._detail.isOpen and post else ""

    @Property("QVariantMap", notify=changed)
    def postState(self) -> dict:
        if not self.postKey:
            return {"known": False, "saved": False, "enabled": False}
        state = self._favorites.state(post_identity(self.postKey))
        return {**state, "enabled": state["known"] and not self._favorites.busy}

    @Slot(str, str, result="QVariantMap")
    def tagState(self, name: str, category: str) -> dict:
        allowed = self._tag_allowed(name, category)
        state = (
            self._favorites.state(FavoriteIdentity(FavoriteObjectType.TAG, name))
            if allowed else {"known": False, "saved": False}
        )
        return {
            **state,
            "enabled": allowed and state["known"] and not self._favorites.busy,
        }

    @Slot(str, int, bool, result=bool)
    def setPostFavorite(self, key: str, revision: int, desired: bool) -> bool:
        if not self._current(key, revision) or not self.postState["enabled"]:
            return False
        return self._favorites.set_post(
            self._detail.current_post, desired,
            guard=lambda: self._current(key, revision),
        )

    @Slot(str, int, str, str, bool, result=bool)
    def setTagFavorite(
        self, key: str, revision: int, name: str, category: str, desired: bool
    ) -> bool:
        def current():
            return self._current(key, revision) and self._tag_allowed(name, category)

        if not current() or not self.tagState(name, category)["enabled"]:
            return False
        return self._favorites.set_tag(
            name, FavoriteTagCategory(category), desired, guard=current
        )

    def _current(self, key: str, revision: int) -> bool:
        return (
            not self._closed and revision == self._revision
            and key == self.postKey and bool(key)
        )

    def _tag_allowed(self, name: str, category: str) -> bool:
        if self._closed or not self._detail.isOpen:
            return False
        try:
            group = FavoriteTagCategory(category)
        except ValueError:
            return False
        post = self._detail.current_post
        return post is not None and name in getattr(post, group.value + "_tags")

    @Slot()
    def _source_changed(self) -> None:
        if self._closed:
            return
        self._revision += 1
        post = self._detail.current_post
        if self._detail.isOpen and post is not None:
            identities = [post_identity(str(post.id))]
            for category in FavoriteTagCategory:
                identities.extend(
                    FavoriteIdentity(FavoriteObjectType.TAG, name)
                    for name in getattr(post, category.value + "_tags")
                )
            self._favorites.ensure(identities)
        self.changed.emit()

    def shutdown(self) -> None:
        self._closed = True
        self._revision += 1
