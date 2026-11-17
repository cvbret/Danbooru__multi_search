"""Immutable pool-page loading facts; no Qt, requests or storage."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from danbooru_multi_search.core.pools import PoolError, PoolPageReference, pool_identifier
from danbooru_multi_search.danbooru.models import DanbooruPost

POOL_WINDOW_SIZE = 40


class PoolPageState(StrEnum):
    WAITING = "waiting"
    LOADING = "loading"
    AVAILABLE = "available"
    DELETED = "deleted"
    UNAVAILABLE = "unavailable"
    NOT_RETURNED = "not_returned"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PoolPostMetadata:
    post_id: int
    state: PoolPageState
    post: DanbooruPost | None = None
    message: str = ""

    def __post_init__(self) -> None:
        pool_identifier(self.post_id, "作品")
        if not isinstance(self.state, PoolPageState):
            raise PoolError("invalid_page", "页面加载状态无效。")
        if self.state is PoolPageState.AVAILABLE:
            if not isinstance(self.post, DanbooruPost) or self.post.id != self.post_id:
                raise PoolError("invalid_page", "页面作品与编号不一致。")
        elif self.post is not None:
            raise PoolError("invalid_page", "不可用页面不能包含作品对象。")
        if not isinstance(self.message, str):
            raise PoolError("invalid_page", "页面状态说明必须是文本。")


@dataclass(frozen=True, slots=True)
class PoolPageSlot:
    reference: PoolPageReference
    metadata: PoolPostMetadata

    def __post_init__(self) -> None:
        if (
            not isinstance(self.reference, PoolPageReference)
            or not isinstance(self.metadata, PoolPostMetadata)
            or self.reference.post_id != self.metadata.post_id
        ):
            raise PoolError("invalid_page", "页面位置与作品元数据不一致。")

    @property
    def key(self) -> str:
        return f"{self.reference.pool_id}:{self.reference.page_number}"


@dataclass(frozen=True, slots=True)
class PoolPageBatch:
    slots: tuple[PoolPageSlot, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.slots, tuple)
            or not 1 <= len(self.slots) <= POOL_WINDOW_SIZE
            or not all(isinstance(slot, PoolPageSlot) for slot in self.slots)
        ):
            raise PoolError("invalid_page", "页面批次必须包含 1—40 个页面位置。")
        references = tuple(slot.reference for slot in self.slots)
        if len({ref.pool_id for ref in references}) != 1 or len({ref.page_number for ref in references}) != len(references):
            raise PoolError("invalid_page", "页面批次包含不同作品池或重复页位。")
