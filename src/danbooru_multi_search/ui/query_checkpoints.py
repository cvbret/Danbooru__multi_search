"""Coordinate durable page boundaries and explicit, revision-checked resume."""

from __future__ import annotations

from uuid import uuid4
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Property, QTimer, Signal, Slot

from danbooru_multi_search.danbooru.query_checkpoint import CheckpointError, CheckpointRecord, ExecutionCheckpoint, QueryCheckpoint
from danbooru_multi_search.danbooru.query_controller import DanbooruM2QueryController, GalleryQueryOrigin, GalleryQueryAccepted, GalleryViewDefinitionChanged, GalleryResultEvent
from danbooru_multi_search.danbooru.query_types import GalleryResultSummary, QueryExecutionSnapshot, QueryExecutionErrorCode
from danbooru_multi_search.danbooru.result_processing import build_query_result_session, process_query_snapshot
from danbooru_multi_search.storage.history import HistoryError, HistoryRepository, RecentQuery
from danbooru_multi_search.storage.query_checkpoints import QueryCheckpointRepository
from danbooru_multi_search.ui.favorites import FavoriteController
from danbooru_multi_search.ui.query_state import M2QueryBridgeController

if TYPE_CHECKING:
    from danbooru_multi_search.ui.session_state import BrowserSessionController


