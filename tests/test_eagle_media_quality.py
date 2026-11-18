import base64
import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace

import pytest

from danbooru_multi_search.app.eagle_import_job import EagleImportJob
from danbooru_multi_search.app.eagle_import_metadata import annotation_markers
from danbooru_multi_search.app.eagle_runtime import EagleRuntime
from danbooru_multi_search.app.eagle_service import EagleBackend
from danbooru_multi_search.core.downloads import DanbooruDownloadOutcome, DownloadState
from danbooru_multi_search.core.results import ServiceAvailability
from danbooru_multi_search.danbooru.download import DanbooruDownloadController
from danbooru_multi_search.tasks import BoundedTaskQueue, QtScheduler, SystemClock, TaskQueueConfig
from tests.test_eagle_client import ServerResponse, eagle_server
from tests.test_eagle_import import context
from tests.test_eagle_import_job import prepare
from tests.test_qml_favorites import wait


GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


def test_real_download_falls_back_then_updates_without_relabelling_existing_bytes(tmp_path, context, eagle_server):
    png = context.path.read_bytes()
    server = eagle_server(
        ServerResponse(b"missing", status=404), ServerResponse(GIF, headers={"Content-Type": "image/gif"}),
        ServerResponse(png, headers={"Content-Type": "image/png"}),
    )
    original = server.base_url + "original.png"
    preview = server.base_url + "rendition?size=small"
    post = replace(context.work.post, file_url=original, preview_url=preview)
    work = replace(context.work, post=post, file_path=None, fallback_quality="preview")
    queue = BoundedTaskQueue(TaskQueueConfig(minimum_interval_seconds=0), clock=SystemClock(), scheduler=QtScheduler())

    @contextmanager
    def factory(cancelled, deadline):
        context.bridge.sent = False
        yield EagleBackend(context.bridge, lambda: context.bridge.sent, context.repository)

    runtime = EagleRuntime(factory, ServiceAvailability(True), queue)
    downloader = DanbooruDownloadController(task_queue=queue, require_media_response=True)
    root = tmp_path / "staging"
    job = EagleImportJob(runtime, downloader, root)

    def submit(request):
        assert job.start(request) and wait(lambda: not job.stage, timeout=4000)
        assert not job.result.error_code, job.result
        return job.result.import_result

    try:
        added = submit(work)
        assert (added.state, added.actual_quality, added.actual_extension) == ("imported", "preview", "gif")
        assert context.server.writes[0][1].suffix == ".gif"
        assert context.repository.find_by_post("library-A", post.id).sha256 == hashlib.sha256(GIF).hexdigest()
        skipped = submit(work)
        assert (skipped.state, skipped.actual_quality, skipped.actual_extension) == ("skipped", None, None)
        assert len(server.requests) == 2
        updated = submit(replace(work, update_duplicates=True))
        assert (updated.state, updated.actual_quality, updated.actual_extension) == ("updated", "original", "png")
        assert updated.item_identifier == added.item_identifier
        annotation = context.server.items[added.item_identifier].annotation
        details = json.loads("\n".join(annotation.splitlines()[1:-1]))["details"]
        assert details["首次导入清晰度"] == "预览图" and details["首次导入文件格式"] == "gif"
        assert details["本次采集清晰度"] == "原图" and details["本次采集文件格式"] == "png"
        marker = annotation_markers(annotation)[0]
        assert marker.sha256 == hashlib.sha256(GIF).hexdigest() and marker.source_sha256 == hashlib.sha256(png).hexdigest()
        assert [r.path for r in server.requests] == ["/api/v2/original.png", "/api/v2/rendition?size=small", "/api/v2/original.png"]
        assert not list(root.iterdir())
    finally:
        job.begin_shutdown()
        runtime.shutdown()
        job.finish_shutdown()
        queue.shutdown()


