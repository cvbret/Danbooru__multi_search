"""Retry-After parsing and bounded retry delay calculation."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


RETRYABLE_HTTP_STATUSES = frozenset({429, 503})


def parse_retry_after(
    value: str | None,
    *,
    now_utc: datetime,
) -> float | None:
    """Parse RFC delay-seconds or an HTTP date without changing server delay."""

    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None

    if normalized.isdigit():
        return float(normalized)

    try:
        parsed = parsedate_to_datetime(normalized)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    normalized_now = now_utc
    if normalized_now.tzinfo is None:
        normalized_now = normalized_now.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - normalized_now).total_seconds())


def bounded_retry_delay(
    retry_after: str | None,
    *,
    retry_number: int,
    now_utc: datetime,
    base_backoff_seconds: float,
    maximum_delay_seconds: float,
) -> float:
    parsed = parse_retry_after(
        retry_after,
        now_utc=now_utc,
    )
    if parsed is not None:
        return parsed
    fallback = base_backoff_seconds * (2 ** max(0, retry_number - 1))
    return min(fallback, maximum_delay_seconds)
