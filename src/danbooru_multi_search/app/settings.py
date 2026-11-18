"""Serialize merged settings changes around one confirmed snapshot."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from danbooru_multi_search.core.settings import AppSettings, SettingsError
from danbooru_multi_search.storage.settings import SettingsStore


class SettingsController:
    """Serialize merged settings updates around one in-memory snapshot."""

    def __init__(
        self,
        store: SettingsStore,
        settings: AppSettings,
    ) -> None:
        self._store = store
        self._settings = settings
        self._lock = threading.RLock()

    @property
    def settings(self) -> AppSettings:
        """Return the latest successfully persisted immutable settings."""

        with self._lock:
            return self._settings

    def update(self, **changes: Any) -> AppSettings:
        """Merge, validate, and atomically persist fields as one serialized update."""

        with self._lock:
            merged = self._settings.to_mapping()
            merged.update(changes)
            updated = AppSettings.from_mapping(merged)
            self._store.save(updated)
            self._settings = updated
            return updated

    def set_tag_csv_path(self, path: Path) -> AppSettings:
        """Persist the absolute path of the last successful CSV import."""

        local_path = Path(path)
        if not local_path.is_absolute():
            raise SettingsError("tag CSV path must be absolute")
        return self.update(tag_csv_path=str(local_path))

    def set_image_cache_limit_bytes(self, value: int) -> AppSettings:
        """Persist the image disk-cache capacity after shared validation."""

        return self.update(image_cache_limit_bytes=value)

    def set_download_directory(self, path: Path) -> AppSettings:
        """Keep the previous snapshot unless the merged directory setting saves."""
        local_path = Path(path)
        if not local_path.is_absolute():
            raise SettingsError("下载目录必须使用绝对路径。")
        return self.update(download_directory=str(local_path))
