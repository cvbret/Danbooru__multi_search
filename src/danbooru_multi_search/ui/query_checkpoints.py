"""History selection, guarded resume interaction and checkpoint status for QML."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from PySide6.QtCore import QObject, Property, QTimer, Signal, Slot

from danbooru_multi_search.app.query_checkpoints import (
    CheckpointOwners, QueryCheckpointService, QueryIdentity, ResumeClaim,
)
from danbooru_multi_search.app.write_access import SharedWriteAccess
from danbooru_multi_search.danbooru.query_checkpoint import CheckpointError
from danbooru_multi_search.storage.history import HistoryError
from danbooru_multi_search.storage.query_checkpoints import QueryCheckpointRepository
from danbooru_multi_search.ui.query_state import M2QueryBridgeController

if TYPE_CHECKING:
    from danbooru_multi_search.ui.session_state import BrowserSessionController


class QueryCheckpointController(QObject):
    changed = Signal()
    draftLoadingChanged = Signal()

    def __init__(
        self,
        repository: QueryCheckpointRepository,
        service: QueryCheckpointService,
        bridge: M2QueryBridgeController,
        access: SharedWriteAccess,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._repository = repository
        self._service = service
        self._bridge = bridge
        self._access = access
        self._session: BrowserSessionController | None = None
        self._selected = None
        self._selection_token = ""
        self._generation = 0
        self._message = "请选择历史记录，查看是否可以续扫。"
        self._error = ""
        self._closed = self._closing = self._draft_loading = False
        self._draft_claim: ResumeClaim | None = None
        service.failure_reported = self._persistence_failed
        service.ownership_retired = self._retire_selection
        access.changed.connect(self._busy_changed)

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
        return (
            not self._closed and not self._closing
            and not self._service.closed and not self._service.closing
            and not self._access.busy and self._selected is not None
            and not self._selected.checkpoint.execution.resume_reason
            and self._selected.checkpoint.source == self._service.source
        )

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
            if (
                not isinstance(key, str) or not key.isascii() or not key.isdecimal()
                or str(int(key)) != key
            ):
                raise CheckpointError("invalid_identifier", "历史编号无效。")
            record = self._repository.get_for_history(int(key))
            if generation != self._generation or self._closed:
                return
            self._selected = record
            self._selection_token = uuid4().hex if record else ""
            self._message = (
                "这条历史没有执行检查点，可以从头执行。" if record is None
                else record.checkpoint.execution.resume_reason
                or "继续原有计划和分页位置，保留已使用的扫描额度。"
            )
            if record is not None and record.checkpoint.source != self._service.source:
                self._message = "当前 API 来源与检查点不同，不能续扫。"
        except (CheckpointError, ValueError) as exc:
            self._error = str(exc)
        if not self._closed and generation == self._generation:
            self.changed.emit()

    @Slot(str, result=bool)
    def resumeSelected(self, token: str) -> bool:
        record, generation = self._selected, self._generation
        if (
            self._service.resuming is not None or not self.canResume
            or not token or token != self._selection_token or self._session is None
        ):
            return False
        operation_id = self._service.identity[0]

        def current() -> bool:
            return (
                not self._closed and not self._closing
                and not self._service.closed and not self._service.closing
                and self._generation == generation and self._selected is record
                and self._selection_token == token
                and self._service.identity[0] == operation_id
            )

        claim = None
        try:
            claim = self._service.begin_resume(record, guard=current)
            if claim is None:
                return False
            self._draft_claim = claim
            self._draft_loading = True
            self.draftLoadingChanged.emit()
            if not current():
                return False
            self._service.release_resume_write(claim)
            # Releasing emits synchronous callbacks which may navigate or query.
            if not current():
                return False
            succeeded = bool(
                self._session.resumeCheckpoint(
                    claim.record.checkpoint, claim.record.history_id,
                )
            )
            if not succeeded and current():
                self._error = self._bridge.errorText or "检查点载入已失效，请重新选择历史。"
            return succeeded
        except (CheckpointError, HistoryError, ValueError) as exc:
            if not self._closed and self._generation == generation:
                self._error = str(exc)
                self._selected, self._selection_token = None, ""
            return False
        finally:
            if claim is not None:
                self._service.finish_resume(claim)
            if not self._closed:
                self.changed.emit()
                QTimer.singleShot(0, lambda: self._finish_draft_loading(claim))

    def _finish_draft_loading(self, claim: ResumeClaim | None) -> None:
        if not self._closed and self._draft_loading and self._draft_claim is claim:
            self._draft_loading = False
            self._draft_claim = None
            self.draftLoadingChanged.emit()

    def _persistence_failed(self, identity: QueryIdentity, error: CheckpointError) -> None:
        if self._closed or self._service.identity != identity:
            return
        self._error = str(error)
        self._bridge._set_exception_error(error)
        if not self._closed and self._service.identity == identity:
            self.changed.emit()

    def _retire_selection(self, owners: CheckpointOwners) -> None:
        if self._closed:
            return
        if self._selected is not None and (
            self._selected.history_id, self._selected.owner
        ) in owners:
            self._generation += 1
            self._selected, self._selection_token = None, ""
            self._message = "检查点已清理，历史条件仍可从头执行。"
            self._error = ""
        self.changed.emit()

    def begin_shutdown(self) -> None:
        """Invalidate presentation actions before the service captures final work."""
        if self._closed:
            return
        self._closed = self._closing = True
        self._generation += 1
        self._selected, self._selection_token = None, ""
        self._draft_claim = None
        self._draft_loading = False

    @Slot()
    def _busy_changed(self) -> None:
        if not self._closed:
            self.changed.emit()