@pytest.mark.parametrize(("requested", "fallback", "expected", "error"), [
    ("original", "large", "large", ""), ("original", "preview", "preview", ""),
    ("original", None, None, "eagle_missing_original"), ("large", None, "large", ""),
    ("preview", None, "preview", ""),
])
def test_missing_original_uses_only_explicit_rendition(tmp_path, requested, fallback, expected, error):
    job, runtime, media, payload = prepare(tmp_path)
    post = replace(payload.post, file_url=None, file_ext="webm", file_size=3000000,
                   large_file_url="https://example.invalid/sample.jpg", preview_url="https://example.invalid/thumb")
    assert job.start(replace(payload, post=post, requested_quality=requested, fallback_quality=fallback))
    runtime.finish("ready")
    if error:
        assert job.result.error_code == error and not media.calls
        return
    url, temporary = media.calls[0]
    assert url == post.media_url(expected)
    media.finish(GIF)
    submitted = runtime.active_request.post_import
    assert submitted.post is post and submitted.selected_quality == expected
    assert submitted.file_path.suffix == ".gif" and not temporary.exists()
    runtime.finish("imported")
    assert not list(tmp_path.iterdir())


def test_html_and_server_error_do_not_trigger_an_unrequested_download(tmp_path):
    job, runtime, media, payload = prepare(tmp_path)
    post = replace(payload.post, file_url="https://example.invalid/original", preview_url="https://example.invalid/thumb")
    assert job.start(replace(payload, post=post, fallback_quality="preview"))
    runtime.finish("ready")
    media.downloadCompleted.emit(DanbooruDownloadOutcome(DownloadState.ERROR, "HTTP 503", http_status=503))
    assert len(media.calls) == 1 and job.result.error_code == "eagle_media_download"
    assert job.start(replace(payload, post=post, requested_quality="preview"))
    runtime.finish("ready")
    media.finish(b"<html>Service unavailable</html>")
    assert job.result.error_code == "eagle_file_invalid" and not list(tmp_path.iterdir())
    assert not runtime.active_request


def test_same_url_as_original_is_downloaded_only_once_and_reports_original(tmp_path):
    job, runtime, media, payload = prepare(tmp_path)
    assert job.start(replace(payload, requested_quality="large"))
    runtime.finish("ready")
    media.finish()
    assert runtime.active_request.post_import.selected_quality == "original"
    runtime.finish("imported")
    assert job.start(replace(payload, fallback_quality="large"))
    runtime.finish("ready")
    media.downloadCompleted.emit(DanbooruDownloadOutcome(DownloadState.ERROR, "HTTP 410", http_status=410))
    assert len(media.calls) == 2 and job.result.error_code == "eagle_media_download"


def test_missing_selected_rendition_does_not_choose_another_quality(tmp_path):
    job, runtime, media, payload = prepare(tmp_path)
    assert job.start(replace(payload, post=replace(payload.post, large_file_url=None), requested_quality="large"))
    runtime.finish("ready")
    assert job.result.error_code == "eagle_missing_media" and not media.calls
    with pytest.raises(ValueError):
        replace(payload, requested_quality="preview", fallback_quality="large")


def test_renamed_rendition_can_be_retried_without_reusing_an_original(tmp_path):
    job, runtime, media, payload = prepare(tmp_path)
    post = replace(payload.post, preview_url="https://example.invalid/thumb")
    sample = replace(payload, post=post, requested_quality="preview")
    job.start(sample)
    runtime.finish("ready")
    media.finish(GIF)
    first = runtime.active_request.post_import.file_path
    runtime.finish("error", error_code="eagle_connection_error")
    assert not first.exists() and len(media.calls) == 1
    job.start(replace(payload, post=post))
    runtime.finish("ready")
    assert len(media.calls) == 2 and media.calls[-1][0] == post.file_url
    assert job.cancel()
    job.start(sample)
    runtime.finish("ready")
    retained = runtime.active_request.post_import.file_path
    assert retained != first and retained.suffix == ".gif" and retained.read_bytes() == GIF
    assert len(media.calls) == 2
    runtime.finish("imported")
    assert not list(tmp_path.iterdir())
