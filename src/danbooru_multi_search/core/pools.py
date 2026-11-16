"""Pool metadata and stable page positions, independent of Qt and storage."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re

# https://github.com/danbooru/danbooru/blob/master/app/models/pool.rb
MAX_POOL_POSTS = 25000
MAX_POOL_DESCRIPTION_LENGTH = 20000
MAX_POOL_LIST_LIMIT = 100
# Local request bounds; these are not claims about the server's maximum page.
MAX_POOL_LIST_PAGE = 1000
DEFAULT_POOL_LIST_LIMIT = 20


class PoolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PoolCategory(StrEnum):
    SERIES = "series"
    COLLECTION = "collection"


def pool_identifier(value: int, label: str = "作品池") -> int:
    """Validate an API identifier without coercing strings or booleans."""
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise PoolError("invalid_identifier", f"{label}编号必须是有效正整数。")
    return value


def pool_pagination(page: int, limit: int) -> None:
    """Validate one explicitly requested, bounded membership page."""
    if type(page) is not int or not 1 <= page <= MAX_POOL_LIST_PAGE:
        raise PoolError("invalid_pagination", f"作品池列表页码必须在 1—{MAX_POOL_LIST_PAGE} 之间。")
    if type(limit) is not int or not 1 <= limit <= MAX_POOL_LIST_LIMIT:
        raise PoolError("invalid_pagination", f"作品池列表每页数量必须在 1—{MAX_POOL_LIST_LIMIT} 之间。")


@dataclass(frozen=True, slots=True)
class PoolPageReference:
    pool_id: int
    page_number: int
    post_id: int

    def __post_init__(self) -> None:
        pool_identifier(self.pool_id)
        pool_identifier(self.post_id, "作品")
        if (
            type(self.page_number) is not int
            or not 1 <= self.page_number <= MAX_POOL_POSTS
        ):
            raise PoolError("invalid_page", "作品池页码无效。")


@dataclass(frozen=True, slots=True)
class DanbooruPool:
    id: int
    name: str
    category: PoolCategory
    post_ids: tuple[int, ...]
    description: str = ""
    is_deleted: bool = False

    def __post_init__(self) -> None:
        pool_identifier(self.id)
        if not isinstance(self.name, str) or not self.name.strip():
            raise PoolError("invalid_pool", "作品池名称不能为空。")
        if not isinstance(self.category, PoolCategory):
            raise PoolError("invalid_category", "作品池分类必须是系列或主题。")
        if not isinstance(self.post_ids, tuple) or len(self.post_ids) > MAX_POOL_POSTS:
            raise PoolError(
                "invalid_pool", f"作品池编号列表必须是有序元组，且不超过 {MAX_POOL_POSTS} 项。"
            )
        for identifier in self.post_ids:
            pool_identifier(identifier, "作品")
        if (
            not isinstance(self.description, str)
            or len(self.description) > MAX_POOL_DESCRIPTION_LENGTH
        ):
            raise PoolError("invalid_pool", "作品池说明过长或不是文本。")
        if type(self.is_deleted) is not bool:
            raise PoolError("invalid_pool", "作品池删除状态无效。")

    @property
    def post_count(self) -> int:
        return len(self.post_ids)

    @property
    def pages(self) -> tuple[PoolPageReference, ...]:
        # A missing, unavailable or repeated post still owns its original page.
        return tuple(
            PoolPageReference(self.id, index, identifier)
            for index, identifier in enumerate(self.post_ids, 1)
        )

    def page_at(self, page_number: int) -> PoolPageReference | None:
        """Return the original 1-based position, including repeated IDs."""
        if type(page_number) is not int or page_number < 1:
            raise PoolError("invalid_page", "作品池页码必须是正整数。")
        if page_number > len(self.post_ids):
            return None
        return PoolPageReference(self.id, page_number, self.post_ids[page_number - 1])


@dataclass(frozen=True, slots=True)
class PoolMembershipPage:
    post_id: int
    page: int
    limit: int
    pools: tuple[DanbooruPool, ...]

    def __post_init__(self) -> None:
        pool_identifier(self.post_id, "作品")
        pool_pagination(self.page, self.limit)
        if (
            not isinstance(self.pools, tuple)
            or len(self.pools) > self.limit
            or not all(isinstance(pool, DanbooruPool) for pool in self.pools)
        ):
            raise PoolError("invalid_membership", "所属作品池响应超过本页范围或内容无效。")
        if any(self.post_id not in pool.post_ids or pool.is_deleted for pool in self.pools):
            raise PoolError("invalid_membership", "所属作品池响应包含不属于该作品或已删除的作品池。")


class PoolRequestKind(StrEnum):
    POOL = "pool"
    MEMBERSHIPS = "memberships"


class PoolRequestState(StrEnum):
    IDLE = "idle"
    WAITING = "waiting"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class PoolRequestIdentity:
    request_id: str
    kind: PoolRequestKind
    target_id: int
    page: int = 1
    limit: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.request_id, str)
            or re.fullmatch(r"[a-f0-9]{32}", self.request_id) is None
        ):
            raise PoolError("invalid_request", "作品池请求身份无效。")
        if not isinstance(self.kind, PoolRequestKind):
            raise PoolError("invalid_request", "作品池请求种类无效。")
        pool_identifier(self.target_id)
        pool_pagination(self.page, self.limit)
        if self.kind is PoolRequestKind.POOL and (self.page != 1 or self.limit != 1):
            raise PoolError("invalid_request", "单个作品池请求不能使用列表分页。")


@dataclass(frozen=True, slots=True)
class PoolRequestOutcome:
    identity: PoolRequestIdentity
    state: PoolRequestState
    pool: DanbooruPool | None = None
    memberships: PoolMembershipPage | None = None
    message: str = ""
    error_code: str = ""
    http_status: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.identity, PoolRequestIdentity)
            or not isinstance(self.state, PoolRequestState)
            or self.state not in {
                PoolRequestState.SUCCESS,
                PoolRequestState.FAILED,
                PoolRequestState.CANCELLED,
            }
        ):
            raise PoolError("invalid_outcome", "作品池请求结果身份或状态无效。")
        if not isinstance(self.error_code, str):
            raise PoolError("invalid_outcome", "作品池错误代码必须是文本。")
        if self.state is PoolRequestState.SUCCESS:
            if self.identity.kind is PoolRequestKind.POOL:
                valid = (
                    isinstance(self.pool, DanbooruPool)
                    and self.pool.id == self.identity.target_id
                    and self.memberships is None
                )
            else:
                valid = (
                    isinstance(self.memberships, PoolMembershipPage)
                    and self.pool is None
                    and (
                        self.memberships.post_id,
                        self.memberships.page,
                        self.memberships.limit,
                    ) == (
                        self.identity.target_id,
                        self.identity.page,
                        self.identity.limit,
                    )
                )
            if not valid or self.error_code:
                raise PoolError("invalid_outcome", "作品池响应与请求身份不一致。")
        elif self.pool is not None or self.memberships is not None or not self.error_code:
            raise PoolError("invalid_outcome", "作品池失败结果无效。")
        if not isinstance(self.message, str) or (
            self.http_status is not None
            and (type(self.http_status) is not int or not 100 <= self.http_status <= 599)
        ):
            raise PoolError("invalid_outcome", "作品池错误说明或 HTTP 状态无效。")

    @property
    def succeeded(self) -> bool:
        return self.state is PoolRequestState.SUCCESS
