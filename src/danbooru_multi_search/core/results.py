"""Immutable operation results shared without platform or GUI dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar


@dataclass(frozen=True, slots=True)
class ServiceAvailability:
    """Describe whether a platform service can currently perform operations."""

    available: bool
    code: str | None = None
    message: str | None = None

    @classmethod
    def unavailable(cls, code: str, message: str) -> ServiceAvailability:
        return cls(available=False, code=code, message=message)


@dataclass(frozen=True, slots=True)
class OperationResult:
    """Result for a platform operation that does not return a value."""

    succeeded: bool
    code: str | None = None
    message: str | None = None

    @classmethod
    def unavailable(cls, code: str, message: str) -> OperationResult:
        return cls(succeeded=False, code=code, message=message)


ResultValue = TypeVar("ResultValue")


@dataclass(frozen=True, slots=True)
class ValueResult(Generic[ResultValue]):
    """Result for a platform operation that may return a typed value."""

    succeeded: bool
    value: ResultValue | None = None
    code: str | None = None
    message: str | None = None

    @classmethod
    def unavailable(cls, code: str, message: str) -> ValueResult[ResultValue]:
        return cls(succeeded=False, code=code, message=message)
