"""One confirmed favorite-state owner shared by all interface sources."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from PySide6.QtCore import QObject, Property, Signal, Slot

from danbooru_multi_search.core.favorites import (
    FavoriteCandidate,
    FavoriteError,
    FavoriteIdentity,
    FavoriteItem,
    FavoriteObjectType,
    FavoriteTagCategory,
    FavoriteMutationSnapshot,
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
        self, repository: FavoriteRepository, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._repository = repository
        self._confirmed: dict[FavoriteIdentity, FavoriteItem | None] = {}
        self._observed: set[FavoriteIdentity] = set()
        self._notice = ""
        self._busy = False
        self._closed = False

    @Property(str, notify=noticeChanged)
    def noticeText(self) -> str:
        return self._notice

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy or self._closed

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
        if self._closed or self._busy:
            return
        self._set_notice("")
        self.ensure(tuple(self._observed), refresh=True)

    def save_posts(
        self, posts: tuple[DanbooruPost, ...], *, guard: Callable[[], bool]
    ) -> bool:
        return self._change(
            lambda: tuple(FavoriteCandidate.for_post(post) for post in posts),
            True,
            guard,
        )

    def set_post(
        self, post: DanbooruPost, desired: bool, *, guard: Callable[[], bool]
    ) -> bool:
        def build():
            if desired:
                return (FavoriteCandidate.for_post(post),)
            if not isinstance(post, DanbooruPost) or type(post.id) is not int:
                raise FavoriteError("invalid_identifier", "作品编号必须是正整数。")
            return (post_identity(str(post.id)),)

        return self._change(build, desired, guard)

    def set_tag(
        self, name: str, category: FavoriteTagCategory, desired: bool,
        *, guard: Callable[[], bool],
    ) -> bool:
        return self._change(
            lambda: (FavoriteCandidate.for_tag(name, category),), desired, guard
        )

    def observe_items(self, items: tuple[FavoriteItem, ...]) -> None:
        """Accept facts already read successfully in a consistent collection page."""

        if self._closed:
            return
        self._observed.update(item.identity for item in items)
        self._confirmed.update((item.identity, item) for item in items)
        self.stateChanged.emit()

    def create_folder(self, name: str, *, guard) -> bool:
        def command():
            self._repository.create_folder(name)
            return {}
        return self._write(command, guard)

    def rename_folder(self, identifier: int, name: str, revision: int, *, guard) -> bool:
        def command():
            self._repository.rename_folder(identifier, name, expected_revision=revision)
            return {}
        return self._write(command, guard)

    def move_items(self, snapshot: FavoriteMutationSnapshot, *, guard) -> bool:
        def command():
            self._repository.move_favorites(
                tuple(item.id for item in snapshot.items),
                snapshot.target_folder.id if snapshot.target_folder else None,
                expected_revision=snapshot.revision,
            )
            return {}
        return self._write(command, guard)

    def delete_items(self, snapshot: FavoriteMutationSnapshot, *, guard) -> bool:
        def command():
            self._repository.delete_favorites(
                tuple(item.id for item in snapshot.items), expected_revision=snapshot.revision
            )
            return {item.identity: None for item in snapshot.items}
        return self._write(command, guard)

    def delete_folder(self, snapshot: FavoriteMutationSnapshot, mode: FolderDeleteMode, *, guard) -> bool:
        def command():
            self._repository.delete_folder(
                snapshot.folder.id, mode, expected_revision=snapshot.revision
            )
            return {item.identity: None for item in snapshot.items} if mode is FolderDeleteMode.DELETE_FAVORITES else {}
        return self._write(command, guard)

    def _change(self, build_candidates, desired: bool, guard) -> bool:
        if type(desired) is not bool:
            return False

        def command():
            candidates = build_candidates()
            if not candidates or self._closed or not guard():
                return None
            if desired:
                items = self._repository.add_favorites(candidates)
                return {item.identity: item for item in items}
            value = candidates[0]
            identity = value if isinstance(value, FavoriteIdentity) else value.identity
            self._repository.delete_by_identity(identity)
            return {identity: None}

        return self._write(command, guard)

    def _write(self, command, guard) -> bool:
        if self._closed or self._busy or not guard():
            return False
        self._busy = True
        self.busyChanged.emit()
        try:
            if self._closed or not guard():
                return False
            values = command()
            if values is None or self._closed:
                return False
            self._confirmed.update(values)
            self._set_notice("")
            if not self._closed:
                self.stateChanged.emit()
            if not self._closed:
                self.collectionChanged.emit()
            return not self._closed
        except (FavoriteError, TypeError, ValueError) as exc:
            self._set_notice(f"收藏操作失败：{exc}")
            return False
        finally:
            self._busy = False
            if not self._closed:
                self.busyChanged.emit()

    def _set_notice(self, text: str) -> None:
        if not self._closed and text != self._notice:
            self._notice = text
            self.noticeChanged.emit()

    def shutdown(self) -> None:
        self._closed = True


def post_identity(key: str) -> FavoriteIdentity:
    """Interpret a canonical text identifier without QML numeric conversion."""

    return FavoriteIdentity(FavoriteObjectType.POST, key)
