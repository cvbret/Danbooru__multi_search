"""Qt cache-key conversion and image/favorite connections for protection."""

from __future__ import annotations

from PySide6.QtCore import QObject

from danbooru_multi_search.app.cache_protection import CacheProtectionService
from danbooru_multi_search.storage.cache_protection import CacheProtectionSnapshot
from danbooru_multi_search.ui.favorites import FavoriteController
from danbooru_multi_search.ui.image_cache import ImageCacheController, cache_key, normalized_cache_url


class CacheProtectionController(QObject):
    def __init__(
        self,
        service: CacheProtectionService,
        favorites: FavoriteController,
        images: ImageCacheController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        service.report_status = images._set_status
        service.reload()
        convert_keys = self._keys
        images.configure_protection(
            lambda: service.protection(convert_keys),
            service.remember_redirect,
            service.can_store,
        )
        favorites.collectionChanged.connect(service.reload)

    @staticmethod
    def _keys(snapshot: CacheProtectionSnapshot) -> frozenset[bytes]:
        keys = {
            cache_key(url) for url in snapshot.urls
            if normalized_cache_url(url).scheme().casefold() in {"http", "https"}
        }
        aliases = tuple(
            (cache_key(original), cache_key(final)) for original, final in snapshot.aliases
        )
        while True:
            expanded = keys | {final for original, final in aliases if original in keys}
            if expanded == keys:
                return frozenset(keys)
            keys = expanded
