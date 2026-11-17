"""Immutable, Qt-free facts for preparing a finite series download."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import re

from danbooru_multi_search.core.downloads import DownloadConflictPolicy, DownloadError, DownloadRequest
from danbooru_multi_search.core.pool_progress import normalize_api_source
from danbooru_multi_search.core.pools import MAX_POOL_POSTS, PoolPageReference

POOL_DOWNLOAD_GROUP_SIZE = 1000


class PoolDownloadState(StrEnum):
    PREPARING = "preparing"
    PAUSED = "paused"
    READY = "ready"


class PoolDownloadPageState(StrEnum):
    WAITING = "waiting"
    READY = "ready"
    ENQUEUED = "enqueued"
    DELETED = "deleted"
    UNAVAILABLE = "unavailable"
    NOT_RETURNED = "not_returned"
    FAILED = "failed"


def _key(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise DownloadError("invalid_record", "整池下载记录编号无效。")


def pool_download_request(reference: PoolPageReference, url: str, extension: str) -> DownloadRequest:
    """Keep every original page number, including repeated post identifiers."""
    if not isinstance(reference, PoolPageReference):
        raise DownloadError("invalid_request", "整池下载页位无效。")
    return DownloadRequest(reference.post_id, url, extension, f"{reference.page_number:05d}")


@dataclass(frozen=True, slots=True)
class PoolDownloadPlan:
    plan_id: str
    source: str
    pool_id: int
    name: str
    total: int
    directory: Path
    policy: DownloadConflictPolicy
    state: PoolDownloadState
    generation: int = 0
    message: str = ""

    def __post_init__(self) -> None:
        _key(self.plan_id)
        if normalize_api_source(self.source) != self.source:
            raise DownloadError("invalid_record", "整池下载 API 来源无效。")
        if type(self.pool_id) is not int or not 1 <= self.pool_id <= 2**63 - 1:
            raise DownloadError("invalid_record", "整池下载作品池编号无效。")
        if not isinstance(self.name, str) or not self.name.strip():
            raise DownloadError("invalid_record", "整池下载名称无效。")
        if type(self.total) is not int or not 1 <= self.total <= MAX_POOL_POSTS:
            raise DownloadError("invalid_record", "整池下载页数必须在 1—25000 之间。")
        if not isinstance(self.directory, Path) or not self.directory.is_absolute():
            raise DownloadError("invalid_directory", "下载目录必须使用绝对路径。")
        if not isinstance(self.policy, DownloadConflictPolicy) or not isinstance(self.state, PoolDownloadState):
            raise DownloadError("invalid_record", "整池下载状态或重名策略无效。")
        if type(self.generation) is not int or not 0 <= self.generation < 2**63 - 1:
            raise DownloadError("invalid_record", "整池下载执行代次无效。")
        if not isinstance(self.message, str):
            raise DownloadError("invalid_record", "整池下载说明无效。")


@dataclass(frozen=True, slots=True)
class PoolDownloadPage:
    reference: PoolPageReference
    state: PoolDownloadPageState
    request: DownloadRequest | None = None
    item_id: str | None = None
    batch_id: str | None = None
    message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reference, PoolPageReference) or not isinstance(self.state, PoolDownloadPageState):
            raise DownloadError("invalid_record", "整池下载页位状态无效。")
        ready = self.state in {PoolDownloadPageState.READY, PoolDownloadPageState.ENQUEUED}
        if ready:
            if not isinstance(self.request, DownloadRequest) or self.request != pool_download_request(
                self.reference, self.request.source_url, self.request.file_ext,
            ):
                raise DownloadError("invalid_record", "整池下载原文件与页位不一致。")
        elif self.request is not None:
            raise DownloadError("invalid_record", "未准备完成的页位不能包含原文件请求。")
        if self.state is PoolDownloadPageState.ENQUEUED:
            _key(self.item_id)
            _key(self.batch_id)
        elif self.item_id is not None or self.batch_id is not None:
            raise DownloadError("invalid_record", "未加入下载的页位不能关联下载任务。")
        if not isinstance(self.message, str):
            raise DownloadError("invalid_record", "整池下载页位说明无效。")


@dataclass(frozen=True, slots=True)
class PoolDownloadSummary:
    plan: PoolDownloadPlan
    prepared: int
    enqueued: int
    unavailable: int
