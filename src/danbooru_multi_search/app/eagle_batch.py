"""Prepare a bounded gallery selection, then run one confirmed import at a time."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal

from danbooru_multi_search.app.eagle_import_job import EagleImportJob
from danbooru_multi_search.app.eagle_import_types import EaglePostImportRequest
from danbooru_multi_search.app.eagle_service import EagleResult
from danbooru_multi_search.app.query_candidates import (
    CandidateCriteria, CandidateResult, CandidateSelectionMode, CandidateSource, QueryCandidateService,
)
from danbooru_multi_search.danbooru.models import DanbooruPost


_ITEM_ERRORS = frozenset({
    "eagle_media_download", "eagle_media_staging", "eagle_missing_original", "eagle_missing_media",
    "eagle_file_invalid", "eagle_file_changed", "eagle_file_hash_failed", "eagle_invalid_metadata",
    "eagle_duplicate_conflict", "eagle_search_incomplete",
})


@dataclass(slots=True)
class BatchImportItem:
    post: DanbooruPost
    state: str = "waiting"
    result: EagleResult | None = None
    detail: str = ""


class EagleBatchJob(QObject):
    changed = Signal()

    def __init__(self, candidates: QueryCandidateService, item_job: EagleImportJob,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.candidates = candidates
        self.item_job = item_job
        self._next = QTimer(self)
        self._next.setSingleShot(True)
        self._next.timeout.connect(self._advance)
        self._build: Callable[[DanbooruPost], EaglePostImportRequest] | None = None
        self._awaiting = False
        self.items: list[BatchImportItem] = []
        self.index = 0
        self.planned = 0
        self.token = ""
        self.phase = ""
        self.reason = ""
        self.scope = ""
        self.started = ""
        self.ended = ""
        self.closed = False
        candidates.completed.connect(self._prepared)
        item_job.changed.connect(self._item_changed)

    @property
    def busy(self) -> bool:
        return self.phase in {"preparing", "running", "cancelling"}

    def start(self, source: CandidateSource, criteria: CandidateCriteria,
              build: Callable[[DanbooruPost], EaglePostImportRequest]) -> bool:
        if self.closed or self.busy or self.item_job.runtime.busy:
            return False
        token = uuid4().hex
        self.token = token
        self.items = []
        self.index = 0
        self.planned = len(source.selected_keys) if criteria.mode == CandidateSelectionMode.SELECTED else criteria.count
        self._build = build
        self.phase = "preparing"
        self.scope = source.range_description
        self.reason = ""
        self.started = datetime.now().astimezone().isoformat(timespec="seconds")
        self.ended = ""
        self.changed.emit()
        if self.phase != "preparing" or self.token != token or self.closed:
            return False
        try:
            accepted = self.candidates.prepare(source, criteria, token)
        except ValueError as exc:
            self._end("stopped", str(exc))
            return False
        if not accepted and self.phase == "preparing" and self.token == token:
            self._end("stopped", "查询来源已改变，无法准备候选作品。")
        return accepted

    def _prepared(self, result: CandidateResult) -> None:
        if self.closed or self.phase != "preparing" or result.token != self.token:
            return
        self.scope = result.reason
        if result.partial:
            self.scope += f" 当前为部分结果，已读取 {result.remote_rows} 条记录，扫描到 {result.scanned_candidates} 件不同作品。"
        if result.error_code:
            self._end("cancelled" if result.error_code == "cancelled" else "stopped", result.reason)
            return
        self.items = [BatchImportItem(post) for post in result.posts]
        self.planned = result.requested_count
        if not self.items:
            self._end("completed")
            return
        self.phase = "running"
        self._next.start(0)
        self.changed.emit()

    def _advance(self) -> None:
        if self.closed or self.phase != "running" or self._awaiting:
            return
        if self.index >= len(self.items):
            self._end("completed")
            return
        item = self.items[self.index]
        try:
            payload = self._build(item.post)
        except (TypeError, ValueError) as exc:
            item.state, item.detail = "failed", str(exc)
            self.index += 1
            self._next.start(0)
            self.changed.emit()
            return
        self._awaiting = True
        if not self.item_job.start(payload) and self._awaiting:
            self._awaiting = False
            item.state = "failed"
            item.detail = "Eagle 正在处理其他请求或当前不可用。"
            self._end("stopped", item.detail)

    def _item_changed(self) -> None:
        if self.closed or not self._awaiting:
            return
        item = self.items[self.index]
        if self.item_job.stage:
            item.state = self.item_job.stage
            self.changed.emit()
            return
        result = self.item_job.result
        if result is None:
            return
        self._awaiting = False
        item.result = result
        item.detail = "\n".join(filter(None, (result.diagnostic, self.item_job.file_notice)))
        if result.uncertain:
            item.state = "uncertain"
        elif result.error_code:
            item.state = "cancelled" if result.error_code == "eagle_cancelled" else "failed"
        elif result.import_result:
            item.state = result.import_result.state
        else:
            item.state = "failed"
        self.index += 1
        if self.phase == "cancelling":
            self._end("cancelled")
        elif result.uncertain or (item.state in {"failed", "cancelled"} and result.error_code not in _ITEM_ERRORS):
            self._end("stopped", f"作品 #{item.post.id} 未能确认导入条件，后续作品已停止。")
        else:
            self._next.start(0)
            self.changed.emit()

    def _end(self, phase: str, reason: str = "") -> None:
        self._next.stop()
        self._build = None
        self.phase = phase
        self.reason = reason
        self.ended = datetime.now().astimezone().isoformat(timespec="seconds")
        for item in self.items[self.index:]:
            if item.state == "waiting":
                item.state = "not_started"
        if not self.closed:
            self.changed.emit()

    def cancel(self, token: str) -> bool:
        if self.closed or not self.busy or self.phase == "cancelling" or token != self.token:
            return False
        previous = self.phase
        self.phase = "cancelling"
        self._next.stop()
        if previous == "preparing":
            self.candidates.cancel(token)
        if self._awaiting:
            self.item_job.cancel()
            if self._awaiting:
                self.changed.emit()
        else:
            self._end("cancelled")
        return True

    def begin_shutdown(self) -> None:
        self.closed = True
        self._next.stop()
        self._build = None
        self.candidates.shutdown()
        self.item_job.begin_shutdown()

    def finish_shutdown(self) -> None:
        self.item_job.finish_shutdown()
        self._awaiting = False
        if self.busy:
            self._end("cancelled")
