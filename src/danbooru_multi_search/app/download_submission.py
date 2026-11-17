"""Confirm download destinations and settings before creating persistent work."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from danbooru_multi_search.core.downloads import (
    DownloadBatchSnapshot,
    DownloadConflictPolicy,
    DownloadError,
)
from danbooru_multi_search.core.pool_downloads import PoolDownloadPlan
from danbooru_multi_search.core.pools import DanbooruPool
from danbooru_multi_search.core.settings import SettingsController
from danbooru_multi_search.danbooru.models import DanbooruPost


class DownloadBatchCreator(Protocol):
    """The ordinary batch operation used after confirmation."""

    def enqueue_batch(
        self,
        posts: Sequence[DanbooruPost],
        directory: Path,
        policy: DownloadConflictPolicy,
    ) -> DownloadBatchSnapshot: ...


class PoolDownloadPlanCreator(Protocol):
    """The pool preparation operation used after confirmation."""

    def start(
        self,
        pool: DanbooruPool,
        directory: Path,
        policy: DownloadConflictPolicy,
    ) -> PoolDownloadPlan: ...


class DownloadSubmissionService:
    """Keep both confirmation flows on the same settings-and-source ordering."""

    def __init__(self, settings: SettingsController) -> None:
        self._settings = settings

    def submit_posts(
        self,
        manager: DownloadBatchCreator,
        posts: Sequence[DanbooruPost],
        directory: Path,
        policy: str,
        save_default: bool,
        *,
        source_current: Callable[[], bool],
    ) -> DownloadBatchSnapshot:
        """Persist a finite batch only while the captured source remains current.

        source_current only observes the caller's captured identity. Settings
        persistence can invoke callbacks, so it is checked again before enqueue.
        """
        parsed = self._prepare(
            directory,
            policy,
            save_default,
            source_current,
            before_save_message="作品来源已改变，请重新选择。",
            after_save_message="作品来源已改变，尚未创建下载任务。",
        )
        return manager.enqueue_batch(posts, directory, parsed)

    def submit_pool(
        self,
        manager: PoolDownloadPlanCreator,
        pool: DanbooruPool,
        directory: Path,
        policy: str,
        save_default: bool,
        *,
        source_current: Callable[[], bool],
    ) -> PoolDownloadPlan:
        """Persist a pool plan after destination, settings and source checks."""
        parsed = self._prepare(
            directory,
            policy,
            save_default,
            source_current,
            before_save_message="作品池来源已改变，尚未创建计划。",
            after_save_message="作品池来源已改变，尚未创建计划。",
        )
        return manager.start(pool, directory, parsed)

    def _prepare(
        self,
        directory: Path,
        policy: str,
        save_default: bool,
        source_current: Callable[[], bool],
        *,
        before_save_message: str,
        after_save_message: str,
    ) -> DownloadConflictPolicy:
        if not source_current():
            raise DownloadError("stale_download", before_save_message)
        parsed = DownloadConflictPolicy(policy)
        if not directory.is_absolute() or (
            directory.exists() and not directory.is_dir()
        ):
            raise DownloadError(
                "invalid_directory",
                "下载目录已不可用，请重新选择文件夹。",
            )
        if save_default:
            self._settings.set_download_directory(directory)
        if not source_current():
            raise DownloadError("stale_download", after_save_message)
        return parsed
