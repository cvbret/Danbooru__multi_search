"""Reader state, bounded page scheduling and current-page commands."""

from __future__ import annotations

import math

from PySide6.QtCore import QObject, Property, Signal, Slot

from danbooru_multi_search.core.pool_pages import PoolPageSlot, PoolPageState, PoolPostMetadata
from danbooru_multi_search.core.pool_reader import (
    READER_DIRECTORY_SIZE, READER_MEDIA_RADIUS, ReaderMode, reader_range,
)
from danbooru_multi_search.core.pools import DanbooruPool, PoolCategory
from danbooru_multi_search.ui.details import DanbooruDetailController
from danbooru_multi_search.danbooru.pool_pages import PoolPageRequestService
from danbooru_multi_search.danbooru.tag_translation import TagTranslationStore
from danbooru_multi_search.ui.downloads import DownloadUiController
from danbooru_multi_search.ui.favorites import FavoriteController, post_identity
from danbooru_multi_search.ui.pool_reader_models import PoolReaderListModel


class PoolReaderController(QObject):
    """Own one reader session; metadata and media stay separate from navigation."""

    changed = Signal()
    positionRequested = Signal(int)
    detailRequested = Signal(object, str)
    downloadPrepared = Signal(str)
    sessionEnding = Signal()
    sessionOpened = Signal(object, int)
    positionChanged = Signal()
    layoutReady = Signal(float, float, int)
    retryProgressRequested = Signal()
    restartRequested = Signal()

    def __init__(
        self, pages: PoolPageRequestService, favorites: FavoriteController,
        downloads: DownloadUiController, translations: TagTranslationStore,
        base_url: str, parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._pages, self._favorites, self._downloads = pages, favorites, downloads
        self._translations, self._base_url = translations, base_url
        self._model = PoolReaderListModel(self)
        self._generation = 0
        self._revision = 0
        self._page = 1
        self._scroll_offset = 0.0
        self._reading_session = 0
        self._position_pending = True
        self._viewport_width = 0.0
        self._viewport_height = 0.0
        self._progress_status = ""
        self._mode = ReaderMode.CONTINUOUS
        self._zoom = 1.0
        self._scale_mode = "fit-width"
        self._active = False
        self._closed = False
        self._directory = False
        self._directory_start = 1
        self._intent = None
        self._request_id = ""
        self._retry_pages: tuple[int, ...] = ()
        self._media: dict[int, DanbooruDetailController] = {}
        self._error = ""
        pages.accepted.connect(self._accepted)
        pages.completed.connect(self._completed)
        favorites.stateChanged.connect(self.changed)
        favorites.busyChanged.connect(self.changed)

    @Property(QObject, constant=True)
    def model(self) -> PoolReaderListModel:
        return self._model

    @Property(int, notify=changed)
    def revision(self) -> int:
        return self._revision

    @Property(int, notify=changed)
    def currentPage(self) -> int:
        return self._page

    @Property(float, notify=changed)
    def scrollOffset(self) -> float:
        return self._scroll_offset

    @Property(int, notify=changed)
    def totalPages(self) -> int:
        return self._model.rowCount()

    @Property(str, notify=changed)
    def mode(self) -> str:
        return self._mode.value

    @Property(float, notify=changed)
    def zoom(self) -> float:
        return self._zoom

    @Property(str, notify=changed)
    def scaleMode(self) -> str:
        return self._scale_mode

    @Property(bool, notify=changed)
    def active(self) -> bool:
        return self._active

    @Property(str, notify=changed)
    def errorText(self) -> str:
        return self._error or self._progress_status or self._favorites.noticeText

    @Property(str, notify=changed)
    def progressStatus(self) -> str:
        return self._progress_status

    @Property(int, notify=changed)
    def readingSession(self) -> int:
        return self._reading_session

    @property
    def current_pool(self) -> DanbooruPool | None:
        return self._model.pool

    @property
    def position_ready(self) -> bool:
        return not self._position_pending

    @property
    def lifecycle_generation(self) -> int:
        return self._generation

    @property
    def viewport_size(self) -> tuple[float, float]:
        return self._viewport_width, self._viewport_height

    def set_progress_status(self, message: str) -> None:
        if not self._closed and message != self._progress_status:
            self._progress_status = message
            self.changed.emit()

    @Property("QVariantMap", notify=changed)
    def current(self) -> dict:
        return self._model.mapping(self._page)

    @Property(int, notify=changed)
    def directoryStart(self) -> int:
        return self._directory_start

    @Property("QVariantList", notify=changed)
    def directoryRows(self) -> list:
        return [self._model.mapping(number) for number in self._directory_numbers()]

    def open_pool(self, pool: DanbooruPool, slots: tuple[PoolPageSlot, ...] = ()) -> bool:
        """Open a series at page one; this does not persist reading progress."""
        if self._closed or pool.category is not PoolCategory.SERIES or pool.is_deleted:
            return False
        previous_generation = self._generation
        self.sessionEnding.emit()
        if self._closed or self._generation != previous_generation:
            return False
        self._generation += 1
        self._reading_session += 1
        self._position_pending = True
        generation = self._generation
        self._revision += 1
        self._active = False
        self._pages.cancel_current()
        self._intent = None
        self._retry_pages = ()
        self._release_media()
        if self._closed or generation != self._generation:
            return False
        self._page, self._directory_start = 1, 1
        self._scroll_offset = 0.0
        self._directory = False
        self._error = ""
        self._model.set_pool(pool)
        if self._closed or generation != self._generation:
            return False
        self._model.update(slots, set())
        if self._closed or generation != self._generation:
            return False
        self.sessionOpened.emit(pool, self._reading_session)
        if self._closed or generation != self._generation:
            return False
        self.changed.emit()
        return not self._closed and generation == self._generation

    @Slot(bool)
    def setActive(self, active: bool) -> None:
        if self._closed or self._active == active:
            return
        self._generation += 1
        generation = self._generation
        self._revision += 1
        self._active = active
        if active:
            self._position_pending = True
        self._pages.cancel_current()
        self._intent = None
        self._request_id = ""
        if not active:
            self._directory = False
            self._retry_pages = ()
            self._release_media()
        else:
            self._sync_media()
        if self._closed or generation != self._generation:
            return
        self.changed.emit()
        if active:
            if self._closed or generation != self._generation or not self._active:
                return
            self.positionRequested.emit(self._page)
            self._pump()

    def _directory_numbers(self) -> tuple[int, ...]:
        if not self._directory:
            return ()
        return tuple(range(self._directory_start, min(self.totalPages + 1, self._directory_start + READER_DIRECTORY_SIZE)))

    def _protected(self) -> set[int]:
        return set(reader_range(self._page, self.totalPages)) | set(self._directory_numbers()) if self.totalPages else set()

    def _pump(self) -> None:
        if self._closed or not self._active or self._intent or not self.totalPages:
            return
        numbers = self._retry_pages
        self._retry_pages = ()
        if not numbers:
            wanted = tuple(dict.fromkeys(reader_range(self._page, self.totalPages) + self._directory_numbers()))
            numbers = tuple(number for number in wanted if number not in self._model.cache)[:5]
        if not numbers:
            return
        refs = tuple(self._model.pool.page_at(number) for number in numbers)
        self._intent = (self._generation, refs)
        self._request_id = ""
        self._pages.fetch_pages(refs)

    def _accepted(self, request) -> None:
        if self._intent and self._intent == (self._generation, request.references) and self._active:
            self._request_id = request.request_id

    def _completed(self, request, batch, error: str, code: str) -> None:
        if (
            self._closed or not self._active or self._intent != (self._generation, request.references)
            or (self._request_id and self._request_id != request.request_id)
        ):
            return
        generation = self._generation
        self._intent, self._request_id = None, ""
        if batch is None:
            slots = tuple(PoolPageSlot(ref, PoolPostMetadata(ref.post_id, PoolPageState.FAILED, message=error)) for ref in request.references)
            self._error = error
        else:
            slots = batch.slots
        self._model.update(slots, self._protected())
        if self._closed or generation != self._generation or not self._active:
            return
        posts = tuple(slot.metadata.post for slot in slots if slot.metadata.post)
        self._favorites.ensure(tuple(post_identity(str(post.id)) for post in posts))
        if self._closed or generation != self._generation or not self._active:
            return
        self._sync_media()
        self.changed.emit()
        if generation == self._generation:
            self._pump()

    def _release_media(self) -> None:
        old, self._media = self._media, {}
        for media in old.values():
            media.shutdown()
            media.deleteLater()

    def _sync_media(self) -> None:
        generation, revision, page = self._generation, self._revision, self._page

        def current() -> bool:
            return (not self._closed and self._active and generation == self._generation
                    and revision == self._revision and page == self._page)

        wanted = set(reader_range(self._page, self.totalPages, READER_MEDIA_RADIUS)) if self._active and self.totalPages else set()
        for number in tuple(self._media):
            if not current():
                return
            slot = self._model.slot(number)
            if number not in wanted or slot is None or self._media[number].current_post is not slot.metadata.post:
                media = self._media.pop(number, None)
                if media is not None:
                    media.shutdown()
                    media.deleteLater()
                if not current():
                    return
        for number in sorted(wanted):
            if not current():
                return
            slot = self._model.slot(number)
            if slot and slot.metadata.post and number not in self._media:
                media = DanbooruDetailController(self._base_url, self._translations, parent=self)
                self._media[number] = media
                media.open_post(slot.metadata.post)
                if not current():
                    if self._media.get(number) is media:
                        self._media.pop(number)
                        media.shutdown()
                        media.deleteLater()
                    return

    @Slot(int, result="QVariant")
    def mediaFor(self, page: int) -> DanbooruDetailController | None:
        """Return a media object or an empty variant for a not-yet-loaded page."""
        return self._media.get(page) if self._active and not self._closed else None

    @Slot(int, result="QVariantMap")
    def pageAt(self, page: int) -> dict:
        return self._model.mapping(page)

    @Slot(int, int, result=bool)
    def goToPage(self, page: int, revision: int) -> bool:
        return self._change_page(page, revision, position=True)

    def _change_page(self, page: int, revision: int, *, position: bool) -> bool:
        if self._closed or not self._active or revision != self._revision or type(page) is not int or not 1 <= page <= self.totalPages:
            return False
        self._revision += 1
        current_revision = self._revision
        self._page = page
        if position:
            self._scroll_offset = 0.0
            self._position_pending = True
        generation = self._generation
        self._sync_media()
        self.changed.emit()
        if self._closed or generation != self._generation or self._page != page or current_revision != self._revision:
            return False
        if position:
            self.positionRequested.emit(page)
        self._pump()
        return True

    @Slot(int)
    def observePage(self, page: int) -> None:
        if page != self._page:
            self._change_page(page, self._revision, position=False)

    @Slot(int, float)
    def rememberScroll(self, page: int, offset: float) -> None:
        """Keep only the in-memory continuous anchor while visiting details."""
        if (not self._closed and self._active and not self._position_pending
                and page == self._page and math.isfinite(offset)):
            self._scroll_offset = max(0.0, offset)
            self.positionChanged.emit()

    def restore_position(self, page: int, offset: float, session: int) -> bool:
        """Apply a loaded record without starting requests before activation."""
        if self._closed or session != self._reading_session or not 1 <= page <= self.totalPages:
            return False
        self._page, self._scroll_offset = page, offset
        self._position_pending = True
        self._revision += 1
        revision = self._revision
        self.changed.emit()
        if self._closed or session != self._reading_session or revision != self._revision:
            return False
        if self._active:
            self._sync_media()
            self.positionRequested.emit(page)
            self._pump()
        return True

    def begin_progress_restore(self, session: int) -> bool:
        """Block writes before any progress-loading status notification."""
        if self._closed or session != self._reading_session:
            return False
        self._position_pending = True
        self._revision += 1
        return True

    @Slot(float, float, int)
    def changeViewport(self, width: float, height: float, session: int) -> None:
        if (self._closed or not self._active or session != self._reading_session
                or not 0 < width <= 100000 or not 0 < height <= 100000):
            return
        width_changed = abs(self._viewport_width - width) > 0.5
        height_changed = abs(self._viewport_height - height) > 0.5
        if not width_changed and not height_changed:
            return
        self._viewport_width, self._viewport_height = width, height
        if width_changed or (height_changed and self._scale_mode == "fit-window"):
            self._scroll_offset = 0
        self._position_pending = True
        self._revision += 1
        revision = self._revision
        self.changed.emit()
        if not self._closed and self._active and session == self._reading_session and revision == self._revision:
            self.positionRequested.emit(self._page)

    def apply_restore_offset(self, offset: float, session: int) -> None:
        if not self._closed and session == self._reading_session and self._position_pending:
            self._scroll_offset = offset

    @Slot(float, float, int, int, result="QVariantMap")
    def preparePosition(self, width: float, height: float, session: int, revision: int) -> dict:
        if (self._closed or not self._active or session != self._reading_session
                or revision != self._revision or not 0 < width <= 100000 or not 0 < height <= 100000):
            return {"valid": False}
        self._viewport_width, self._viewport_height = width, height
        self.layoutReady.emit(width, height, session)
        return {"valid": not self._closed and session == self._reading_session and revision == self._revision,
                "offset": self._scroll_offset}

    @Slot(int, int)
    def confirmPosition(self, session: int, revision: int) -> None:
        if self._closed or not self._active or session != self._reading_session or revision != self._revision:
            return
        self._position_pending = False
        self.positionChanged.emit()

    @Slot(int, result=bool)
    def restartFromBeginning(self, revision: int) -> bool:
        if self._closed or not self._active or revision != self._revision:
            return False
        self.restartRequested.emit()
        if self._closed or not self._active or revision != self._revision:
            return False
        return self.goToPage(1, revision)

    @Slot()
    def retryProgress(self) -> None:
        if not self._closed:
            self.retryProgressRequested.emit()

    @Slot(str, int, result=bool)
    def setMode(self, mode: str, revision: int) -> bool:
        if self._closed or not self._active or revision != self._revision:
            return False
        try:
            selected = ReaderMode(mode)
        except ValueError:
            return False
        self._mode = selected
        self._position_pending = True
        self._revision += 1
        generation, current_revision = self._generation, self._revision
        self.changed.emit()
        if self._closed or not self._active or generation != self._generation or current_revision != self._revision:
            return False
        self.positionRequested.emit(self._page)
        return True

    @Slot(float, int, result=bool)
    def setZoom(self, zoom: float, revision: int) -> bool:
        if self._closed or not self._active or revision != self._revision or not 0.05 <= zoom <= 8:
            return False
        self._zoom, self._scale_mode = zoom, "manual"
        self.changed.emit()
        return True

    @Slot(str, int, result=bool)
    def fit(self, mode: str, revision: int) -> bool:
        if self._closed or revision != self._revision or mode not in {"fit-width", "fit-window"}:
            return False
        self._scale_mode, self._zoom = mode, 1.0
        self.changed.emit()
        return True

    @Slot(bool)
    def setDirectoryOpen(self, opened: bool) -> None:
        if self._closed or not self._active:
            return
        self._directory = opened
        self.changed.emit()
        self._pump()

    @Slot(int, int, result=bool)
    def showDirectory(self, first_page: int, revision: int) -> bool:
        if self._closed or not self._active or revision != self._revision or not 1 <= first_page <= self.totalPages:
            return False
        self._directory_start = first_page
        self.changed.emit()
        self._pump()
        return True

    @Slot(str, int, result=bool)
    def retryPage(self, key: str, revision: int) -> bool:
        slot = self._current_slot(key, revision)
        if slot is None or self._intent:
            return False
        self._revision += 1
        self._error = ""
        self._retry_pages = (slot.reference.page_number,)
        self._pump()
        return True

    def _current_slot(self, key: str, revision: int) -> PoolPageSlot | None:
        if self._closed or not self._active or revision != self._revision:
            return None
        slot = self._model.slot(self._page)
        return slot if slot and slot.key == key else None

    @Property("QVariantMap", notify=changed)
    def favoriteState(self) -> dict:
        slot = self._model.slot(self._page)
        state = self._favorites.state(post_identity(str(slot.reference.post_id))) if slot else {"known": False, "saved": False}
        return {**state, "enabled": bool(slot and slot.metadata.post and self._active) and state["known"] and not self._favorites.busy}

    @Slot(str, int, bool, result=bool)
    def setFavorite(self, key: str, revision: int, desired: bool) -> bool:
        slot = self._current_slot(key, revision)
        return bool(slot and slot.metadata.post and self._favorites.set_post(
            slot.metadata.post, desired, guard=lambda: self._current_slot(key, revision) is slot,
        ))

    @Slot(str, int, result=str)
    def prepareDownload(self, key: str, revision: int) -> str:
        slot = self._current_slot(key, revision)
        if slot is None or slot.metadata.post is None:
            return ""
        token = self._downloads.capture_snapshot(slot.metadata.post, guard=lambda: self._current_slot(key, revision) is slot)
        if token:
            self.downloadPrepared.emit(token)
        return token

    @Slot(str, int, result=bool)
    def openDetail(self, key: str, revision: int) -> bool:
        slot = self._current_slot(key, revision)
        if slot is None or slot.metadata.post is None:
            return False
        self.detailRequested.emit(slot.metadata.post, key)
        return True

    def shutdown(self) -> None:
        if self._closed:
            return
        self.setActive(False)
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        self._pages.shutdown()
        self._release_media()
