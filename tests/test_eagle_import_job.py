from dataclasses import replace
import pytest

from PySide6.QtCore import QObject, Signal

from danbooru_multi_search.app.eagle_import_types import EaglePostImportOutcome, EaglePostImportRequest
from danbooru_multi_search.app.eagle_import_job import EagleImportJob
from danbooru_multi_search.app.eagle_media import EagleMediaFile
from danbooru_multi_search.app.eagle_service import EagleOperation, EagleResult
from danbooru_multi_search.core.downloads import DanbooruDownloadOutcome, DownloadedFile, DownloadState
from danbooru_multi_search.core.results import ServiceAvailability
from tests.test_favorite_controller import post


class Runtime(QObject):
    completed = Signal(object)
    availability = ServiceAvailability(True)
    shutdown_completed = True

    def __init__(self):
        super().__init__()
        self.requests = []
        self.active_request = None
        self.last_result = None

    @property
    def busy(self):
        return self.active_request is not None

    def start(self, request):
        assert not self.busy
        self.active_request = request
        self.requests.append(request)
        return True

    def finish(self, state, **kwargs):
        request = self.active_request
        self.active_request = None
        outcome = EaglePostImportOutcome(
            state=state, item_identifier="ITEM" if state in {"skipped", "imported", "updated"} else None,
        )
        self.last_result = EagleResult(request.request_id, import_result=outcome, **kwargs)
        self.completed.emit(self.last_result)

    def stopped_import_result(self, request_id):
        return self.last_result if self.last_result and self.last_result.request_id == request_id else None

    def cancel(self):
        self.finish("error", error_code="eagle_cancelled")
        return True


class Media(QObject):
    downloadCompleted = Signal(object)

    def __init__(self):
        super().__init__()
        self.calls = []

    def download_source(self, url, path):
        self.calls.append((url, path))

    def finish(self, data=b"owned bytes"):
        url, path = self.calls[-1]
        path.write_bytes(data)
        self.downloadCompleted.emit(DanbooruDownloadOutcome(
            DownloadState.SUCCESS, "", DownloadedFile(path, url, len(data))))
        return path

    def cancelCurrentDownload(self):
        self.downloadCompleted.emit(DanbooruDownloadOutcome(DownloadState.CANCELLED, "cancelled"))

    def shutdown(self):
        self.cancelCurrentDownload()


def prepare(tmp_path):
    runtime, media = Runtime(), Media()
    job = EagleImportJob(runtime, media, tmp_path)
    payload = EaglePostImportRequest("LIB", None, post(), "https://example.invalid/posts/1")
    return job, runtime, media, payload


def test_precheck_can_skip_without_original_or_download(tmp_path):
    job, runtime, media, payload = prepare(tmp_path)
    assert job.start(replace(payload, post=replace(payload.post, file_url=None)))
    runtime.finish("skipped")
    assert job.result.import_result.item_identifier == "ITEM"
    assert not job.stage and not media.calls and not list(tmp_path.iterdir())


def test_retry_after_known_failure_reuses_file_and_uncertain_keeps_submitted_path(tmp_path):
    job, runtime, media, payload = prepare(tmp_path)
    assert job.start(payload)
    runtime.finish("ready")
    first = media.finish()
    assert runtime.active_request.post_import.file_path == first
    runtime.finish("error", error_code="eagle_connection_error")
    assert not first.exists() and job.file_notice
    assert job.start(payload)
    runtime.finish("ready")
    retry = runtime.active_request.post_import.file_path
    assert retry != first and retry.read_bytes() == b"owned bytes" and len(media.calls) == 1
    runtime.finish("uncertain", error_code="eagle_timeout", uncertain=True, post_started=True)
    assert retry.is_file() and str(retry) in job.file_notice


def test_invalid_staged_file_is_downloaded_again_and_cancellation_stops_submission(tmp_path):
    job, runtime, media, payload = prepare(tmp_path)
    job.start(payload)
    runtime.finish("ready")
    invalid = media.finish()
    runtime.finish("error", error_code="eagle_file_invalid")
    assert not invalid.exists()
    job.start(payload)
    runtime.finish("ready")
    assert len(media.calls) == 2
    assert job.cancel()
    assert job.result.error_code == "eagle_cancelled"
    assert [req.operation for req in runtime.requests].count(EagleOperation.IMPORT_POST) == 1


@pytest.mark.parametrize("confirmed", [True, False])
def test_shutdown_waits_before_releasing_file_and_ignores_worker_callback(tmp_path, confirmed):
    job, runtime, media, payload = prepare(tmp_path)
    job.start(payload)
    runtime.finish("ready")
    pending = media.finish()
    job.begin_shutdown()
    if confirmed:
        runtime.finish("imported")
    else:
        runtime.finish("uncertain", error_code="eagle_timeout", uncertain=True, post_started=True)
    assert pending.exists() and job.result is None
    job.finish_shutdown()
    assert pending.exists() is not confirmed and not job.stage
    assert not job.start(payload)


def test_simultaneous_imports_cannot_remove_each_others_inputs(tmp_path):
    work = post()
    first = EagleMediaFile.prepare(tmp_path, work)
    second = EagleMediaFile.prepare(tmp_path, work)
    assert first.path != second.path
    first.path.write_bytes(b"first")
    second.path.write_bytes(b"second")
    first.finish(retain=True)
    assert second.path.read_bytes() == b"second"
    third = EagleMediaFile.prepare(tmp_path, work)
    assert third.recovered and third.path.read_bytes() == b"first"
    second.finish(retain=False)
    third.finish(retain=False)
    assert not list(tmp_path.iterdir())


def test_recovery_cleans_only_owned_stopped_and_unchanged_files(tmp_path):
    root = tmp_path / "staging"
    external = EagleMediaFile.prepare(tmp_path / "other-app", post())
    running, stopped, modified = [EagleMediaFile.prepare(root, post()) for _ in range(3)]
    for file in (running, stopped, modified, external):
        file.path.write_bytes(b"original")
        file.capture_for_import()
    for file in (stopped, modified, external):
        file.mark_stopped()
    modified.path.write_bytes(b"changed since the Eagle call")
    for file in (running, stopped, modified, external):
        EagleMediaFile.release_recovered(root, file.path)
    assert not stopped.path.exists()
    assert all(file.path.is_file() for file in (running, modified, external))


@pytest.mark.parametrize("later_error", ["", "eagle_duplicate_conflict"])
def test_restarted_precheck_releases_confirmed_previous_file_without_redownloading(tmp_path, later_error):
    job, runtime, media, payload = prepare(tmp_path)
    job.start(payload)
    runtime.finish("ready")
    previous = media.finish()
    runtime.finish("uncertain", error_code="eagle_timeout", uncertain=True, post_started=True)
    assert previous.is_file()

    new_job, new_runtime, new_media, payload = prepare(tmp_path)
    new_job.start(payload)
    request = new_runtime.active_request
    new_runtime.active_request = None
    new_runtime.completed.emit(EagleResult(
        request.request_id, error_code=later_error, import_result=EaglePostImportOutcome(
            state="error" if later_error else "skipped", item_identifier="RECOVERED", recovered_paths=(previous,),
        )))
    assert new_job.result.import_result.item_identifier == "RECOVERED"
    assert not previous.exists() and not new_media.calls
