"""Bind durable favorite and redirect facts to Qt cache maintenance."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from typing import Iterator

from PySide6.QtCore import QObject, Slot

from danbooru_multi_search.storage.cache_protection import CacheProtectionError, CacheProtectionRepository, CacheProtectionSnapshot
from danbooru_multi_search.ui.favorites import FavoriteController
from danbooru_multi_search.ui.image_cache import ImageCacheController, cache_key, normalized_cache_url


class CacheProtectionController(QObject):
    def __init__(self, repository: CacheProtectionRepository, favorites: FavoriteController,
                 images: ImageCacheController, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._repository, self._favorites, self._images = repository, favorites, images
        self._closed = False
        self._cleanup_owner: str | None = None
        self._pending_aliases: set[tuple[str, str]] = set()
        self._error = ""
        self._alias_error = ""
        self._snapshot: CacheProtectionSnapshot | None = None
        self.reload()
        images.configure_protection(self.protection, self.remember_redirect, self.can_store)
        favorites.collectionChanged.connect(self.reload)
        favorites.busyChanged.connect(self._busy_changed)

    def _occupied(self) -> bool:
        return self._favorites._busy or self._favorites._external_write not in {None, self._cleanup_owner}

    @staticmethod
    def _keys(snapshot: CacheProtectionSnapshot) -> frozenset[bytes]:
        keys = {cache_key(url) for url in snapshot.urls if normalized_cache_url(url).scheme().casefold() in {"http", "https"}}
        aliases = tuple((cache_key(original), cache_key(final)) for original, final in snapshot.aliases)
        while True:
            expanded = keys | {final for original, final in aliases if original in keys}
            if expanded == keys:
                return frozenset(keys)
            keys = expanded

    def set_cleanup_owner(self, owner: str | None) -> None:
        self._cleanup_owner = owner

    def can_store(self) -> bool:
        return not self._closed and self._snapshot is not None and not self._error and not self._alias_error and not self._pending_aliases

    @Slot()
    def reload(self) -> None:
        if self._closed:
            return
        try:
            self._snapshot = self._repository.load_snapshot()
            self._error = ""
        except CacheProtectionError as exc:
            self._error = str(exc)
            self._images._set_status(f"收藏图片保护暂不可用，已保留现有缓存：{exc}")

    def remember_redirect(self, original: str, final: str) -> None:
        if self._closed or original == final:
            return
        self._pending_aliases.add((original, final))
        self._flush_aliases()

    def _flush_aliases(self) -> None:
        if self._closed or self._occupied():
            return
        try:
            for original, final in tuple(self._pending_aliases):
                self._repository.remember_redirect(original, final)
                self._pending_aliases.discard((original, final))
            self._alias_error = ""
        except CacheProtectionError as exc:
            self._alias_error = str(exc)
            self._images._set_status(f"图片重定向保护信息未能保存，已保留现有缓存：{exc}")

    @contextmanager
    def protection(self) -> Iterator[frozenset[bytes] | None]:
        """None means retain everything until complete current facts are available."""
        if self._closed or self._occupied():
            self._images._set_status("收藏正在变更，暂时保留现有图片缓存。")
            yield None
            return
        self._flush_aliases()
        if self._pending_aliases:
            yield None
            return
        stack = ExitStack()
        try:
            snapshot = stack.enter_context(self._repository.locked_snapshot(self._snapshot))
            keys = self._keys(snapshot)
            self._snapshot, self._error = snapshot, ""
        except (CacheProtectionError, TypeError, ValueError) as exc:
            stack.close()
            self._error = str(exc)
            self._images._set_status(f"收藏图片保护暂不可用，已保留现有缓存：{exc}")
            yield None
            return
        with stack:
            yield keys

    @Slot()
    def _busy_changed(self) -> None:
        if not self._closed and not self._occupied():
            self._flush_aliases()

    def shutdown(self) -> None:
        self._flush_aliases()
        self._closed = True
