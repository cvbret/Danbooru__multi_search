"""Favorite mutations with shared write ownership and synchronous publication."""

from __future__ import annotations

from collections.abc import Callable

from danbooru_multi_search.app.write_access import SharedWriteAccess
from danbooru_multi_search.core.favorites import (
    FavoriteCandidate,
    FavoriteError,
    FavoriteIdentity,
    FavoriteItem,
    FavoriteMutationSnapshot,
    FavoriteObjectType,
    FavoriteTagCategory,
    FolderDeleteMode,
)
from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.storage.favorites import FavoriteRepository


FavoriteChanges = dict[FavoriteIdentity, FavoriteItem | None]


class FavoriteWriteService:
    """Publish committed facts or failures before releasing the write reservation.

    publish and failed run synchronously. Their callbacks may close the view or
    application, and must never start a second write while this one is held.
    """

    def __init__(
        self, repository: FavoriteRepository, access: SharedWriteAccess,
    ) -> None:
        self._repository = repository
        self.access = access

    def save_posts(
        self,
        posts: tuple[DanbooruPost, ...],
        *,
        guard: Callable[[], bool],
        publish: Callable[[FavoriteChanges], bool],
        failed: Callable[[Exception], None],
    ) -> bool:
        return self._change(
            lambda: tuple(FavoriteCandidate.for_post(post) for post in posts),
            True,
            guard,
            publish,
            failed,
        )

    def set_post(
        self,
        post: DanbooruPost,
        desired: bool,
        *,
        guard: Callable[[], bool],
        publish: Callable[[FavoriteChanges], bool],
        failed: Callable[[Exception], None],
    ) -> bool:
        def build() -> tuple[FavoriteCandidate | FavoriteIdentity, ...]:
            if desired:
                return (FavoriteCandidate.for_post(post),)
            if not isinstance(post, DanbooruPost) or type(post.id) is not int:
                raise FavoriteError("invalid_identifier", "作品编号必须是正整数。")
            return (FavoriteIdentity(FavoriteObjectType.POST, str(post.id)),)

        return self._change(build, desired, guard, publish, failed)

    def set_tag(
        self,
        name: str,
        category: FavoriteTagCategory,
        desired: bool,
        *,
        guard: Callable[[], bool],
        publish: Callable[[FavoriteChanges], bool],
        failed: Callable[[Exception], None],
    ) -> bool:
        return self._change(
            lambda: (FavoriteCandidate.for_tag(name, category),),
            desired,
            guard,
            publish,
            failed,
        )

    def set_pool(
        self,
        pool_id: int,
        name: str,
        desired: bool,
        *,
        guard: Callable[[], bool],
        publish: Callable[[FavoriteChanges], bool],
        failed: Callable[[Exception], None],
    ) -> bool:
        """Share confirmed series-pool facts with the existing collection."""
        return self._change(
            lambda: (FavoriteCandidate.for_pool(pool_id, name),),
            desired,
            guard,
            publish,
            failed,
        )

    def create_folder(
        self,
        name: str,
        *,
        guard: Callable[[], bool],
        publish: Callable[[FavoriteChanges], bool],
        failed: Callable[[Exception], None],
    ) -> bool:
        def command() -> FavoriteChanges:
            self._repository.create_folder(name)
            return {}
        return self._write(command, guard, publish, failed)

    def rename_folder(
        self,
        identifier: int,
        name: str,
        revision: int,
        *,
        guard: Callable[[], bool],
        publish: Callable[[FavoriteChanges], bool],
        failed: Callable[[Exception], None],
    ) -> bool:
        def command() -> FavoriteChanges:
            self._repository.rename_folder(identifier, name, expected_revision=revision)
            return {}
        return self._write(command, guard, publish, failed)

    def move_items(
        self,
        snapshot: FavoriteMutationSnapshot,
        *,
        guard: Callable[[], bool],
        publish: Callable[[FavoriteChanges], bool],
        failed: Callable[[Exception], None],
    ) -> bool:
        def command() -> FavoriteChanges:
            self._repository.move_favorites(
                tuple(item.id for item in snapshot.items),
                snapshot.target_folder.id if snapshot.target_folder else None,
                expected_revision=snapshot.revision,
            )
            return {}
        return self._write(command, guard, publish, failed)

    def delete_items(
        self,
        snapshot: FavoriteMutationSnapshot,
        *,
        guard: Callable[[], bool],
        publish: Callable[[FavoriteChanges], bool],
        failed: Callable[[Exception], None],
    ) -> bool:
        def command() -> FavoriteChanges:
            self._repository.delete_favorites(
                tuple(item.id for item in snapshot.items), expected_revision=snapshot.revision
            )
            return {item.identity: None for item in snapshot.items}
        return self._write(command, guard, publish, failed)

    def delete_folder(
        self,
        snapshot: FavoriteMutationSnapshot,
        mode: FolderDeleteMode,
        *,
        guard: Callable[[], bool],
        publish: Callable[[FavoriteChanges], bool],
        failed: Callable[[Exception], None],
    ) -> bool:
        def command() -> FavoriteChanges:
            self._repository.delete_folder(
                snapshot.folder.id, mode, expected_revision=snapshot.revision
            )
            if mode is FolderDeleteMode.DELETE_FAVORITES:
                return {item.identity: None for item in snapshot.items}
            return {}
        return self._write(command, guard, publish, failed)

    def _change(
        self,
        build_candidates: Callable[[], tuple[FavoriteCandidate | FavoriteIdentity, ...]],
        desired: bool,
        guard: Callable[[], bool],
        publish: Callable[[FavoriteChanges], bool],
        failed: Callable[[Exception], None],
    ) -> bool:
        if type(desired) is not bool:
            return False

        def command() -> FavoriteChanges | None:
            candidates = build_candidates()
            if not candidates or self.access.closed or not guard():
                return None
            if desired:
                items = self._repository.add_favorites(candidates)
                return {item.identity: item for item in items}
            value = candidates[0]
            identity = value if isinstance(value, FavoriteIdentity) else value.identity
            self._repository.delete_by_identity(identity)
            return {identity: None}

        return self._write(command, guard, publish, failed)

    def _write(
        self,
        command: Callable[[], FavoriteChanges | None],
        guard: Callable[[], bool],
        publish: Callable[[FavoriteChanges], bool],
        failed: Callable[[Exception], None],
    ) -> bool:
        if self.access.busy or not guard() or not self.access.begin_local_write():
            return False
        try:
            if self.access.closed or not guard():
                return False
            values = command()
            if values is None or self.access.closed:
                return False
            return publish(values)
        except (FavoriteError, TypeError, ValueError) as exc:
            failed(exc)
            return False
        finally:
            self.access.end_local_write()
