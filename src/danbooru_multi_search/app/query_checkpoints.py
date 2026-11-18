"""Synchronous query checkpoint persistence, ownership and resume coordination."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from sqlite3 import Connection
from typing import Protocol
from uuid import uuid4

from danbooru_multi_search.app.write_access import SharedWriteAccess
from danbooru_multi_search.danbooru.query_checkpoint import (
    CheckpointError, CheckpointRecord, ExecutionCheckpoint, QueryCheckpoint,
)
from danbooru_multi_search.danbooru.query_events import (
    GalleryQueryAccepted, GalleryQueryOrigin,
    GalleryResultEvent, GalleryViewDefinitionChanged,
)
from danbooru_multi_search.danbooru.query_types import (
    GalleryResultSummary, QueryExecutionErrorCode, QueryExecutionSnapshot,
)
from danbooru_multi_search.danbooru.result_processing import (
    build_query_result_session, process_query_snapshot,
)
from danbooru_multi_search.storage.history import HistoryError, HistoryRepository, RecentQuery
from danbooru_multi_search.storage.query_checkpoints import QueryCheckpointRepository


QueryIdentity = tuple[int | None, int | None, int]
CheckpointOwners = frozenset[tuple[int, str]]


class CheckpointFailureTarget(Protocol):
    @property
    def current_execution_id(self) -> int | None: ...

    def checkpoint_failed(self, message: str) -> None: ...


class QueryCheckpointTarget(Protocol):
    """Only execution facts/actions needed by the persistence use case."""

    @property
    def operation_id(self) -> int | None: ...

    @property
    def execution_id(self) -> int | None: ...

    @property
    def view_generation(self) -> int: ...

    @property
    def checkpoint_source(self) -> str: ...

    @property
    def executor(self) -> CheckpointFailureTarget: ...

    def capture_checkpoint(
        self, execution: ExecutionCheckpoint | None = None,
    ) -> QueryCheckpoint: ...

    def cancel(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ResumeClaim:
    """One claimed database owner and its short, independently held write lease."""

    record: CheckpointRecord
    write_owner: str


class QueryCheckpointService:
    """Own durable progress independently of history selection and Qt timers.

    failure_reported and ownership_retired are optional synchronous presentation
    callbacks. They run after service state is updated and may change execution.
    """

    def __init__(
        self,
        repository: QueryCheckpointRepository,
        history: HistoryRepository,
        target: QueryCheckpointTarget,
        access: SharedWriteAccess,
    ) -> None:
        self._repository = repository
        self._history = history
        self._target = target
        self._access = access
        self._leases: dict[QueryIdentity, CheckpointRecord] = {}
        self._retired_executions: set[tuple[int | None, int | None]] = set()
        self._failing_identities: set[QueryIdentity] = set()
        self._deferred: tuple[QueryIdentity, QueryCheckpoint, GalleryResultSummary] | None = None
        self._resuming: ResumeClaim | None = None
        self._closed = self._closing = False
        self.failure_reported: Callable[[QueryIdentity, CheckpointError], None] | None = None
        self.ownership_retired: Callable[[CheckpointOwners], None] | None = None

    @property
    def identity(self) -> QueryIdentity:
        return (
            self._target.operation_id,
            self._target.execution_id,
            self._target.view_generation,
        )

    @property
    def source(self) -> str:
        return self._target.checkpoint_source

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def resuming(self) -> ResumeClaim | None:
        return self._resuming

    @property
    def current_ownership(self) -> CheckpointRecord | None:
        return self._leases.get(self.identity)

    def begin_resume(
        self, record: CheckpointRecord, *, guard: Callable[[], bool],
    ) -> ResumeClaim | None:
        """Claim after reservation notifications; revoke only the replaced lease."""
        if self._closed or self._closing or self._resuming is not None or not guard():
            return None
        owner = uuid4().hex
        if not self._access.begin_external_write(owner):
            return None
        claimed = None
        try:
            if self._closed or self._closing or not guard():
                return None
            stored = self._repository.claim(record, self.source)
            for identity, lease in tuple(self._leases.items()):
                if lease.history_id == record.history_id and lease.owner == record.owner:
                    del self._leases[identity]
            claimed = ResumeClaim(stored, owner)
            self._resuming = claimed
            return claimed
        finally:
            if claimed is None:
                self._access.finish_external_write(owner)

    def release_resume_write(self, claim: ResumeClaim) -> None:
        """Release before network startup without ending the accepted-event claim."""
        if self._resuming is claim:
            self._access.finish_external_write(claim.write_owner)

    def finish_resume(self, claim: ResumeClaim) -> None:
        """End only this resume; database ownership remains claimed on UI abort."""
        if self._resuming is claim:
            self._resuming = None
        self._access.finish_external_write(claim.write_owner)

    def _check_write(self) -> None:
        own_maintenance = (
            self._access.maintenance_owner is not None
            and self._access.external_owner == self._access.maintenance_owner
            and not self._access.local_write_active
        )
        occupied = (
            self._access.external_owner is not None and not own_maintenance
            if self._closing else self._access.busy and not own_maintenance
        )
        if self._closed or occupied:
            raise CheckpointError("storage_busy", "收藏正在导入，检查点暂时无法保存，查询已停止。")

    def _hook(
        self,
        identity: QueryIdentity,
        checkpoint: QueryCheckpoint,
        owner: str,
        expected: CheckpointRecord | None = None,
        source_expected: CheckpointRecord | None = None,
    ) -> tuple[Callable[[Connection, RecentQuery], None], list[CheckpointRecord]]:
        stored: list[CheckpointRecord] = []

        def attach(connection: Connection, query: RecentQuery) -> None:
            if identity != self.identity or self._closed:
                raise CheckpointError("stale_checkpoint", "查询已改变，旧检查点不能保存。")
            stored.append(
                self._repository.save_in_transaction(
                    connection, query.id, owner, identity[2], checkpoint,
                    expected=expected, source_expected=source_expected,
                )
            )
        return attach, stored

    def record_accepted(self, event: GalleryQueryAccepted) -> RecentQuery | None:
        """Create history and its initial active checkpoint in one transaction."""
        self._check_write()
        identity = (event.operation_id, event.execution_id, 0)
        checkpoint = self._target.capture_checkpoint()
        claimed = (
            self._resuming.record
            if self._resuming is not None and event.origin is GalleryQueryOrigin.CHECKPOINT
            else None
        )
        owner = claimed.owner if claimed else uuid4().hex
        hook, stored = self._hook(identity, checkpoint, owner, expected=claimed)
        if claimed:
            self._history.update_result_summary(
                claimed.history_id, build_checkpoint_summary(checkpoint), transaction_hook=hook,
            )
            query = self._history.get_query(claimed.history_id)
        else:
            query = self._history.record_accepted_definition(
                definition=event.definition,
                display_text=event.display_text,
                page=1,
                count_tag_usage=event.count_tag_usage,
                used_at=event.accepted_at,
                transaction_hook=hook,
            )
        if not stored:
            raise CheckpointError("missing_history", "历史记录没有保存，查询已停止。")
        self._leases[identity] = stored[-1]
        return query

    def record_view(self, source_id: int, event: GalleryViewDefinitionChanged) -> RecentQuery | None:
        self._check_write()
        identity = (event.operation_id, event.execution_id, event.view_generation)
        if identity[:2] in self._retired_executions:
            return self._history.record_view_definition(source_query_id=source_id, definition=event.definition)
        source = self._leases.get((event.operation_id, event.execution_id, event.view_generation - 1))
        if source is None or source.history_id != source_id:
            raise CheckpointError("stale_checkpoint", "原查询的检查点归属已经失效。")
        hook, stored = self._hook(
            identity, self._target.capture_checkpoint(), source.owner, source_expected=source,
        )
        query = self._history.record_view_definition(
            source_query_id=source_id, definition=event.definition, transaction_hook=hook,
        )
        if stored:
            self._leases[identity] = stored[-1]
        return query

    def record_result(self, history_id: int, event: GalleryResultEvent) -> bool:
        identity = (event.operation_id, event.execution_id, event.view_generation)
        if identity in self._failing_identities:
            return True
        if identity[:2] in self._retired_executions:
            return self._history.update_result_summary(history_id, event.summary)
        if identity in self._leases and self._leases[identity].history_id != history_id:
            raise CheckpointError("stale_checkpoint", "结果与检查点的历史归属不同。")
        checkpoint = self._target.capture_checkpoint()
        if self._closing:
            self._deferred = (identity, checkpoint, event.summary)
            return True
        return self._save(identity, checkpoint, event.summary)

    def _save(
        self, identity: QueryIdentity, checkpoint: QueryCheckpoint,
        summary: GalleryResultSummary,
    ) -> bool:
        self._check_write()
        expected = self._leases.get(identity)
        if expected is None:
            raise CheckpointError("stale_checkpoint", "当前执行没有有效的检查点归属。")
        hook, stored = self._hook(identity, checkpoint, expected.owner, expected=expected)
        updated = self._history.update_result_summary(expected.history_id, summary, transaction_hook=hook)
        if not updated or not stored:
            raise CheckpointError("stale_checkpoint", "历史已经删除，旧执行不能重新建立检查点。")
        self._leases[identity] = stored[-1]
        return True

    def persist_execution(self, execution: ExecutionCheckpoint) -> bool:
        """Synchronous barrier: True alone authorizes the network starter."""
        if self._closed:
            return False
        if self.identity in self._failing_identities:
            return True
        if self.identity[:2] in self._retired_executions:
            if execution.active:
                self.persistence_failed(CheckpointError("cleared_checkpoint", "该执行的检查点已清理，请从头执行查询。"))
                return False
            return True
        if self._closing:
            checkpoint = self._target.capture_checkpoint(execution)
            self._deferred = (self.identity, checkpoint, build_checkpoint_summary(checkpoint))
            return True
        try:
            checkpoint = self._target.capture_checkpoint(execution)
            return self._save(self.identity, checkpoint, build_checkpoint_summary(checkpoint))
        except (CheckpointError, HistoryError, ValueError) as exc:
            self.persistence_failed(exc)
            return False

    def persistence_failed(self, error: Exception) -> None:
        identity = self.identity
        if self._closed or identity in self._failing_identities:
            return
        failure = CheckpointError("persistence", f"检查点保存失败，查询已停止：{error}")
        self._failing_identities.add(identity)
        try:
            if self._target.executor.current_execution_id == identity[1]:
                self._target.executor.checkpoint_failed(str(failure))
            if not self._closed and self.identity == identity and self.failure_reported is not None:
                self.failure_reported(identity, failure)
        finally:
            self._failing_identities.discard(identity)

    def cancel_if_owned(self, owners: CheckpointOwners) -> bool:
        """Cancel only an execution whose persisted owner is being removed."""
        active = self.current_ownership
        if active is None or (active.history_id, active.owner) not in owners:
            return False
        return self._target.cancel()

    def retire_checkpoints(self, owners: CheckpointOwners) -> None:
        """Revoke all affected writes before any presentation notification."""
        for identity, lease in tuple(self._leases.items()):
            if (lease.history_id, lease.owner) in owners:
                self._retired_executions.add(identity[:2])
                del self._leases[identity]
        if self._deferred is not None and self._deferred[0][:2] in self._retired_executions:
            self._deferred = None
        if not self._closed and self.ownership_retired is not None:
            self.ownership_retired(owners)

    def reconcile_ownership(self) -> None:
        current = {item.history_id: item.owner for item in self._repository.capture_cleanup()}
        revoked = frozenset(
            (lease.history_id, lease.owner)
            for lease in self._leases.values()
            if current.get(lease.history_id) != lease.owner
        )
        if revoked:
            self.retire_checkpoints(revoked)

    def begin_shutdown(self) -> None:
        """Stop requests now and capture final progress before waiting for import."""
        if self._closed or self._closing:
            return
        self._closing = True
        self._target.cancel()
        identity = self.identity
        if self._deferred is None and identity in self._leases:
            checkpoint = self._target.capture_checkpoint()
            self._deferred = (identity, checkpoint, build_checkpoint_summary(checkpoint))

    def finish_shutdown(self) -> None:
        """Write the captured final progress after the existing writer finishes."""
        if self._closed:
            return
        try:
            if self._deferred is not None:
                identity, checkpoint, summary = self._deferred
                self._save(identity, checkpoint, summary)
        finally:
            self._closed = True


def build_checkpoint_summary(checkpoint: QueryCheckpoint) -> GalleryResultSummary:
    """Summarize confirmed candidates with the persisted filter and sort rules."""
    execution = checkpoint.execution
    by_id = {post.id: post for post in execution.candidates}
    snapshot = QueryExecutionSnapshot(
        1,
        execution.state,
        execution.counts,
        tuple(by_id[key] for key in execution.matched_ids),
        execution.branches,
        execution.limit_reason,
        execution.failure,
        execution.persistence_error,
    )
    processed = process_query_snapshot(
        snapshot,
        build_query_result_session(1, execution.plan),
        checkpoint.runtime_filter,
        checkpoint.definition.sort_spec,
    )
    error_code = (
        QueryExecutionErrorCode.PERSISTENCE if execution.persistence_error is not None
        else execution.failure.error_code if execution.failure else None
    )
    return GalleryResultSummary(
        execution.state,
        execution.counts.remote_rows,
        execution.counts.unique_candidates,
        execution.counts.local_matches,
        processed.visible_count,
        execution.limit_reason,
        error_code,
    )
