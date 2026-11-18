"""Saved-image protection facts, redirect writes and locked maintenance contexts."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager

from danbooru_multi_search.app.write_access import SharedWriteAccess
from danbooru_multi_search.storage.cache_protection import (
    CacheProtectionError, CacheProtectionRepository, CacheProtectionSnapshot,
)


class CacheProtectionService:
    def __init__(
        self, repository: CacheProtectionRepository, access: SharedWriteAccess,
    ) -> None:
        self._repository = repository
        self._access = access
        self._closed = False
        self._pending_aliases: set[tuple[str, str]] = set()
        self._error = ""
        self._alias_error = ""
        self._snapshot: CacheProtectionSnapshot | None = None
        self.report_status: Callable[[str], None] | None = None
        access.changed.connect(self._access_changed)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def error(self) -> str:
        return self._error

    def _report_status(self, message: str) -> None:
        if self.report_status is not None:
            self.report_status(message)

    def _occupied(self) -> bool:
        return (
            self._access.local_write_active
            or self._access.external_owner not in {None, self._access.maintenance_owner}
        )

    def can_store(self) -> bool:
        return not self._closed and self._snapshot is not None and not self._error and not self._alias_error and not self._pending_aliases

    def reload(self) -> None:
        if self._closed:
            return
        try:
            self._snapshot = self._repository.load_snapshot()
            self._error = ""
        except CacheProtectionError as exc:
            self._error = str(exc)
            self._report_status(f"收藏图片保护暂不可用，已保留现有缓存：{exc}")

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
            self._report_status(f"图片重定向保护信息未能保存，已保留现有缓存：{exc}")

    @contextmanager
    def protection(
        self, convert_keys: Callable[[CacheProtectionSnapshot], frozenset[bytes]],
    ) -> Iterator[frozenset[bytes] | None]:
        """None means retain everything until complete current facts are available."""
        if self._closed or self._occupied():
            self._report_status("收藏正在变更，暂时保留现有图片缓存。")
            yield None
            return
        self._flush_aliases()
        if self._pending_aliases:
            yield None
            return
        stack = ExitStack()
        try:
            snapshot = stack.enter_context(self._repository.locked_snapshot(self._snapshot))
            keys = convert_keys(snapshot)
            self._snapshot, self._error = snapshot, ""
        except BaseException as exc:
            stack.close()
            if not isinstance(exc, (CacheProtectionError, TypeError, ValueError)):
                raise
            self._error = str(exc)
            self._report_status(f"收藏图片保护暂不可用，已保留现有缓存：{exc}")
            yield None
            return
        with stack:
            yield keys

    def _access_changed(self) -> None:
        if not self._closed and not self._occupied():
            self._flush_aliases()

    def shutdown(self) -> None:
        self._flush_aliases()
        self._closed = True
