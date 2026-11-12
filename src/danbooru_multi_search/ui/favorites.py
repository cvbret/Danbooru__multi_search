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
)
from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.storage.favorites import FavoriteRepository


class FavoriteController(QObject):
    """Publish favorite facts only after successful reads or committed writes."""

    stateChanged = Signal()
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

    def _change(self, build_candidates, desired: bool, guard) -> bool:
        if self._closed or self._busy or type(desired) is not bool or not guard():
            return False
        self._busy = True
        self.busyChanged.emit()
        try:
            if self._closed or not guard():
                return False
            candidates = build_candidates()
            if not candidates:
                return False
            # Recheck after any source callbacks and before starting a transaction.
            if self._closed or not guard():
                return False
            if desired:
                items = self._repository.add_favorites(candidates)
                values = {item.identity: item for item in items}
            else:
                value = candidates[0]
                identity = value if isinstance(value, FavoriteIdentity) else value.identity
                self._repository.delete_by_identity(identity)
                values = {identity: None}
            if self._closed:
                return False
            self._confirmed.update(values)
            self._set_notice("")
            if not self._closed:
                self.stateChanged.emit()
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
