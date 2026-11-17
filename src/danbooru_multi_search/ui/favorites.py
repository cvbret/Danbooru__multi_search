"""One confirmed favorite-state owner shared by all interface sources."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from PySide6.QtCore import QObject, Property, Signal, Slot

from danbooru_multi_search.app.favorites import FavoriteChanges, FavoriteWriteService
from danbooru_multi_search.core.favorites import (
    FavoriteError,
    FavoriteIdentity,
    FavoriteItem,
    FavoriteObjectType,
    FavoriteTagCategory,
    FavoriteMutationSnapshot,
    FavoriteCollectionSnapshot,
    FolderDeleteMode,
)
from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.storage.favorites import FavoriteRepository


class FavoriteController(QObject):
    """Publish favorite facts only after successful reads or committed writes."""

    stateChanged = Signal()
    collectionChanged = Signal()
    noticeChanged = Signal()
    busyChanged = Signal()

    def __init__(
        self, repository: FavoriteRepository, writes: FavoriteWriteService,
        parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._repository = repository
        self._writes = writes
        self._confirmed: dict[FavoriteIdentity, FavoriteItem | None] = {}
        self._observed: set[FavoriteIdentity] = set()
        self._notice = ""
        self._closed = False
        writes.access.changed.connect(self._access_changed)

    @Property(str, notify=noticeChanged)
    def noticeText(self) -> str:
        return self._notice

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._closed or self._writes.access.busy

    def publish_external_snapshot(self, token: str, snapshot: FavoriteCollectionSnapshot) -> bool:
        """Publish committed facts while the external write reservation is held."""

        if self._closed or self._writes.access.external_owner != token:
            return False
        current = {item.identity: item for item in snapshot.favorites}
        self._observed.update(current)
        self._confirmed = {identity: current.get(identity) for identity in self._observed}
        self._set_notice("")
        if self._closed or self._writes.access.external_owner != token:
            return False
        self.stateChanged.emit()
        if self._closed or self._writes.access.external_owner != token:
            return False
        self.collectionChanged.emit()
        return not self._closed and self._writes.access.external_owner == token

    def state(self, identity: FavoriteIdentity) -> dict[str, bool]:
        """Return cached facts; reading this value never performs database work."""

        return {
            "known": identity in self._confirmed,
            "saved": self._confirmed.get(identity) is not None,
        }

    def ensure(self, identities: Iterable[FavoriteIdentity], *, refresh=False) -> None:
        if self._closed:
            return
        identities = tuple(dict.fromkeys(identities))
        self._observed.update(identities)
        values = {}
        try:
            for identity in identities:
                if self._closed:
                    return
                if refresh or identity not in self._confirmed:
                    values[identity] = self._repository.get_by_identity(identity)
        except (FavoriteError, TypeError, ValueError) as exc:
            self._set_notice(f"收藏状态读取失败：{exc}")
            return
        if self._closed:
            return
        if values:
            self._confirmed.update(values)
            self.stateChanged.emit()

    @Slot()
    def retryStates(self) -> None:
        if self._closed or self._writes.access.local_write_active:
            return
        self._set_notice("")
        self.ensure(tuple(self._observed), refresh=True)

    def save_posts(
        self, posts: tuple[DanbooruPost, ...], *, guard: Callable[[], bool],
    ) -> bool:
        return self._writes.save_posts(
            posts,
            guard=lambda: not self._closed and guard(),
            publish=self._publish,
            failed=self._write_failed,
        )

    def set_post(
        self, post: DanbooruPost, desired: bool, *, guard: Callable[[], bool],
    ) -> bool:
        return self._writes.set_post(
            post, desired,
            guard=lambda: not self._closed and guard(),
            publish=self._publish,
            failed=self._write_failed,
        )

    def set_tag(
        self, name: str, category: FavoriteTagCategory, desired: bool,
        *, guard: Callable[[], bool],
    ) -> bool:
        return self._writes.set_tag(
            name, category, desired,
            guard=lambda: not self._closed and guard(),
            publish=self._publish,
            failed=self._write_failed,
        )

    def set_pool(
        self, pool_id: int, name: str, desired: bool, *, guard: Callable[[], bool],
    ) -> bool:
        return self._writes.set_pool(
            pool_id, name, desired,
            guard=lambda: not self._closed and guard(),
            publish=self._publish,
            failed=self._write_failed,
        )

    def observe_items(self, items: tuple[FavoriteItem, ...]) -> None:
        """Accept facts already read successfully in a consistent collection page."""

        if self._closed:
            return
        self._observed.update(item.identity for item in items)
        self._confirmed.update((item.identity, item) for item in items)
        self.stateChanged.emit()

    def create_folder(
        self, name: str, *, guard: Callable[[], bool],
    ) -> bool:
        return self._writes.create_folder(
            name,
            guard=lambda: not self._closed and guard(),
            publish=self._publish,
            failed=self._write_failed,
        )

    def rename_folder(
        self, identifier: int, name: str, revision: int, *, guard: Callable[[], bool],
    ) -> bool:
        return self._writes.rename_folder(
            identifier, name, revision,
            guard=lambda: not self._closed and guard(),
            publish=self._publish,
            failed=self._write_failed,
        )

    def move_items(
        self, snapshot: FavoriteMutationSnapshot, *, guard: Callable[[], bool],
    ) -> bool:
        return self._writes.move_items(
            snapshot,
            guard=lambda: not self._closed and guard(),
            publish=self._publish,
            failed=self._write_failed,
        )

    def delete_items(
        self, snapshot: FavoriteMutationSnapshot, *, guard: Callable[[], bool],
    ) -> bool:
        return self._writes.delete_items(
            snapshot,
            guard=lambda: not self._closed and guard(),
            publish=self._publish,
            failed=self._write_failed,
        )

    def delete_folder(
        self, snapshot: FavoriteMutationSnapshot, mode: FolderDeleteMode,
        *, guard: Callable[[], bool],
    ) -> bool:
        return self._writes.delete_folder(
            snapshot, mode,
            guard=lambda: not self._closed and guard(),
            publish=self._publish,
            failed=self._write_failed,
        )

    def _publish(self, values: FavoriteChanges) -> bool:
        if self._closed:
            return False
        self._confirmed.update(values)
        self._set_notice("")
        if not self._closed:
            self.stateChanged.emit()
        if not self._closed:
            self.collectionChanged.emit()
        return not self._closed

    def _write_failed(self, error: Exception) -> None:
        self._set_notice(f"收藏操作失败：{error}")

    def _access_changed(self) -> None:
        if not self._closed:
            self.busyChanged.emit()

    def _set_notice(self, text: str) -> None:
        if not self._closed and text != self._notice:
            self._notice = text
            self.noticeChanged.emit()

    def shutdown(self) -> None:
        self._closed = True
        self._writes.access.close()


def post_identity(key: str) -> FavoriteIdentity:
    """Interpret a canonical text identifier without QML numeric conversion."""

    return FavoriteIdentity(FavoriteObjectType.POST, key)
