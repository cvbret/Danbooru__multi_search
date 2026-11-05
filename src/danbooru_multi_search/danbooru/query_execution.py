"""Serial, bounded execution of immutable multi-branch query plans."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import math
from typing import Protocol

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest

from danbooru_multi_search.danbooru.client import (
    DEFAULT_TIMEOUT_MS,
    QtPostsRequestAttempt,
    RequestErrorKind,
    build_posts_request,
    select_preferred_tls_backend,
)
from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.danbooru.parsing import parse_complete_posts_response
from danbooru_multi_search.danbooru.query_evaluator import evaluate_post_query
from danbooru_multi_search.danbooru.query_planning import (
    QueryPlan,
    RemoteOrder,
    expand_query_to_dnf,
)
from danbooru_multi_search.tasks import (
    BoundedTaskQueue,
    Clock,
    QtScheduler,
    ScheduledCall,
    Scheduler,
    SystemClock,
    TaskAttemptResult,
    TaskFinalResult,
    TaskKind,
    TaskQueueConfig,
    TaskQueueFullError,
    TaskQueueShutdownError,
    TaskRecord,
    TaskState,
)


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


class Abortable(Protocol):
    def abort(self) -> None: ...


PageAttemptFactory = Callable[
    [QNetworkRequest, int, Callable[[TaskAttemptResult], None]],
    Abortable | None,
]


@dataclass(slots=True)
class _BranchState:
    next_page: int | None = 1
    successful_pages: int = 0
    natural_end: bool = False
    page_limited: bool = False


@dataclass(slots=True)
class _PageRequestIdentity:
    request_sequence: int
    position: QueryBranchPosition
    task_id: str | None = None
    completion_handled: bool = False


@dataclass(slots=True)
class _ExecutionSession:
    execution_id: int
    plan: QueryPlan
    limits: QueryExecutionLimits
    branches: list[_BranchState]
    pending_branches: deque[int]
    binding_token: object | None = None
    state: QueryExecutionState = QueryExecutionState.RUNNING
    counts: QueryExecutionCounts = QueryExecutionCounts()
    candidates: dict[int, DanbooruPost] | None = None
    matches: dict[int, DanbooruPost] | None = None
    limit_reason: QueryLimitReason | None = None
    failure: QueryExecutionFailure | None = None
    active_request: _PageRequestIdentity | None = None
    active_task_id: str | None = None
    request_sequence: int = 0
    control_generation: int = 0
    elapsed_active_seconds: float = 0.0
    active_since: float | None = None
    deadline_call: ScheduledCall | None = None
    deadline_generation: int = 0
    deadline_pending: bool = False
    continue_call: ScheduledCall | None = None
    continue_generation: int = 0
    continue_pending: bool = False
    first_page_accepted: bool = False
    acceptance_in_progress: bool = False

    def __post_init__(self) -> None:
        if self.candidates is None:
            self.candidates = {}
        if self.matches is None:
            self.matches = {}


class DanbooruMultiBranchExecutor(QObject):
    """Execute one query plan at a time with deterministic page rotation."""

    executionBound = Signal(object)
    firstPageAccepted = Signal(object)
    snapshotChanged = Signal(object)

    def __init__(
        self,
        base_url: str,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        network_manager: QNetworkAccessManager | None = None,
        task_queue: BoundedTaskQueue | None = None,
        queue_config: TaskQueueConfig | None = None,
        clock: Clock | None = None,
        scheduler: Scheduler | None = None,
        attempt_factory: PageAttemptFactory | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._base_url = base_url
        self._timeout_ms = timeout_ms
        self._clock = clock or SystemClock()
        self._qt_scheduler: QtScheduler | None = None
        if scheduler is None:
            self._qt_scheduler = QtScheduler(self)
            scheduler = self._qt_scheduler
        self._scheduler = scheduler
        self._owns_queue = task_queue is None
        if task_queue is None:
            task_queue = BoundedTaskQueue(
                queue_config or TaskQueueConfig(),
                clock=self._clock,
                scheduler=self._scheduler,
            )
        self._task_queue = task_queue
        self._network_manager = network_manager
        if attempt_factory is None:
            select_preferred_tls_backend()
            self._network_manager = network_manager or QNetworkAccessManager(self)
            self._attempt_factory = self._start_qt_attempt
        else:
            self._attempt_factory = attempt_factory
        self._execution_counter = 0
        self._session: _ExecutionSession | None = None
        self._closed = False
        self._snapshot = QueryExecutionSnapshot(
            execution_id=0,
            state=QueryExecutionState.IDLE,
            counts=QueryExecutionCounts(),
            posts=(),
            branch_progress=(),
        )

    @property
    def snapshot(self) -> QueryExecutionSnapshot:
        return self._snapshot

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def owns_queue(self) -> bool:
        return self._owns_queue

    @property
    def current_execution_id(self) -> int | None:
        session = self._session
        return session.execution_id if session is not None else None

    def is_execution_active(self, execution_id: int) -> bool:
        session = self._session
        return (
            session is not None
            and session.execution_id == execution_id
            and self._is_active_session(session)
        )

    def start(
        self,
        plan: QueryPlan,
        limits: QueryExecutionLimits | None = None,
        *,
        binding_token: object | None = None,
    ) -> int:
        """Start a fresh execution after validating all synchronous inputs."""

        if self._closed:
            raise RuntimeError("The query executor has been shut down.")
        resolved_limits = (
            QueryExecutionLimits() if limits is None else limits
        )
        self._validate_start(plan, resolved_limits)

        old_session = self._session
        self._execution_counter += 1
        session = _ExecutionSession(
            execution_id=self._execution_counter,
            plan=plan,
            limits=resolved_limits,
            branches=[_BranchState() for _ in plan.remote_branches],
            pending_branches=deque(range(len(plan.remote_branches))),
            binding_token=binding_token,
        )
        self._session = session
        if old_session is not None:
            self._abandon_session(old_session)

        self.executionBound.emit(
            QueryExecutionBound(session.execution_id, session.binding_token)
        )
        if not self._is_active_session(session):
            return session.execution_id
        self._resume_active_time(session)
        if self._is_current_session(session):
            self._submit_next_page(session)
        return session.execution_id

    def cancel(self) -> bool:
        """Cancel an active execution and retain its accepted partial results."""

        session = self._session
        if session is None or session.state not in ACTIVE_EXECUTION_STATES:
            return False
        session.control_generation += 1
        task_id = self._invalidate_active_request(session)
        self._cancel_continue(session)
        self._stop_active_time(session)
        session.state = QueryExecutionState.PARTIAL_CANCELLED
        session.limit_reason = None
        session.failure = None
        if task_id is not None:
            self._task_queue.cancel(task_id, "用户取消了复杂查询。")
        if not session.acceptance_in_progress:
            self._publish(session)
        return True

    def retry_failed_page(self) -> bool:
        """Retry the exact failed branch page without clearing accepted data."""

        if self._closed:
            return False
        session = self._session
        if (
            session is None
            or session.state
            not in {
                QueryExecutionState.FAILED,
                QueryExecutionState.PARTIAL_FAILED,
            }
            or session.failure is None
        ):
            return False

        position = session.failure.position
        session.control_generation += 1
        session.failure = None
        session.limit_reason = None
        session.state = QueryExecutionState.RUNNING
        self._resume_active_time(session)
        if self._is_current_session(session):
            self._submit_page(session, position)
        return True

    def shutdown(self) -> None:
        """Stop owned work once while leaving a shared queue available."""

        if self._closed:
            return
        self._closed = True
        session = self._session
        if session is not None and session.state in ACTIVE_EXECUTION_STATES:
            session.control_generation += 1
            task_id = self._invalidate_active_request(session)
            self._cancel_continue(session)
            self._stop_active_time(session)
            session.state = QueryExecutionState.PARTIAL_CANCELLED
            session.limit_reason = None
            session.failure = None
            if task_id is not None:
                self._task_queue.cancel(
                    task_id,
                    "Application is shutting down.",
                )
            if not session.acceptance_in_progress:
                self._publish(session, allow_closed=True)
        elif session is not None:
            self._cancel_continue(session)
            self._cancel_deadline(session)
        if self._owns_queue:
            self._task_queue.shutdown()

    def _start_qt_attempt(
        self,
        request: QNetworkRequest,
        attempt_number: int,
        completion: Callable[[TaskAttemptResult], None],
    ) -> QtPostsRequestAttempt:
        del attempt_number
        manager = self._network_manager
        if manager is None:
            raise RuntimeError("The query executor has no network manager.")
        return QtPostsRequestAttempt(
            manager,
            request,
            completion,
            self,
            parser=parse_complete_posts_response,
        )

    @staticmethod
    def _validate_start(plan: QueryPlan, limits: QueryExecutionLimits) -> None:
        if not isinstance(plan, QueryPlan):
            raise TypeError("plan must be a QueryPlan")
        if not isinstance(limits, QueryExecutionLimits):
            raise TypeError("limits must be QueryExecutionLimits")
        if not plan.remote_branches:
            raise ValueError("The query plan must contain at least one remote branch.")
        expected_dnf = (
            ()
            if plan.expression is None
            else expand_query_to_dnf(plan.expression)
        )
        if plan.dnf_branches != expected_dnf:
            raise ValueError("The query plan DNF does not match its expression.")
        if plan.remote_order == RemoteOrder.RANK:
            if len(plan.remote_branches) != 1:
                raise ValueError("A rank plan must contain one remote branch.")
            remote = plan.remote_branches[0]
            if (
                remote.remote_order != RemoteOrder.RANK
                or remote.local_branch is not None
                or remote.rating != plan.rating
            ):
                raise ValueError("The rank remote branch is inconsistent with its plan.")
            if plan.expression is None:
                if remote.candidate_tags:
                    raise ValueError("A tagless rank plan cannot have a candidate tag.")
            else:
                common = set(plan.dnf_branches[0].positive_tags)
                for branch in plan.dnf_branches[1:]:
                    common.intersection_update(branch.positive_tags)
                if (
                    len(remote.candidate_tags) != 1
                    or remote.candidate_tags[0] not in common
                ):
                    raise ValueError(
                        "A rank candidate must be common to every DNF branch."
                    )
        elif plan.expression is None:
            if not plan.is_latest:
                raise ValueError("A latest plan must use one empty remote branch.")
        elif len(plan.remote_branches) != len(plan.dnf_branches):
            raise ValueError(
                "An ordinary plan must contain one remote branch per DNF branch."
            )
        else:
            for remote, local in zip(
                plan.remote_branches,
                plan.dnf_branches,
                strict=True,
            ):
                if (
                    remote.remote_order is not None
                    or remote.local_branch != local
                    or remote.rating != plan.rating
                    or not remote.candidate_tags
                    or not set(remote.candidate_tags) <= local.positive_tags
                ):
                    raise ValueError(
                        "An ordinary remote branch is inconsistent with its plan."
                    )

    def _submit_next_page(self, session: _ExecutionSession) -> None:
        if not self._is_active_session(session):
            return
        if self._active_time_reached(session, self._clock.monotonic()):
            self._terminate_limited(session, QueryLimitReason.TIME_LIMIT)
            return
        if not session.pending_branches:
            self._finish_after_all_branches(session)
            return
        branch_index = session.pending_branches.popleft()
        next_page = session.branches[branch_index].next_page
        if next_page is None:
            self._schedule_next_page(session)
            return
        self._submit_page(
            session,
            QueryBranchPosition(branch_index, next_page),
        )

    def _submit_page(
        self,
        session: _ExecutionSession,
        position: QueryBranchPosition,
    ) -> None:
        if not self._is_active_session(session):
            return
        if self._active_time_reached(session, self._clock.monotonic()):
            self._terminate_limited(session, QueryLimitReason.TIME_LIMIT)
            return

        remote_branch = session.plan.remote_branches[position.branch_index]
        try:
            request = build_posts_request(
                self._base_url,
                remote_branch.api_tags,
                page=position.page,
                limit=session.limits.page_size,
                timeout_ms=self._timeout_ms,
            )
        except (TypeError, ValueError) as exc:
            if self._active_time_reached(session, self._clock.monotonic()):
                self._terminate_limited(session, QueryLimitReason.TIME_LIMIT)
                return
            self._record_failure(
                session,
                position,
                QueryExecutionErrorCode.REQUEST_START,
                str(exc),
            )
            return

        session.request_sequence += 1
        identity = _PageRequestIdentity(
            request_sequence=session.request_sequence,
            position=position,
        )
        session.active_request = identity
        completion_is_live = False
        deferred_results: list[TaskFinalResult] = []

        def starter(
            attempt_number: int,
            completion: Callable[[TaskAttemptResult], None],
        ) -> Abortable | None:
            return self._attempt_factory(request, attempt_number, completion)

        def completed(result: TaskFinalResult) -> None:
            if not completion_is_live:
                deferred_results.append(result)
                return
            self._task_completed(session, identity, result)

        try:
            record = self._task_queue.submit(
                TaskKind.METADATA,
                starter,
                completed,
            )
        except (TaskQueueFullError, TaskQueueShutdownError) as exc:
            if self._is_identity_current(session, identity, require_task=False):
                session.active_request = None
                if self._active_time_reached(
                    session,
                    self._clock.monotonic(),
                ):
                    self._terminate_limited(
                        session,
                        QueryLimitReason.TIME_LIMIT,
                    )
                    return
                self._record_failure(
                    session,
                    position,
                    QueryExecutionErrorCode.QUEUE_REJECTED,
                    str(exc),
                )
            return

        if not self._is_identity_current(session, identity, require_task=False):
            if not record.terminal:
                self._task_queue.cancel(
                    record.identifier,
                    "A newer query execution replaced this page.",
                )
            return

        identity.task_id = record.identifier
        session.active_task_id = record.identifier
        record.add_listener(
            lambda current: self._task_state_changed(
                session,
                identity,
                current,
            )
        )
        if not session.first_page_accepted:
            session.first_page_accepted = True
            session.acceptance_in_progress = True
            try:
                self.firstPageAccepted.emit(
                    QueryExecutionAccepted(
                        execution_id=session.execution_id,
                        position=identity.position,
                        task_id=record.identifier,
                    )
                )
            finally:
                session.acceptance_in_progress = False
            if not self._is_identity_current(session, identity):
                if not record.terminal:
                    self._task_queue.cancel(
                        record.identifier,
                        "The execution changed during its acceptance event.",
                    )
                return
        completion_is_live = True

        if record.terminal:
            for result in deferred_results:
                self._task_completed(session, identity, result)
                if not self._is_current_session(session):
                    break
            return

        self._task_state_changed(session, identity, record)
        if not self._is_identity_current(session, identity):
            if not record.terminal:
                self._task_queue.cancel(
                    record.identifier,
                    "The page became stale while publishing its state.",
                )
            return
        for result in deferred_results:
            self._task_completed(session, identity, result)
            if not self._is_current_session(session):
                break

    def _task_state_changed(
        self,
        session: _ExecutionSession,
        identity: _PageRequestIdentity,
        record: TaskRecord,
    ) -> None:
        if (
            record.terminal
            or not self._is_identity_current(session, identity)
            or record.identifier != identity.task_id
        ):
            return
        state = {
            TaskState.WAITING: QueryExecutionState.WAITING,
            TaskState.RUNNING: QueryExecutionState.RUNNING,
            TaskState.RETRY_WAIT: QueryExecutionState.RETRY_WAIT,
        }.get(record.state)
        if state is None:
            return
        session.state = state
        control_generation = session.control_generation
        self._publish(session)
        if (
            session.control_generation != control_generation
            or not self._is_identity_current(session, identity)
        ):
            return

    def _task_completed(
        self,
        session: _ExecutionSession,
        identity: _PageRequestIdentity,
        result: TaskFinalResult,
    ) -> None:
        if (
            not self._is_identity_current(session, identity)
            or result.task_id != identity.task_id
            or identity.completion_handled
        ):
            return
        identity.completion_handled = True
        session.active_request = None
        session.active_task_id = None
        callback_started_at = self._clock.monotonic()
        if self._active_time_reached(session, callback_started_at):
            self._terminate_limited(session, QueryLimitReason.TIME_LIMIT)
            return

        if result.state == TaskState.CANCELLED:
            self._record_failure(
                session,
                identity.position,
                QueryExecutionErrorCode.QUEUE_REJECTED,
                result.error or "共享任务队列取消了当前页面。",
                http_status=result.http_status,
            )
            return
        if result.state == TaskState.FAILED:
            error_code = self._classify_failure(result)
            self._record_failure(
                session,
                identity.position,
                error_code,
                result.error or "Danbooru 页面请求失败。",
                http_status=result.http_status,
            )
            return
        if result.state != TaskState.SUCCEEDED:
            self._record_failure(
                session,
                identity.position,
                QueryExecutionErrorCode.INVALID_RESPONSE,
                "页面任务返回了未知终止状态。",
                http_status=result.http_status,
            )
            return

        posts = result.value
        if not isinstance(posts, tuple) or not all(
            isinstance(post, DanbooruPost) for post in posts
        ):
            self._record_failure(
                session,
                identity.position,
                QueryExecutionErrorCode.INVALID_RESPONSE,
                "Danbooru 页面返回了无法识别的作品列表。",
                http_status=result.http_status,
            )
            return
        if len(posts) > session.limits.page_size:
            self._record_failure(
                session,
                identity.position,
                QueryExecutionErrorCode.INVALID_RESPONSE,
                "Danbooru 页面返回的作品数量超过请求上限。",
                http_status=result.http_status,
            )
            return
        self._accept_page(session, identity.position, posts)

    def _accept_page(
        self,
        session: _ExecutionSession,
        position: QueryBranchPosition,
        posts: tuple[DanbooruPost, ...],
    ) -> None:
        if not self._is_active_session(session):
            return
        candidates = dict(session.candidates or {})
        matches = dict(session.matches or {})
        new_candidates = 0
        new_matches = 0
        candidate_overflow = False
        try:
            for post in posts:
                if post.id in candidates:
                    continue
                if len(candidates) >= session.limits.candidate_limit:
                    candidate_overflow = True
                    break
                candidates[post.id] = post
                new_candidates += 1
                matched = (
                    True
                    if session.plan.expression is None
                    else evaluate_post_query(session.plan.expression, post)
                )
                if matched:
                    matches[post.id] = post
                    new_matches += 1
        except (TypeError, ValueError) as exc:
            self._record_failure(
                session,
                position,
                QueryExecutionErrorCode.INVALID_RESPONSE,
                f"本地查询计算无法处理页面数据：{exc}",
            )
            return

        branch = session.branches[position.branch_index]
        successful_pages = branch.successful_pages + 1
        short_page = len(posts) < session.limits.page_size
        next_page: int | None
        natural_end = False
        page_limited = False
        if short_page:
            next_page = None
            natural_end = True
        elif successful_pages >= session.limits.branch_page_limit:
            next_page = None
            page_limited = True
        else:
            next_page = position.page + 1

        session.candidates = candidates
        session.matches = matches
        session.counts = QueryExecutionCounts(
            remote_rows=session.counts.remote_rows + len(posts),
            unique_candidates=session.counts.unique_candidates + new_candidates,
            local_matches=session.counts.local_matches + new_matches,
        )
        branch.successful_pages = successful_pages
        branch.next_page = next_page
        branch.natural_end = natural_end
        branch.page_limited = page_limited
        if next_page is not None:
            session.pending_branches.append(position.branch_index)
        session.failure = None
        session.limit_reason = None

        if self._active_time_reached(session, self._clock.monotonic()):
            self._terminate_limited(session, QueryLimitReason.TIME_LIMIT)
            return
        all_natural = all(item.natural_end for item in session.branches)
        candidate_limit_reached = (
            candidate_overflow
            or (
                session.counts.unique_candidates
                >= session.limits.candidate_limit
                and not all_natural
            )
        )
        if candidate_limit_reached:
            self._terminate_limited(session, QueryLimitReason.CANDIDATE_LIMIT)
            return
        if not session.pending_branches:
            self._finish_after_all_branches(session)
            return

        session.state = QueryExecutionState.RUNNING
        control_generation = session.control_generation
        self._publish(session)
        if (
            not self._is_active_session(session)
            or session.control_generation != control_generation
        ):
            return
        self._schedule_next_page(session)

    def _finish_after_all_branches(self, session: _ExecutionSession) -> None:
        if not self._is_active_session(session):
            return
        if any(branch.page_limited for branch in session.branches):
            self._terminate_limited(session, QueryLimitReason.PAGE_LIMIT)
            return
        self._stop_active_time(session)
        session.control_generation += 1
        session.state = QueryExecutionState.COMPLETE
        session.limit_reason = None
        session.failure = None
        self._publish(session)

    def _terminate_limited(
        self,
        session: _ExecutionSession,
        reason: QueryLimitReason,
    ) -> None:
        if not self._is_active_session(session):
            return
        session.control_generation += 1
        task_id = self._invalidate_active_request(session)
        self._cancel_continue(session)
        self._stop_active_time(session)
        session.state = QueryExecutionState.PARTIAL_LIMITED
        session.limit_reason = reason
        session.failure = None
        if task_id is not None:
            self._task_queue.cancel(
                task_id,
                f"Query stopped at {reason.value}.",
            )
        self._publish(session)

    def _record_failure(
        self,
        session: _ExecutionSession,
        position: QueryBranchPosition,
        error_code: QueryExecutionErrorCode,
        message: str,
        *,
        http_status: int | None = None,
    ) -> None:
        if not self._is_active_session(session):
            return
        session.control_generation += 1
        self._invalidate_active_request(session)
        self._cancel_continue(session)
        self._pause_active_time(session)
        session.state = (
            QueryExecutionState.PARTIAL_FAILED
            if session.counts.local_matches > 0
            else QueryExecutionState.FAILED
        )
        session.limit_reason = None
        session.failure = QueryExecutionFailure(
            position=position,
            api_tags=session.plan.remote_branches[position.branch_index].api_tags,
            error_code=error_code,
            http_status=http_status,
            message=message,
        )
        self._publish(session)

    def _schedule_next_page(self, session: _ExecutionSession) -> None:
        if not self._is_active_session(session):
            return
        self._cancel_continue(session)
        session.continue_generation += 1
        generation = session.continue_generation
        session.continue_pending = True

        def continue_scan() -> None:
            if (
                not self._is_active_session(session)
                or generation != session.continue_generation
            ):
                return
            session.continue_pending = False
            session.continue_call = None
            self._submit_next_page(session)

        call = self._scheduler.call_later(0, continue_scan)
        if (
            self._is_active_session(session)
            and generation == session.continue_generation
            and session.continue_pending
        ):
            session.continue_call = call
        else:
            call.cancel()

    def _resume_active_time(self, session: _ExecutionSession) -> None:
        if session.active_since is not None:
            return
        remaining = (
            session.limits.active_time_limit_seconds
            - session.elapsed_active_seconds
        )
        if remaining <= 0:
            self._terminate_limited(session, QueryLimitReason.TIME_LIMIT)
            return
        session.active_since = self._clock.monotonic()
        self._schedule_deadline(session, remaining)

    def _pause_active_time(self, session: _ExecutionSession) -> None:
        now = self._clock.monotonic()
        if session.active_since is not None:
            session.elapsed_active_seconds += max(0.0, now - session.active_since)
            session.active_since = None
        self._cancel_deadline(session)

    def _stop_active_time(self, session: _ExecutionSession) -> None:
        self._pause_active_time(session)

    def _schedule_deadline(
        self,
        session: _ExecutionSession,
        remaining_seconds: float,
    ) -> None:
        self._cancel_deadline(session)
        session.deadline_generation += 1
        generation = session.deadline_generation
        session.deadline_pending = True

        def deadline_reached() -> None:
            if (
                not self._is_active_session(session)
                or generation != session.deadline_generation
            ):
                return
            session.deadline_pending = False
            session.deadline_call = None
            self._terminate_limited(session, QueryLimitReason.TIME_LIMIT)

        call = self._scheduler.call_later(
            remaining_seconds,
            deadline_reached,
        )
        if (
            self._is_active_session(session)
            and generation == session.deadline_generation
            and session.deadline_pending
        ):
            session.deadline_call = call
        else:
            call.cancel()

    def _cancel_deadline(self, session: _ExecutionSession) -> None:
        session.deadline_generation += 1
        session.deadline_pending = False
        if session.deadline_call is not None:
            session.deadline_call.cancel()
        session.deadline_call = None

    def _cancel_continue(self, session: _ExecutionSession) -> None:
        session.continue_generation += 1
        session.continue_pending = False
        if session.continue_call is not None:
            session.continue_call.cancel()
        session.continue_call = None

    def _active_time_reached(
        self,
        session: _ExecutionSession,
        moment: float,
    ) -> bool:
        elapsed = session.elapsed_active_seconds
        if session.active_since is not None:
            elapsed += max(0.0, moment - session.active_since)
        return elapsed >= session.limits.active_time_limit_seconds

    def _invalidate_active_request(
        self,
        session: _ExecutionSession,
    ) -> str | None:
        task_id = session.active_task_id
        if session.active_request is not None:
            session.active_request.completion_handled = True
        session.active_request = None
        session.active_task_id = None
        return task_id

    def _abandon_session(self, session: _ExecutionSession) -> None:
        session.control_generation += 1
        task_id = self._invalidate_active_request(session)
        self._cancel_continue(session)
        self._stop_active_time(session)
        if task_id is not None:
            self._task_queue.cancel(
                task_id,
                "A newer query execution replaced this one.",
            )

    def _is_current_session(self, session: _ExecutionSession) -> bool:
        return self._session is session

    def _is_active_session(self, session: _ExecutionSession) -> bool:
        return (
            self._session is session
            and not self._closed
            and session.state in ACTIVE_EXECUTION_STATES
        )

    def _is_identity_current(
        self,
        session: _ExecutionSession,
        identity: _PageRequestIdentity,
        *,
        require_task: bool = True,
    ) -> bool:
        return (
            self._is_active_session(session)
            and session.active_request is identity
            and not identity.completion_handled
            and identity.request_sequence == session.request_sequence
            and 0 <= identity.position.branch_index < len(session.branches)
            and session.branches[
                identity.position.branch_index
            ].next_page
            == identity.position.page
            and (
                not require_task
                or (
                    identity.task_id is not None
                    and session.active_task_id == identity.task_id
                )
            )
        )

    def _publish(
        self,
        session: _ExecutionSession,
        *,
        allow_closed: bool = False,
    ) -> bool:
        if self._session is not session or (self._closed and not allow_closed):
            return False
        snapshot = QueryExecutionSnapshot(
            execution_id=session.execution_id,
            state=session.state,
            counts=session.counts,
            posts=tuple((session.matches or {}).values()),
            branch_progress=tuple(
                QueryBranchProgress(
                    branch_index=index,
                    next_page=branch.next_page,
                    successful_pages=branch.successful_pages,
                    natural_end=branch.natural_end,
                    page_limited=branch.page_limited,
                )
                for index, branch in enumerate(session.branches)
            ),
            limit_reason=session.limit_reason,
            failure=session.failure,
        )
        self._snapshot = snapshot
        self.snapshotChanged.emit(snapshot)
        return self._session is session and (allow_closed or not self._closed)

    @staticmethod
    def _classify_failure(result: TaskFinalResult) -> QueryExecutionErrorCode:
        if result.http_status in {401, 403}:
            return QueryExecutionErrorCode.AUTHENTICATION
        if result.http_status == 429:
            return QueryExecutionErrorCode.RATE_LIMITED
        if result.error_code == RequestErrorKind.TIMEOUT:
            return QueryExecutionErrorCode.TIMEOUT
        if result.error_code == RequestErrorKind.NETWORK:
            return QueryExecutionErrorCode.NETWORK
        if result.error_code == RequestErrorKind.INVALID_RESPONSE:
            return QueryExecutionErrorCode.INVALID_RESPONSE
        if result.error_code == "start_error":
            return QueryExecutionErrorCode.REQUEST_START
        if result.http_status is not None:
            return QueryExecutionErrorCode.HTTP
        return QueryExecutionErrorCode.NETWORK


def _positive_bounded_integer(value: int, maximum: int, name: str) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
        or value > maximum
    ):
        raise ValueError(f"{name} must be between 1 and {maximum}")
