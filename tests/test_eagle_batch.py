from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal

from danbooru_multi_search.app.eagle_batch import EagleBatchJob
from danbooru_multi_search.app.eagle_import_types import EaglePostImportOutcome
from danbooru_multi_search.app.eagle_service import EagleResult
from danbooru_multi_search.app.query_candidates import CandidateCriteria, CandidateResult
from tests.test_favorite_controller import APPLICATION, post


class Candidates(QObject):
    completed = Signal(object)

    def __init__(self):
        super().__init__()
        self.cancelled = []
        self.closed = False

    def prepare(self, source, criteria, token):
        self.token = token
        return True

    def ready(self, token):
        self.completed.emit(CandidateResult(token, (post(1), post(2), post(3)), 3, "当前已扫描范围", True, 3, 5))

    def cancel(self, token):
        self.cancelled.append(token)

    def shutdown(self):
        self.closed = True


class ItemJob(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.runtime = SimpleNamespace(busy=False)
        self.calls = []
        self.stage = self.file_notice = ""
        self.result = None
        self.cancels = 0
        self.closed = self.finished = False

    def start(self, payload):
        assert not self.stage
        self.calls.append(payload.id)
        self.stage = "precheck_post"
        self.result = None
        self.changed.emit()
        return True

    def finish(self, error="", *, uncertain=False):
        self.stage = ""
        state = "uncertain" if uncertain else "error" if error else "imported"
        outcome = EaglePostImportOutcome(state, item_identifier="kept-id" if state != "error" else None)
        self.result = EagleResult("worker", error_code=error, uncertain=uncertain, import_result=outcome)
        self.changed.emit()

    def cancel(self):
        self.cancels += 1

    def begin_shutdown(self):
        self.closed = True

    def finish_shutdown(self):
        assert self.closed
        self.finished = True


@pytest.fixture
def batch():
    job = EagleBatchJob(Candidates(), ItemJob())
    assert job.start(SimpleNamespace(range_description="原范围"), CandidateCriteria(count=3), lambda work: work)
    yield job
    job.begin_shutdown()
    job.finish_shutdown()


@pytest.mark.parametrize("code, uncertain", [
    ("eagle_library_changed", False), ("eagle_folder_not_found", False), ("eagle_timeout", True),
])
def test_individual_error_continues_and_unsafe_outcome_stops_next_write(batch, code, uncertain):
    item = batch.item_job
    batch.candidates.ready("other-task")
    assert not batch.items
    batch.candidates.ready(batch.token)
    assert "5 条记录" in batch.scope
    APPLICATION.processEvents()
    assert item.calls == [1]
    item.finish("eagle_file_invalid")
    assert item.calls == [1]
    APPLICATION.processEvents()
    assert item.calls == [1, 2]
    item.finish(code, uncertain=uncertain)
    APPLICATION.processEvents()
    assert batch.phase == "stopped" and item.calls == [1, 2] and batch.ended
    assert [row.state for row in batch.items] == ["failed", "uncertain" if uncertain else "failed", "not_started"]
    if uncertain:
        assert batch.items[1].result.import_result.item_identifier == "kept-id"


@pytest.mark.parametrize("stage", ["preparing", "before_first", "active", "between"])
def test_cancel_never_starts_another_item_and_keeps_late_confirmation(batch, stage):
    item = batch.item_job
    assert not batch.cancel("old-token")
    if stage != "preparing":
        batch.candidates.ready(batch.token)
    if stage in ("active", "between"):
        APPLICATION.processEvents()
    if stage == "between":
        item.finish()
    assert batch.cancel(batch.token)
    if stage == "active":
        assert item.cancels == 1 and batch.phase == "cancelling"
        item.finish()
    batch.candidates.ready(batch.token)
    APPLICATION.processEvents()
    assert batch.phase == "cancelled" and not batch.busy
    if stage in ("active", "between"):
        assert item.calls == [1] and batch.items[0].state == "imported"
    else:
        assert not item.calls
    assert batch.candidates.cancelled == ([batch.token] if stage == "preparing" else [])


@pytest.mark.parametrize("stage", ["preparing", "before_first", "active", "between"])
def test_close_blocks_queued_steps_and_late_completions(batch, stage):
    item = batch.item_job
    if stage != "preparing":
        batch.candidates.ready(batch.token)
    if stage in ("active", "between"):
        APPLICATION.processEvents()
    if stage == "between":
        item.finish()
    before = list(item.calls)
    batch.begin_shutdown()
    batch.candidates.ready(batch.token)
    if stage == "active":
        item.finish()
    APPLICATION.processEvents()
    batch.finish_shutdown()
    assert item.calls == before and batch.candidates.closed and item.finished and not batch.busy
