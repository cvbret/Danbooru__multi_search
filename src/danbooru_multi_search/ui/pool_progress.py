"""Reading-progress persistence, restore preparation and two-phase closing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from PySide6.QtCore import QObject, QTimer

from danbooru_multi_search.core.pool_progress import (
    PoolProgressError, PoolReadingProgress, normalize_api_source, resolve_pool_progress,
)
from danbooru_multi_search.core.pool_reader import ReaderMode
from danbooru_multi_search.core.pools import DanbooruPool
from danbooru_multi_search.storage.pool_progress import PoolProgressRepository
from danbooru_multi_search.ui.pool_reader import PoolReaderController


@dataclass(slots=True)
class _ProgressSession:
    pool: DanbooruPool
    number: int
    expected: PoolReadingProgress | None = None
    read_ok: bool = False
    blocked: bool = False
    reload_needed: bool = False
    restore: PoolReadingProgress | None = None
    pending: PoolReadingProgress | None = None
    last_content: tuple | None = None


def _content(value: PoolReadingProgress) -> tuple:
    return (value.source, value.pool_id, value.post_id, value.page_number, value.offset,
            value.viewport_width, value.viewport_height, value.zoom, value.mode, value.scale_mode)


class PoolProgressController(QObject):
    """Save only known, laid-out positions; a failed read never means absence.

    The reader announces session boundaries before replacing its pool. Normal
    changes share one debounce timer. Closing stops it, captures one immutable
    position, then waits for the launcher's existing import shutdown before the
    final nonblocking transaction. This controller never starts network work.
    """

    def __init__(
        self, repository: PoolProgressRepository, reader: PoolReaderController,
        base_url: str, parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._repository, self._reader = repository, reader
        self._source = normalize_api_source(base_url)
        self._session: _ProgressSession | None = None
        self._closing = False
        self._closed = False
        self._final: tuple[_ProgressSession, PoolReadingProgress] | None = None
        self._last_error: PoolProgressError | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self.flush)
        reader.sessionEnding.connect(self._ending)
        reader.sessionOpened.connect(self._opened)
        reader.changed.connect(self._changed)
        reader.positionChanged.connect(self._changed)
        reader.layoutReady.connect(self._layout_ready)
        reader.retryProgressRequested.connect(self.retry)
        reader.restartRequested.connect(self._restart)

    def _current(self, session: _ProgressSession, *, closing: bool = False) -> bool:
        return (not self._closed and (closing or not self._closing)
                and self._session is session and self._reader.readingSession == session.number
                and self._reader.current_pool is session.pool)

    def _status(self, session: _ProgressSession, message: str) -> None:
        if self._current(session):
            self._reader.set_progress_status(message)

    def _opened(self, pool: DanbooruPool, number: int) -> None:
        if self._closing or self._closed:
            return
        self._timer.stop()
        session = _ProgressSession(pool, number)
        self._session = session
        self._load(session)

    def _load(self, session: _ProgressSession) -> None:
        if not self._current(session) or not self._reader.begin_progress_restore(session.number):
            return
        self._timer.stop()
        session.pending = None
        session.read_ok = False
        generation = self._reader.lifecycle_generation
        try:
            record = self._repository.get(self._source, session.pool.id)
        except PoolProgressError as exc:
            if self._current(session) and generation == self._reader.lifecycle_generation:
                session.read_ok = False
                session.reload_needed = True
                session.pending = None
                self._status(session, f"无法读取阅读进度，本次不会覆盖已有记录：{exc}")
            return
        if not self._current(session) or generation != self._reader.lifecycle_generation:
            return
        session.expected, session.read_ok = record, True
        session.blocked = session.reload_needed = False
        session.restore = record
        session.pending = None
        session.last_content = _content(record) if record else None
        if record and session.pool.post_count:
            position = resolve_pool_progress(record, session.pool, viewport_width=0,
                zoom=self._reader.zoom, mode=ReaderMode(self._reader.mode), scale_mode=self._reader.scaleMode)
            if position:
                self._reader.restore_position(position.page_number, 0, session.number)
        elif session.pool.post_count:
            self._reader.restore_position(self._reader.currentPage, self._reader.scrollOffset, session.number)
        if self._current(session) and generation == self._reader.lifecycle_generation:
            self._status(session, "")

    def _layout_ready(self, width: float, height: float, number: int) -> None:
        session = self._session
        if session is None or not self._current(session) or session.number != number or session.restore is None:
            return
        record = session.restore
        position = resolve_pool_progress(record, session.pool, viewport_width=width,
            viewport_height=height, zoom=self._reader.zoom, mode=ReaderMode(self._reader.mode),
            scale_mode=self._reader.scaleMode)
        session.restore = None
        if position and position.page_number == self._reader.currentPage:
            self._reader.apply_restore_offset(position.offset, number)

    def _capture(self, session: _ProgressSession) -> None:
        if not self._current(session) or not session.read_ok or not self._reader.position_ready or not session.pool.post_count:
            return
        width, height = self._reader.viewport_size
        page = self._reader.currentPage
        try:
            value = PoolReadingProgress(
                self._source, session.pool.id, session.pool.post_ids[page - 1], page,
                datetime.now(timezone.utc), offset=max(0.0, self._reader.scrollOffset),
                viewport_width=width, viewport_height=height, zoom=self._reader.zoom,
                mode=ReaderMode(self._reader.mode), scale_mode=self._reader.scaleMode,
            )
        except PoolProgressError as exc:
            session.blocked = True
            self._status(session, f"阅读进度无法保存：{exc}")
            return
        if _content(value) == session.last_content:
            session.pending = None
        else:
            session.pending = value

    def _changed(self) -> None:
        session = self._session
        if session is None or not self._current(session):
            return
        before = _content(session.pending) if session.pending else None
        self._capture(session)
        if not self._current(session):
            return
        if not self._reader.active:
            self.flush()
        elif session.pending and not session.blocked and (_content(session.pending) != before or not self._timer.isActive()):
            self._timer.start()

    def _save(self, session: _ProgressSession, value: PoolReadingProgress, *, closing: bool = False) -> bool:
        if not self._current(session, closing=closing) or not session.read_ok:
            return False
        try:
            saved = self._repository.save(value, expected=session.expected)
        except PoolProgressError as exc:
            if self._current(session, closing=closing):
                self._last_error = exc
                session.blocked = True
                session.reload_needed = exc.code == "stale_progress"
                self._status(session, f"阅读进度未能保存，已有记录已保留：{exc}")
            return False
        if not self._current(session, closing=closing):
            return False
        session.expected = saved
        session.last_content = _content(saved)
        session.pending = None
        session.blocked = False
        self._last_error = None
        self._status(session, "")
        return True

    def flush(self) -> bool:
        """Persist the current known snapshot; no-op after begin_shutdown."""
        self._timer.stop()
        session = self._session
        if (session is None or not self._current(session) or not session.pending
                or session.blocked or not self._reader.position_ready):
            return False
        return self._save(session, session.pending)

    def _ending(self) -> None:
        session = self._session
        if session and self._current(session):
            self._capture(session)
            self.flush()
            if self._session is session:
                self._session = None

    def retry(self) -> None:
        """Retry a failed read first; retry writes only against known progress."""
        session = self._session
        if session is None or not self._current(session):
            return
        if not session.read_ok or session.reload_needed:
            self._load(session)
        else:
            session.blocked = False
            self._capture(session)
            self.flush()

    def _restart(self) -> None:
        if self._session and self._current(self._session):
            self._session.restore = None

    def begin_shutdown(self) -> None:
        """Capture before reader/media shutdown; do not touch a worker's DB lock."""
        if self._closing or self._closed:
            return
        session = self._session
        if session and self._current(session):
            self._capture(session)
            if session.pending and session.read_ok:
                self._final = (session, session.pending)
        self._timer.stop()
        self._closing = True

    def finish_shutdown(self) -> None:
        """Run after the existing import worker has completed its transaction."""
        if self._closed:
            return
        self.begin_shutdown()
        try:
            if self._final:
                session, value = self._final
                if not self._save(session, value, closing=True) and self._last_error:
                    raise self._last_error
        finally:
            self._timer.stop()
            self._closed = True
