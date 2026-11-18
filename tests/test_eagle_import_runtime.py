import base64
from contextlib import contextmanager
from dataclasses import replace
from threading import Event

import pytest
from PySide6.QtCore import QThread

from danbooru_multi_search.app import eagle_runtime
from danbooru_multi_search.app.eagle_import_job import EagleImportJob
from danbooru_multi_search.app.eagle_import_types import EaglePostImportError, EaglePostImportOutcome, EaglePostImportRequest
from danbooru_multi_search.app.eagle_runtime import EagleRuntime, validate_eagle_media
from danbooru_multi_search.app.eagle_service import EagleBackend, EagleOperation, EagleRequest, EagleResult
from danbooru_multi_search.core.results import ServiceAvailability
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.eagle_imports import EagleImportRepository
from danbooru_multi_search.tasks import BoundedTaskQueue, TaskQueueConfig
from tests.test_favorite_controller import APPLICATION, IMAGE, post
from tests.test_eagle_import import context
from tests.test_eagle_import_job import Media
from tests.test_qml_favorites import wait
from tests.test_task_queue import ManualClock, ManualScheduler


def test_image_decoder_checks_the_image_data_after_the_signature(tmp_path):
    path = tmp_path / "image.png"
    png = base64.b64decode(IMAGE.split(",", 1)[1])
    path.write_bytes(png)
    validate_eagle_media(path, "png")
    path.write_bytes(png[:33] + png[-12:])  # Keep the valid header and ending, remove image data.
    with pytest.raises(EaglePostImportError) as failed:
        validate_eagle_media(path, "png")
    assert failed.value.code == "eagle_file_invalid"


def test_shutdown_after_worker_commit_before_gui_delivery_cleans_original(tmp_path, context):
    committed = Event()

    @contextmanager
    def factory(cancelled, deadline):
        yield EagleBackend(context.bridge, lambda: context.bridge.sent, context.repository)
        if context.bridge.sent:
            committed.set()

    clock = ManualClock()
    scheduler = ManualScheduler(clock)
    queue = BoundedTaskQueue(TaskQueueConfig(minimum_interval_seconds=0), clock=clock, scheduler=scheduler)
    runtime = EagleRuntime(factory, ServiceAvailability(True), queue)
    media = Media()
    job = EagleImportJob(runtime, media, tmp_path / "staging")
    try:
        assert job.start(replace(context.work, file_path=None))
        scheduler.run_ready()
        assert wait(lambda: bool(media.calls))
        submitted = media.finish(context.path.read_bytes())
        request_id = runtime.active_request.request_id
        scheduler.run_ready()
        assert committed.wait(2)  # Wait without processing the queued GUI notification.
        assert job.result is None and submitted.is_file()
        job.begin_shutdown()
        runtime.shutdown()
        assert runtime.stopped_import_result("unrelated-request") is None
        assert runtime.stopped_import_result(request_id).import_result.item_identifier == "item-1"
        job.finish_shutdown()
        reopened = EagleImportRepository(Database(context.database.path))
        assert reopened.find_by_post("library-A", 321).item_id == "item-1"
        assert not reopened.find_attempts("library-A", 321)
        assert job.result.import_result.state == "imported" and not submitted.exists()
        assert not (submitted.parent / ".stopped").exists() and len(context.server.writes) == 1
    finally:
        job.begin_shutdown()
        runtime.shutdown()
        job.finish_shutdown()
        queue.shutdown()


@pytest.mark.parametrize("state", ["ready", "imported", "updated", "skipped", "uncertain"])
def test_late_cancel_preserves_confirmed_item_and_stops_unfinished_operation(tmp_path, monkeypatch, state):
    worker_finished = Event()
    observed = []
    clock = ManualClock()
    scheduler = ManualScheduler(clock)
    queue = BoundedTaskQueue(TaskQueueConfig(minimum_interval_seconds=0), clock=clock, scheduler=scheduler)
    wrote = state in {"imported", "updated", "uncertain"}
    item_id = None if state == "ready" else "EAGLE-1"

    @contextmanager
    def factory(cancelled, deadline):
        yield EagleBackend(None, lambda: wrote)
        worker_finished.set()

    def execute(request, backend, cancelled):
        assert QThread.currentThread() is not APPLICATION.thread()
        assert backend.validate_media is validate_eagle_media
        return EagleResult(
            request.request_id, post_started=wrote, uncertain=state == "uncertain",
            error_code="eagle_timeout" if state == "uncertain" else "",
            import_result=EaglePostImportOutcome(state=state, item_identifier=item_id),
        )

    monkeypatch.setattr(eagle_runtime, "execute_eagle_request", execute)
    runtime = EagleRuntime(factory, ServiceAvailability(True), queue)
    runtime.completed.connect(observed.append)
    work = EaglePostImportRequest("LIB", None, post(), "https://example.invalid/posts/1")
    operation = EagleOperation.PRECHECK_POST if state in {"ready", "skipped"} else EagleOperation.IMPORT_POST
    if operation == EagleOperation.IMPORT_POST:
        work = replace(work, file_path=tmp_path / "simulated-file.png")
    try:
        assert runtime.start(EagleRequest("request", operation, post_import=work))
        scheduler.run_ready()
        assert worker_finished.wait(2)
        assert runtime.cancel()
        assert wait(lambda: len(observed) == 1)
        result = observed[0]
        assert result.import_result.item_identifier == item_id
        if state in {"imported", "updated", "skipped"}:
            assert not result.error_code and not result.uncertain
            assert result.import_result.state == state
        else:
            assert result.error_code == "eagle_cancelled"
            assert result.uncertain == (state == "uncertain")
    finally:
        runtime.shutdown()
        queue.shutdown()
