"""Coordinate a single Eagle import with the shared media and Eagle queues."""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, Signal, Slot

from danbooru_multi_search.app.eagle_import_types import EaglePostImportError, EaglePostImportRequest
from danbooru_multi_search.app.eagle_media import EagleMediaFile
from danbooru_multi_search.app.eagle_runtime import EagleRuntime
from danbooru_multi_search.app.eagle_service import EagleOperation, EagleRequest, EagleResult
from danbooru_multi_search.core.downloads import DanbooruDownloadOutcome, DownloadState
from danbooru_multi_search.danbooru.download import DanbooruDownloadController
from danbooru_multi_search.danbooru.models import MediaQuality


class EagleImportJob(QObject):
    """Hold the confirmed post and target until this user's operation finishes."""

    changed = Signal()

    def __init__(self, runtime: EagleRuntime, downloader: DanbooruDownloadController,
                 staging_root: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.runtime = runtime
        self._downloader = downloader
        self._root = staging_root
        self._media: EagleMediaFile | None = None
        self._request_id = ""
        self.payload: EaglePostImportRequest | None = None
        self.result: EagleResult | None = None
        self.file_notice = ""
        self.stage = ""
        self.cancelling = False
        self.closed = False
        runtime.completed.connect(self._eagle_finished)
        downloader.downloadCompleted.connect(self._download_finished)

    def start(self, payload: EaglePostImportRequest) -> bool:
        if self.closed or self.payload or self.runtime.busy or not self.runtime.availability.available:
            return False
        self.payload = payload
        self.result = None
        self.file_notice = ""
        self._submit(EagleOperation.PRECHECK_POST)
        return True

    def _submit(self, operation: EagleOperation) -> None:
        payload = self.payload
        request = EagleRequest(uuid4().hex, operation, post_import=payload)
        self._request_id = request.request_id
        self.stage = operation.value
        self.changed.emit()
        if self.closed or self.payload is not payload or self.cancelling:
            return
        if not self.runtime.start(request) and self._request_id == request.request_id:
            self._finish(EagleResult(request.request_id, error_code="eagle_queue_rejected"))

    @Slot(object)
    def _eagle_finished(self, result: EagleResult) -> None:
        if self.closed or self.payload is None or self._request_id != result.request_id:
            return
        self._apply_result(result)

    def _apply_result(self, result: EagleResult) -> None:
        self._request_id = ""
        outcome = result.import_result
        if outcome:
            for path in outcome.recovered_paths:
                try:
                    EagleMediaFile.release_recovered(self._root, path)
                except OSError as exc:
                    self.file_notice = f"已确认先前的导入，清理暂存文件失败（{path}）：{exc}"
        if (not self.closed and not result.error_code and not self.cancelling and self.stage == EagleOperation.PRECHECK_POST
                and outcome and outcome.state == "ready"):
            self._prepare_media()
        else:
            self._finish(result)

    def _prepare_media(self, quality: MediaQuality | None = None) -> None:
        post = self.payload.post
        choices = self.payload.media_choices
        if not choices:
            code = ("eagle_missing_original" if self.payload.requested_quality == MediaQuality.ORIGINAL
                    and self.payload.fallback_quality is None else "eagle_missing_media")
            self._finish(EagleResult("", error_code=code))
            return
        quality = quality or choices[0]
        self.payload = replace(self.payload, selected_quality=quality)
        try:
            self._media = EagleMediaFile.prepare(self._root, post, quality)
        except (OSError, ValueError) as exc:
            self._finish(EagleResult("", error_code="eagle_media_staging", diagnostic=str(exc)))
            return
        if self._media.recovered:
            self._import_file()
            return
        payload = self.payload
        self.stage = "download"
        self.changed.emit()
        if not self.closed and self.payload is payload and not self.cancelling:
            self._downloader.download_source(post.media_url(quality), self._media.path)

    @Slot(object)
    def _download_finished(self, outcome: DanbooruDownloadOutcome) -> None:
        if self.closed or self.payload is None or self.stage != "download":
            return
        if self.cancelling or outcome.state == DownloadState.CANCELLED:
            self._finish(EagleResult("", error_code="eagle_cancelled"))
        elif outcome.succeeded and outcome.downloaded_file and outcome.downloaded_file.path == self._media.path:
            self._import_file()
        elif (outcome.http_status in (404, 410) and self.payload.selected_quality == MediaQuality.ORIGINAL
              and len(self.payload.media_choices) == 2):
            self._release_file(retain=False)
            self._prepare_media(self.payload.media_choices[1])
        else:
            self._finish(EagleResult("", error_code="eagle_media_download", diagnostic=outcome.message))

    def _import_file(self) -> None:
        try:
            if self.payload.selected_quality != MediaQuality.ORIGINAL:
                self._media.name_rendition()
            self._media.capture_for_import()
        except (EaglePostImportError, OSError) as exc:
            self._finish(EagleResult("", error_code="eagle_file_invalid", diagnostic=str(exc)))
            return
        self.payload = replace(self.payload, file_path=self._media.path)
        self._submit(EagleOperation.IMPORT_POST)

    def _release_file(self, *, retain: bool, keep_path: bool = False) -> None:
        media = self._media
        self._media = None
        if media is None:
            return
        try:
            if keep_path:
                media.mark_stopped()
                self.file_notice = f"待核实的文件保存在：{media.path}"
            else:
                saved = media.finish(retain=retain)
                if saved:
                    self.file_notice = f"已保留文件供重试：{saved}"
        except OSError as exc:
            self.file_notice = f"暂存文件处理失败（{media.path}）：{exc}"

    def _finish(self, result: EagleResult) -> None:
        outcome = result.import_result
        confirmed = (not result.error_code and not result.uncertain and outcome is not None
                     and outcome.state in {"imported", "updated", "skipped"} and bool(outcome.item_identifier))
        invalid_media = result.error_code in {"eagle_file_invalid", "eagle_file_changed", "eagle_file_hash_failed"}
        self._release_file(retain=not confirmed and not invalid_media, keep_path=result.uncertain)
        pending_write = result.uncertain or result.error_code == "eagle_unresolved_attempt"
        if pending_write and not self.file_notice and outcome and outcome.file_path:
            self.file_notice = f"待核实的文件路径：{outcome.file_path}"
        self._request_id = ""
        self.result = result
        self.stage = ""
        self.cancelling = False
        self.payload = None
        if not self.closed:
            self.changed.emit()

    def cancel(self) -> bool:
        if self.closed or self.payload is None or self.cancelling:
            return False
        payload = self.payload
        self.cancelling = True
        active = self.runtime.active_request
        if active and active.request_id == self._request_id:
            self.runtime.cancel()
        else:
            if self.stage == "download":
                self._downloader.cancelCurrentDownload()
            if self.payload is payload:
                self._finish(EagleResult("", error_code="eagle_cancelled"))
        self.changed.emit()
        return True

    def begin_shutdown(self) -> None:
        self.closed = True
        self._downloader.shutdown()

    def finish_shutdown(self) -> None:
        """The session calls this after the Eagle worker has stopped reading."""
        result = self.runtime.stopped_import_result(self._request_id)
        if result is not None:
            self._apply_result(result)
            return
        if self.runtime.shutdown_completed or self.stage != EagleOperation.IMPORT_POST:
            self._release_file(retain=True, keep_path=self.stage == EagleOperation.IMPORT_POST)
        self.payload = None
        self._request_id = ""
        self.stage = ""