class QueryCheckpointController(QObject):
    changed = Signal()
    draftLoadingChanged = Signal()

    def __init__(self, repository: QueryCheckpointRepository, history: HistoryRepository,
                 controller: DanbooruM2QueryController, bridge: M2QueryBridgeController,
                 favorites: FavoriteController, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._repository, self._history, self._controller = repository, history, controller
        self._bridge, self._favorites = bridge, favorites
        self._session = None
        self._leases = {}
        self._selected = None
        self._selection_token = ""
        self._generation = 0
        self._message = "请选择历史记录，查看是否可以续扫。"
        self._error = ""
        self._closed = self._closing = self._draft_loading = False
        self._failing_identities: set[tuple[int | None, int | None, int]] = set()
        self._deferred = None
        self._resuming = None
        controller.executor.before_attempt = self.persist_execution
        controller.executor.before_abandon = self.persist_execution
        controller.executor.before_publish = self.persist_execution
        favorites.busyChanged.connect(self._busy_changed)

    def attach_session(self, session: BrowserSessionController) -> None:
        self._session = session

    @Property(str, notify=changed)
    def messageText(self) -> str:
        return self._error or self._message

    @Property(str, notify=changed)
    def selectionToken(self) -> str:
        return self._selection_token

    @Property(bool, notify=changed)
    def canResume(self) -> bool:
        return not self._closed and not self._closing and not self._favorites.busy and self._selected is not None and not self._selected.checkpoint.execution.resume_reason and self._selected.checkpoint.source == self._controller.checkpoint_source

    @Property(bool, notify=draftLoadingChanged)
    def draftLoading(self) -> bool:
        return self._draft_loading

    @Slot(str)
    def selectHistory(self, key: str) -> None:
        if self._closed or self._closing:
            return
        self._generation += 1
        generation = self._generation
        self._selected, self._selection_token, self._error = None, "", ""
        try:
            if not isinstance(key, str) or not key.isascii() or not key.isdecimal() or str(int(key)) != key:
                raise CheckpointError("invalid_identifier", "历史编号无效。")
            record = self._repository.get_for_history(int(key))
            if generation != self._generation or self._closed:
                return
            self._selected = record
            self._selection_token = uuid4().hex if record else ""
            self._message = "这条历史没有执行检查点，可以从头执行。" if record is None else record.checkpoint.execution.resume_reason or "继续原有计划和分页位置，保留已使用的扫描额度。"
            if record is not None and record.checkpoint.source != self._controller.checkpoint_source:
                self._message = "当前 API 来源与检查点不同，不能续扫。"
        except (CheckpointError, ValueError) as exc:
            self._error = str(exc)
        if not self._closed and generation == self._generation:
            self.changed.emit()

    @Slot(str, result=bool)
    def resumeSelected(self, token: str) -> bool:
        record, generation = self._selected, self._generation
        if self._resuming is not None or not self.canResume or not token or token != self._selection_token or self._session is None:
            return False
        operation_id = self._controller.operation_id
        claimed = None
        owner = uuid4().hex
        if not self._favorites.begin_external_write(owner):
            return False
        try:
            if self._closed or self._closing or self._generation != generation or self._selected != record or self._selection_token != token or self._controller.operation_id != operation_id:
                return False
            claimed = self._repository.claim(record, self._controller.checkpoint_source)
            # Claim transfers this history's ownership even when loading is later
            # interrupted. Old local executions must never save with that lease.
            for identity, lease in tuple(self._leases.items()):
                if lease.history_id == record.history_id and lease.owner == record.owner:
                    del self._leases[identity]
            self._resuming = claimed
            self._draft_loading = True
            self.draftLoadingChanged.emit()
            if self._closed or self._closing or self._generation != generation:
                return False
            # Release the short claim before execution; the accepted boundary writes again.
            self._favorites.finish_external_write(owner)
            if self._closed or self._closing or self._generation != generation or self._selected != record or self._selection_token != token or self._controller.operation_id != operation_id:
                return False
            succeeded = bool(self._session.resumeCheckpoint(claimed.checkpoint, claimed.history_id))
            if not succeeded:
                self._error = self._bridge.errorText or "检查点载入已失效，请重新选择历史。"
            return succeeded
        except (CheckpointError, HistoryError, ValueError) as exc:
            self._error = str(exc)
            self._selected, self._selection_token = None, ""
            return False
        finally:
            if self._resuming is claimed:
                self._resuming = None
            self._favorites.finish_external_write(owner)
            if not self._closed:
                self.changed.emit()
                QTimer.singleShot(0, self._finish_draft_loading)

    def _finish_draft_loading(self):
        if not self._closed and self._draft_loading:
            self._draft_loading = False
            self.draftLoadingChanged.emit()

    def _identity(self):
        return self._controller.operation_id, self._controller.execution_id, self._controller.view_generation

    def _check_write(self):
        occupied = self._favorites._external_write is not None if self._closing else self._favorites.busy
        if self._closed or occupied:
            raise CheckpointError("storage_busy", "收藏正在导入，检查点暂时无法保存，查询已停止。")

    def _hook(self, identity, checkpoint, owner, expected=None, source_expected=None):
        stored = []
        def attach(connection, query):
            if identity != self._identity() or self._closed:
                raise CheckpointError("stale_checkpoint", "查询已改变，旧检查点不能保存。")
            stored.append(self._repository.save_in_transaction(connection, query.id, owner, identity[2], checkpoint,
                expected=expected, source_expected=source_expected))
        return attach, stored

    def record_accepted(self, event: GalleryQueryAccepted) -> RecentQuery | None:
        """Create history and its initial active checkpoint in one transaction."""
        self._check_write()
        identity = (event.operation_id, event.execution_id, 0)
        checkpoint = self._controller.capture_checkpoint()
        claimed = self._resuming if event.origin is GalleryQueryOrigin.CHECKPOINT else None
        owner = claimed.owner if claimed else uuid4().hex
        hook, stored = self._hook(identity, checkpoint, owner, expected=claimed)
        if claimed:
            self._history.update_result_summary(claimed.history_id, self._summary(checkpoint), transaction_hook=hook)
            query = self._history.get_query(claimed.history_id)
        else:
            query = self._history.record_accepted_definition(definition=event.definition, display_text=event.display_text, page=1,
                count_tag_usage=event.count_tag_usage, used_at=event.accepted_at, transaction_hook=hook)
        if not stored:
            raise CheckpointError("missing_history", "历史记录没有保存，查询已停止。")
        self._leases[identity] = stored[-1]
        return query

    def record_view(self, source_id: int, event: GalleryViewDefinitionChanged) -> RecentQuery | None:
        self._check_write()
        identity = (event.operation_id, event.execution_id, event.view_generation)
        source = self._leases.get((event.operation_id, event.execution_id, event.view_generation - 1))
        if source is None or source.history_id != source_id:
            raise CheckpointError("stale_checkpoint", "原查询的检查点归属已经失效。")
        hook, stored = self._hook(identity, self._controller.capture_checkpoint(), source.owner, source_expected=source)
        query = self._history.record_view_definition(source_query_id=source_id, definition=event.definition, transaction_hook=hook)
        if stored:
            self._leases[identity] = stored[-1]
        return query

    def record_result(self, history_id: int, event: GalleryResultEvent) -> bool:
        identity = (event.operation_id, event.execution_id, event.view_generation)
        if identity in self._failing_identities:
            return True
        if identity in self._leases and self._leases[identity].history_id != history_id:
            raise CheckpointError("stale_checkpoint", "结果与检查点的历史归属不同。")
        checkpoint = self._controller.capture_checkpoint()
        if self._closing:
            self._deferred = (identity, checkpoint, event.summary)
            return True
        return self._save(identity, checkpoint, event.summary)

    @staticmethod
    def _summary(checkpoint: QueryCheckpoint) -> GalleryResultSummary:
        execution = checkpoint.execution
        by_id = {post.id: post for post in execution.candidates}
        snapshot = QueryExecutionSnapshot(1, execution.state, execution.counts, tuple(by_id[key] for key in execution.matched_ids),
            execution.branches, execution.limit_reason, execution.failure, execution.persistence_error)
        processed = process_query_snapshot(snapshot, build_query_result_session(1, execution.plan), checkpoint.runtime_filter, checkpoint.definition.sort_spec)
        return GalleryResultSummary(execution.state, execution.counts.remote_rows, execution.counts.unique_candidates, execution.counts.local_matches,
            processed.visible_count, execution.limit_reason, QueryExecutionErrorCode.PERSISTENCE if execution.persistence_error is not None else execution.failure.error_code if execution.failure else None)

    def _save(self, identity, checkpoint, summary):
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
        if self._identity() in self._failing_identities:
            return True
        if self._closing:
            checkpoint = self._controller.capture_checkpoint(execution)
            self._deferred = (self._identity(), checkpoint, self._summary(checkpoint))
            return True
        if self._closed:
            return False
        try:
            checkpoint = self._controller.capture_checkpoint(execution)
            return self._save(self._identity(), checkpoint, self._summary(checkpoint))
        except (CheckpointError, HistoryError, ValueError) as exc:
            self.persistence_failed(exc)
            return False

    def persistence_failed(self, error: Exception) -> None:
        identity = self._identity()
        if self._closed or identity in self._failing_identities:
            return
        self._error = f"检查点保存失败，查询已停止：{error}"
        self._failing_identities.add(identity)
        try:
            if self._controller.executor.current_execution_id == identity[1]:
                self._controller.executor.checkpoint_failed(self._error)
            if not self._closed and self._identity() == identity:
                self._bridge._set_exception_error(CheckpointError("persistence", self._error))
        finally:
            self._failing_identities.discard(identity)
        if not self._closed and self._identity() == identity:
            self.changed.emit()

    def begin_shutdown(self) -> None:
        if self._closed or self._closing:
            return
        self._closing = True
        self._generation += 1
        self._controller.cancel()
        identity = self._identity()
        if self._deferred is None and identity in self._leases:
            checkpoint = self._controller.capture_checkpoint()
            self._deferred = (identity, checkpoint, self._summary(checkpoint))

    def finish_shutdown(self) -> None:
        if self._closed:
            return
        try:
            if self._deferred is not None:
                identity, checkpoint, summary = self._deferred
                self._save(identity, checkpoint, summary)
        except (CheckpointError, HistoryError, ValueError) as exc:
            self._error = f"最后检查点保存失败：{exc}"
            raise
        finally:
            self._closed = True
            self._selected, self._selection_token = None, ""

    @Slot()
    def _busy_changed(self):
        if not self._closed:
            self.changed.emit()
