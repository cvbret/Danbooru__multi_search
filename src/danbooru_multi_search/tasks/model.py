"""Reusable task state and completion models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable


class TaskState(StrEnum):
    WAITING = "waiting"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskKind(StrEnum):
    METADATA = "metadata"
    MEDIA = "media"


TERMINAL_STATES = frozenset(
    {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}
)

_ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.WAITING: frozenset(
        {TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.RETRY_WAIT,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.RETRY_WAIT: frozenset(
        {TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


class InvalidTaskTransition(RuntimeError):
    pass


StateListener = Callable[["TaskRecord"], None]


@dataclass(slots=True)
class TaskRecord:
    identifier: str
    kind: TaskKind
    state: TaskState = TaskState.WAITING
    attempts: int = 0
    message: str | None = None
    _history: list[TaskState] = field(
        default_factory=lambda: [TaskState.WAITING],
        repr=False,
    )
    _listeners: list[StateListener] = field(default_factory=list, repr=False)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def history(self) -> tuple[TaskState, ...]:
        return tuple(self._history)

    def add_listener(self, listener: StateListener) -> None:
        self._listeners.append(listener)

    def transition(self, next_state: TaskState, message: str | None = None) -> None:
        if next_state not in _ALLOWED_TRANSITIONS[self.state]:
            raise InvalidTaskTransition(
                f"Task cannot transition from {self.state.value} to {next_state.value}."
            )
        self.state = next_state
        self.message = message
        self._history.append(next_state)
        for listener in tuple(self._listeners):
            listener(self)


@dataclass(frozen=True, slots=True)
class TaskAttemptResult:
    succeeded: bool
    value: Any = None
    error: str | None = None
    error_code: str | None = None
    http_status: int | None = None
    retry_after: str | None = None

    @classmethod
    def success(
        cls,
        value: Any = None,
        *,
        http_status: int | None = None,
    ) -> TaskAttemptResult:
        return cls(succeeded=True, value=value, http_status=http_status)

    @classmethod
    def failure(
        cls,
        error: str,
        *,
        error_code: str | None = None,
        http_status: int | None = None,
        retry_after: str | None = None,
    ) -> TaskAttemptResult:
        return cls(
            succeeded=False,
            error=error,
            error_code=error_code,
            http_status=http_status,
            retry_after=retry_after,
        )


@dataclass(frozen=True, slots=True)
class TaskFinalResult:
    task_id: str
    state: TaskState
    attempts: int
    value: Any = None
    error: str | None = None
    error_code: str | None = None
    http_status: int | None = None
    retry_exhausted: bool = False
