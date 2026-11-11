"""Shared query execution data without controller or transport dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math

from danbooru_multi_search.danbooru.models import DanbooruPost


DEFAULT_EXECUTION_PAGE_SIZE = 200
DEFAULT_CANDIDATE_LIMIT = 1_000
DEFAULT_BRANCH_PAGE_LIMIT = 10
DEFAULT_ACTIVE_TIME_LIMIT_SECONDS = 60.0


class QueryExecutionState(StrEnum):
    IDLE = "idle"
    WAITING = "waiting"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    COMPLETE = "complete"
    PARTIAL_CANCELLED = "partial_cancelled"
    PARTIAL_LIMITED = "partial_limited"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"


ACTIVE_EXECUTION_STATES = frozenset(
    {
        QueryExecutionState.WAITING,
        QueryExecutionState.RUNNING,
        QueryExecutionState.RETRY_WAIT,
    }
)


class QueryLimitReason(StrEnum):
    CANDIDATE_LIMIT = "candidate_limit"
    PAGE_LIMIT = "page_limit"
    TIME_LIMIT = "time_limit"


class QueryExecutionErrorCode(StrEnum):
    AUTHENTICATION = "authentication"
    RATE_LIMITED = "rate_limited"
    HTTP = "http"
    TIMEOUT = "timeout"
    NETWORK = "network"
    INVALID_RESPONSE = "invalid_response"
    REQUEST_START = "request_start"
    QUEUE_REJECTED = "queue_rejected"


@dataclass(frozen=True, slots=True)
class QueryExecutionLimits:
    """Validated bounds shared by query definitions and their execution."""

    page_size: int = DEFAULT_EXECUTION_PAGE_SIZE
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT
    branch_page_limit: int = DEFAULT_BRANCH_PAGE_LIMIT
    active_time_limit_seconds: float = DEFAULT_ACTIVE_TIME_LIMIT_SECONDS

    def __post_init__(self) -> None:
        _positive_bounded_integer(
            self.page_size,
            DEFAULT_EXECUTION_PAGE_SIZE,
            "page_size",
        )
        _positive_bounded_integer(
            self.candidate_limit,
            DEFAULT_CANDIDATE_LIMIT,
            "candidate_limit",
        )
        _positive_bounded_integer(
            self.branch_page_limit,
            DEFAULT_BRANCH_PAGE_LIMIT,
            "branch_page_limit",
        )
        value = self.active_time_limit_seconds
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value <= 0
            or value > DEFAULT_ACTIVE_TIME_LIMIT_SECONDS
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            raise ValueError(
                "active_time_limit_seconds must be greater than zero and at "
                f"most {DEFAULT_ACTIVE_TIME_LIMIT_SECONDS:g}"
            )


@dataclass(frozen=True, slots=True)
class QueryBranchPosition:
    branch_index: int
    page: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.branch_index, int)
            or isinstance(self.branch_index, bool)
            or self.branch_index < 0
        ):
            raise ValueError("branch_index must be a non-negative integer")
        if (
            not isinstance(self.page, int)
            or isinstance(self.page, bool)
            or self.page < 1
        ):
            raise ValueError("page must be a positive integer")


@dataclass(frozen=True, slots=True)
class QueryBranchProgress:
    branch_index: int
    next_page: int | None
    successful_pages: int
    natural_end: bool
    page_limited: bool


@dataclass(frozen=True, slots=True)
class QueryExecutionCounts:
    remote_rows: int = 0
    unique_candidates: int = 0
    local_matches: int = 0


@dataclass(frozen=True, slots=True)
class QueryExecutionFailure:
    position: QueryBranchPosition
    api_tags: str
    error_code: QueryExecutionErrorCode
    http_status: int | None
    message: str


@dataclass(frozen=True, slots=True)
class QueryExecutionSnapshot:
    """One immutable execution result consumed by processing and presentation."""

    execution_id: int
    state: QueryExecutionState
    counts: QueryExecutionCounts
    posts: tuple[DanbooruPost, ...]
    branch_progress: tuple[QueryBranchProgress, ...]
    limit_reason: QueryLimitReason | None = None
    failure: QueryExecutionFailure | None = None


@dataclass(frozen=True, slots=True)
class QueryExecutionBound:
    execution_id: int
    binding_token: object | None = None


@dataclass(frozen=True, slots=True)
class QueryExecutionAccepted:
    execution_id: int
    position: QueryBranchPosition
    task_id: str


@dataclass(frozen=True, slots=True)
class GalleryResultSummary:
    """Persistable result counts and terminal diagnostics for query history."""

    state: QueryExecutionState
    remote_rows: int
    unique_candidates: int
    local_matches: int
    visible_count: int
    limit_reason: QueryLimitReason | None
    failure_error_code: QueryExecutionErrorCode | None


def _positive_bounded_integer(value: int, maximum: int, name: str) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
        or value > maximum
    ):
        raise ValueError(f"{name} must be between 1 and {maximum}")
