"""Qt disk caching with selective clear and capacity eviction."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
import os
from pathlib import Path
from typing import ContextManager

from PySide6.QtCore import QIODevice, QObject, QUrl
from PySide6.QtNetwork import QNetworkCacheMetaData, QNetworkDiskCache

ProtectionProvider = Callable[[], ContextManager[frozenset[bytes] | None]]


@dataclass(frozen=True, slots=True)
class CacheEntry:
    url: QUrl
    size: int
    modified: float


class ProtectedImageDiskCache(QNetworkDiskCache):
    """Remove only valid Qt entries; unknown files are never deletion targets."""

    def __init__(self, parent: QObject | None = None, *, key: Callable[[QUrl], bytes],
                 protection: ProtectionProvider = lambda: nullcontext(frozenset()),
                 status: Callable[[str], None] = lambda message: None,
                 can_store: Callable[[], bool] = lambda: True) -> None:
        super().__init__(parent)
        self._key, self._protection, self._status = key, protection, status
        self._can_store = can_store
        self._maintaining = False
        self._last_size = 0
        self._measuring = False
        self.removed_count = 0
        self.retained_count = 0
        self.protected_count = 0
        self.failed_count = 0
        self.maintenance_ok = True
        self.last_message = ""

    def entries(self) -> tuple[CacheEntry, ...]:
        directory = self.cacheDirectory()
        if not directory:
            return ()
        entries: dict[bytes, CacheEntry] = {}
        for parent, directories, names in os.walk(directory, followlinks=False):
            directories[:] = [name for name in directories if name != "prepared" and not (Path(parent) / name).is_symlink()]
            for name in names:
                path = Path(parent) / name
                if path.is_symlink():
                    continue
                metadata = self.fileMetaData(str(path))
                if not metadata.isValid() or metadata.url().scheme().casefold() not in {"http", "https"}:
                    continue
                # Prepared files already have Qt metadata but are not committed
                # entries yet; counting them here makes Qt count insertion twice.
                if not self.metaData(metadata.url()).isValid():
                    continue
                stat = path.stat()
                entries[self._key(metadata.url())] = CacheEntry(metadata.url(), stat.st_size, stat.st_mtime)
        return tuple(entries.values())

    def _maintain(self, *, clear: bool) -> int:
        if self._maintaining:
            return self._last_size
        self._maintaining = True
        try:
            entries = self.entries()
            total = sum(entry.size for entry in entries)
            self._last_size = total
            self.removed_count = 0
            self.retained_count = len(entries)
            self.protected_count = self.failed_count = 0
            self.maintenance_ok = True
            self.last_message = ""
            with self._protection() as protected:
                if protected is None:
                    self.maintenance_ok = False
                    return total
                protected_bytes = sum(entry.size for entry in entries if self._key(entry.url) in protected)
                self.protected_count = sum(self._key(entry.url) in protected for entry in entries)
                limit = 0 if clear else self.maximumCacheSize()
                for entry in sorted(entries, key=lambda value: (value.modified, self._key(value.url))):
                    if total <= limit:
                        break
                    if self._key(entry.url) in protected:
                        continue
                    if self.remove(entry.url):
                        total -= entry.size
                        self.removed_count += 1
                        self.retained_count -= 1
                        self._last_size = total
                    else:
                        self.failed_count += 1
                if self.failed_count:
                    self.maintenance_ok = False
                    self.last_message = f"已清理 {self.removed_count} 张图片，{self.failed_count} 张普通缓存图片未能删除；保留 {self.protected_count} 张受保护图片。请重试。"
                elif protected_bytes > self.maximumCacheSize():
                    self.last_message = "收藏图片缓存已超过容量上限，受保护图片继续保留。"
                elif clear:
                    self.last_message = f"已清理 {self.removed_count} 张缓存图片，保留 {self.protected_count} 张受保护图片。"
            self._status(self.last_message)
            return total
        except (OSError, RuntimeError, ValueError) as exc:
            self.maintenance_ok = False
            self.last_message = f"图片缓存维护失败，已保留未处理文件：{exc}"
            self._status(self.last_message)
            return self._last_size
        finally:
            self._maintaining = False

    def expire(self) -> int:
        if self._maintaining:
            return self._last_size
        if self._measuring:
            self._last_size = sum(entry.size for entry in self.entries())
            return self._last_size
        self._measuring = True
        try:
            current = super().cacheSize()
        finally:
            self._measuring = False
        if current <= self.maximumCacheSize():
            return current
        return self._maintain(clear=False)

    def clear(self) -> None:
        self._maintain(clear=True)

    def prepare(self, metadata: QNetworkCacheMetaData) -> QIODevice | None:
        # Do not create a disk entry whose protection facts cannot be persisted.
        return super().prepare(metadata) if self._can_store() else None

    def insert(self, device: QIODevice) -> None:
        super().insert(device)
        # Qt also checks capacity before committing the incoming entry. Recheck
        # after insertion so the new entry is part of the actual disk total.
        if super().cacheSize() > self.maximumCacheSize():
            self._maintain(clear=False)
