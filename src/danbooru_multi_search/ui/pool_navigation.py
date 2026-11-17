"""One pool workspace and its outer detail source; no global navigation stack."""

from __future__ import annotations

from uuid import uuid4

from PySide6.QtCore import QObject, Property, Signal, Slot

from danbooru_multi_search.ui.detail_navigation import DetailNavigationController, DetailReturnState
from danbooru_multi_search.ui.pools import PoolBrowserController


class PoolNavigationController(QObject):
    changed = Signal()
    membershipsRequested = Signal()
    poolPageRequested = Signal()
    sourceRestored = Signal()

    def __init__(
        self, browser: PoolBrowserController, navigation: DetailNavigationController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._browser = browser
        self._navigation = navigation
        self._detail = navigation.detail_controller
        self._origin: DetailReturnState | None = None
        self._token = ""
        self._detail_revision = 0
        self._membership_source = None
        self._internal = 0
        self._closed = False
        self._expected_navigation = navigation.generation
        self._detail.openChanged.connect(self._detail_changed)
        navigation.sourceChanged.connect(self._navigation_changed)
        navigation.tagSearchRequested.connect(self.invalidate)

    @Property(str, notify=changed)
    def detailPostKey(self) -> str:
        post = self._detail.current_post
        return str(post.id) if self._detail.isOpen and post is not None else ""

    @Property(int, notify=changed)
    def detailRevision(self) -> int:
        return self._detail_revision

    @Property(str, notify=changed)
    def returnToken(self) -> str:
        return self._token

    @Property(str, notify=changed)
    def sourceLabel(self) -> str:
        return f"返回来源作品 #{self._origin.post.id}" if self._origin else "返回来源作品"

    def _detail_changed(self) -> None:
        if self._closed:
            return
        self._detail_revision += 1
        if not self._internal:
            self._browser.cancel_memberships()
            self._membership_source = None
            if self._token:
                self.invalidate()
        self.changed.emit()

    def _navigation_changed(self) -> None:
        if not self._closed and not self._internal and self._token:
            self.invalidate()

    @Slot(str, int, result=bool)
    def requestDetailMemberships(self, key: str, revision: int) -> bool:
        if self._closed or revision != self._detail_revision or key != self.detailPostKey or not key:
            return False
        post = self._detail.current_post
        source = (post, revision, self._navigation.generation)
        self._membership_source = source
        self.membershipsRequested.emit()
        if self._closed or self._membership_source is not source or not self._detail_current(source):
            return False
        self._browser.fetch_memberships(post.id)
        return True

    def _detail_current(self, source: tuple) -> bool:
        return (
            self._detail.isOpen and self._detail.current_post is source[0]
            and self._detail_revision == source[1]
            and self._navigation.generation == source[2]
        )

    @Slot()
    def cancelMemberships(self) -> None:
        self._membership_source = None
        self._browser.cancel_memberships()

    @Slot(str, int, result=bool)
    def openMembership(self, key: str, revision: int) -> bool:
        source = self._membership_source
        pool = self._browser.membership_pool(key, revision)
        if self._closed or source is None or pool is None or not self._detail_current(source):
            return False
        if self._origin is None:
            self._origin = self._navigation.capture_return_state()
            self._token = uuid4().hex
        token = self._token
        navigation_generation = self._navigation.generation
        detail_revision = self._detail_revision
        self._internal += 1
        try:
            closed = self._navigation.closeDetail()
        finally:
            self._internal -= 1
        if (
            self._closed or self._token != token or not closed or self._detail.isOpen
            or self._navigation.generation != navigation_generation + 1
            or self._detail_revision != detail_revision + 1
        ):
            self.invalidate()
            return False
        self._expected_navigation = navigation_generation + 1
        self._membership_source = None
        self._browser.cancel_memberships()
        self.poolPageRequested.emit()
        if self._closed or self._token != token or self._detail.isOpen:
            return False
        self._browser.open_pool(pool.id)
        self.changed.emit()
        return not self._closed and self._token == token

    @Slot(str, int, result=bool)
    def openPage(self, key: str, revision: int) -> bool:
        slot = self._browser.slot(key, revision)
        token = self._token
        if self._closed or not token or slot is None or slot.metadata.post is None:
            return False
        navigation_generation = self._navigation.generation
        detail_revision = self._detail_revision
        self._internal += 1
        try:
            opened = self._navigation.open_saved_post(slot.metadata.post, source_page="pool")
        finally:
            self._internal -= 1
        if (
            not opened or self._closed or self._token != token
            or self._navigation.generation != navigation_generation + 1
            or self._detail_revision != detail_revision + 1
            or self._detail.current_post is not slot.metadata.post
            or self._navigation.sourcePage != "pool"
        ):
            if not self._closed:
                self.invalidate()
            return False
        self._expected_navigation = navigation_generation + 1
        return True

    @Slot(result=bool)
    def returnToPool(self) -> bool:
        if self._closed or not self._token or self._navigation.sourcePage != "pool":
            return False
        token = self._token
        navigation_generation = self._navigation.generation
        detail_revision = self._detail_revision
        self._internal += 1
        try:
            closed = self._navigation.closeDetail()
        finally:
            self._internal -= 1
        if (
            not closed or self._closed or token != self._token or self._detail.isOpen
            or self._navigation.generation != navigation_generation + 1
            or self._detail_revision != detail_revision + 1
        ):
            if not self._closed:
                self.invalidate()
            return False
        self._expected_navigation = navigation_generation + 1
        self.poolPageRequested.emit()
        return not self._closed and token == self._token

    @Slot(str, result=bool)
    def returnToSource(self, token: str) -> bool:
        if self._closed or not token or token != self._token or self._origin is None or self._detail.isOpen:
            return False
        state = self._origin
        navigation_generation = self._navigation.generation
        detail_revision = self._detail_revision
        self._internal += 1
        try:
            opened = self._navigation.restore_return_state(state, expected_generation=self._expected_navigation)
        finally:
            self._internal -= 1
        if (
            not opened or self._closed or self._detail.current_post is not state.post
            or self._token != token
            or self._navigation.generation != navigation_generation + 1
            or self._detail_revision != detail_revision + 1
        ):
            if not self._closed:
                self.invalidate()
            return False
        self._origin = None
        self._token = ""
        self.sourceRestored.emit()
        self.changed.emit()
        return True

    @Slot()
    def invalidate(self) -> None:
        if self._closed:
            return
        self._origin = None
        self._token = ""
        self._membership_source = None
        self._browser.invalidate()
        self.changed.emit()

    def shutdown(self) -> None:
        self._closed = True
        self._origin = None
        self._token = ""
        self._membership_source = None
