"""Bounded pool metadata presentation, independent of application navigation."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QObject, Property, Signal, Slot

from danbooru_multi_search.core.favorites import FavoriteIdentity, FavoriteObjectType
from danbooru_multi_search.core.pool_pages import (
    POOL_WINDOW_SIZE, PoolPageBatch, PoolPageSlot, PoolPageState, PoolPostMetadata,
)
from danbooru_multi_search.core.pools import DanbooruPool, PoolCategory, PoolRequestKind
from danbooru_multi_search.danbooru.pool_pages import PoolPageRequest, PoolPageRequestService
from danbooru_multi_search.danbooru.pools import PoolRequestService
from danbooru_multi_search.ui.favorites import FavoriteController, post_identity
from danbooru_multi_search.ui.pool_models import PoolPageListModel, membership_mapping


class PoolBrowserController(QObject):
    """Own current metadata and explicit windows, without changing query/history."""

    changed = Signal()
    membershipsChanged = Signal()
    poolOpened = Signal()

    def __init__(
        self, pools: PoolRequestService, memberships: PoolRequestService,
        pages: PoolPageRequestService, favorites: FavoriteController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._pools, self._memberships, self._pages = pools, memberships, pages
        self._favorites = favorites
        self._model = PoolPageListModel(self)
        self._generation = 0
        self._membership_revision = 0
        self._membership_post = 0
        self._membership_page = 1
        self._membership_values = ()
        self._membership_request = ""
        self._pool_request = ""
        self._pool_intent: int | None = None
        self._page_request = ""
        self._pool: DanbooruPool | None = None
        self._pool_target = 0
        self._start_page = 1
        self._closed = False
        self._active = False
        self._error = ""
        self._membership_error = ""
        self._cover = ""
        self._intent: tuple[int, tuple] | None = None
        pools.requestAccepted.connect(self._pool_accepted)
        pools.requestCompleted.connect(self._pool_completed)
        memberships.requestAccepted.connect(self._membership_accepted)
        memberships.requestCompleted.connect(self._membership_completed)
        pages.accepted.connect(self._pages_accepted)
        pages.completed.connect(self._pages_completed)
        self._model.changed.connect(self.changed)
        favorites.stateChanged.connect(self.changed)
        favorites.busyChanged.connect(self.changed)

    @Property(QObject, constant=True)
    def model(self):
        return self._model

    @property
    def current_pool(self) -> DanbooruPool | None:
        """Expose the confirmed pool for an independent reader session."""
        return self._pool

    def known_pools_for(self, post_id: int) -> tuple[DanbooruPool, ...]:
        """Return loaded membership information belonging to this specific post."""
        pools = self._membership_values if self._membership_post == post_id else ()
        current = self._pool
        if current and post_id in current.post_ids and all(pool.id != current.id for pool in pools):
            return (*pools, current)
        return pools

    @property
    def loaded_slots(self) -> tuple[PoolPageSlot, ...]:
        return self._model.slots

    @Property(int, notify=changed)
    def generation(self) -> int:
        return self._generation

    @Property(str, notify=changed)
    def errorText(self) -> str:
        return self._error

    @Property(str, notify=membershipsChanged)
    def membershipError(self) -> str:
        return self._membership_error

    @Property(int, notify=membershipsChanged)
    def membershipRevision(self) -> int:
        return self._membership_revision

    @Property(int, notify=membershipsChanged)
    def membershipPage(self) -> int:
        return self._membership_page

    @Property(bool, notify=membershipsChanged)
    def membershipBusy(self) -> bool:
        return self._memberships.busy

    @Property("QVariantList", notify=membershipsChanged)
    def memberships(self):
        return [membership_mapping(pool) for pool in self._membership_values]

    @Property("QVariantMap", notify=changed)
    def poolInfo(self):
        return membership_mapping(self._pool) if self._pool else {}

    @Property(str, notify=changed)
    def coverUrl(self) -> str:
        return self._cover

    @Property(int, notify=changed)
    def firstPage(self) -> int:
        return self._start_page

    @Property(bool, notify=changed)
    def busy(self) -> bool:
        return self._pools.busy or bool(self._page_request) or self._intent is not None

    @Property("QVariantMap", notify=changed)
    def poolFavoriteState(self):
        if not self._pool or self._pool.category is not PoolCategory.SERIES:
            return {"known": False, "saved": False, "enabled": False}
        state = self._favorites.state(FavoriteIdentity(FavoriteObjectType.POOL, str(self._pool.id)))
        return {**state, "enabled": state["known"] and not self._favorites.busy}

    def fetch_memberships(self, post_id: int, *, page: int = 1) -> None:
        if self._closed:
            return
        self._membership_revision += 1
        revision = self._membership_revision
        self._membership_post, self._membership_page = post_id, page
        self._membership_values = ()
        self._membership_request = ""
        self._membership_error = ""
        self.membershipsChanged.emit()
        if self._closed or revision != self._membership_revision:
            return
        self._memberships.fetch_memberships(post_id, page=page, limit=20)

    def _membership_accepted(self, identity) -> None:
        if not self._closed and identity.target_id == self._membership_post and identity.page == self._membership_page:
            self._membership_request = identity.request_id
            self.membershipsChanged.emit()

    def _membership_completed(self, outcome) -> None:
        if (
            self._closed or self._memberships.current_request is not outcome.identity
            or outcome.identity.target_id != self._membership_post
            or outcome.identity.page != self._membership_page
            or (self._membership_request and outcome.identity.request_id != self._membership_request)
        ):
            return
        self._membership_request = ""
        self._membership_values = outcome.memberships.pools if outcome.succeeded else ()
        self._membership_error = "" if outcome.succeeded else outcome.message
        self.membershipsChanged.emit()

    def membership_pool(self, key: str, revision: int) -> DanbooruPool | None:
        if self._closed or revision != self._membership_revision:
            return None
        return next((pool for pool in self._membership_values if str(pool.id) == key), None)

    @Slot(int, int)
    def showMembershipPage(self, page: int, revision: int) -> None:
        if revision == self._membership_revision and self._membership_post and not self._closed:
            self.fetch_memberships(self._membership_post, page=page)

    def cancel_memberships(self) -> None:
        self._membership_revision += 1
        self._membership_request = ""
        self._memberships.cancel_current()

    def open_pool(self, pool_id: int) -> None:
        if self._closed:
            return
        self._generation += 1
        generation = self._generation
        self._pool_target = pool_id
        self._pool = None
        self._pool_request = self._page_request = ""
        self._pool_intent = generation
        self._intent = None
        self._pages.cancel_current()
        self._error = self._cover = ""
        self._model.replace(())
        if not self._current(generation):
            return
        self._pools.fetch_pool(pool_id)

    def _pool_accepted(self, identity) -> None:
        if not self._closed and identity.kind is PoolRequestKind.POOL and identity.target_id == self._pool_target:
            self._pool_request = identity.request_id
            self.changed.emit()

    def _pool_completed(self, outcome) -> None:
        if (
            self._closed or self._pool_intent != self._generation
            or self._pools.current_request is not outcome.identity
            or outcome.identity.target_id != self._pool_target
            or (self._pool_request and outcome.identity.request_id != self._pool_request)
        ):
            return
        self._pool_request = ""
        self._pool_intent = None
        generation = self._generation
        if not outcome.succeeded:
            self._error = outcome.message
            self.changed.emit()
            return
        self._pool = outcome.pool
        if self._pool.is_deleted:
            self._error = "该作品池已删除。"
            self.changed.emit()
            return
        if self._pool.category is PoolCategory.SERIES:
            self._favorites.ensure((FavoriteIdentity(FavoriteObjectType.POOL, str(self._pool.id)),))
        if not self._current(generation):
            return
        self.poolOpened.emit()
        if self._current(generation):
            self.showWindow(1, generation)

    def _current(self, generation: int) -> bool:
        return not self._closed and generation == self._generation

    @Slot(int, int, result=bool)
    def showWindow(self, first_page: int, generation: int) -> bool:
        if not self._current(generation) or self._pool is None or self._pool.is_deleted:
            return False
        if type(first_page) is not int or not 1 <= first_page <= max(1, self._pool.post_count):
            return False
        self._generation += 1
        generation = self._generation
        self._start_page = first_page
        self._pages.cancel_current()
        self._page_request = ""
        self._intent = None
        self._error = ""
        references = tuple(self._pool.page_at(number) for number in range(
            first_page, min(first_page + POOL_WINDOW_SIZE, self._pool.post_count + 1),
        ))
        slots = tuple(PoolPageSlot(ref, PoolPostMetadata(ref.post_id, PoolPageState.LOADING)) for ref in references)
        self._model.replace(slots)
        if not self._current(generation) or not references:
            return self._current(generation)
        self._fetch_pages(references, generation)
        return True

    def _fetch_pages(self, references: tuple, generation: int) -> None:
        self._intent = (generation, references)
        self._pages.fetch_pages(references)

    def _pages_accepted(self, request: PoolPageRequest) -> None:
        if self._intent and self._current(self._intent[0]) and request.references == self._intent[1]:
            self._page_request = request.request_id
            self.changed.emit()

    def _pages_completed(self, request: PoolPageRequest, batch: PoolPageBatch | None, error: str, code: str) -> None:
        intent = self._intent
        if self._closed or intent is None or not self._current(intent[0]):
            return
        if request.references != intent[1] or (self._page_request and request.request_id != self._page_request):
            return
        self._intent = None
        self._page_request = ""
        generation = self._generation
        if batch is None:
            values = {ref.page_number: PoolPageSlot(ref, PoolPostMetadata(ref.post_id, PoolPageState.FAILED, message=error)) for ref in request.references}
            self._error = error
        else:
            values = {slot.reference.page_number: slot for slot in batch.slots}
        slots = tuple(values.get(slot.reference.page_number, slot) for slot in self._model.slots)
        model_revision = self._model.revision
        posts = tuple(slot.metadata.post for slot in slots if slot.metadata.post)
        self._favorites.ensure(tuple(post_identity(str(post.id)) for post in posts))
        if not self._current(generation) or self._model.revision != model_revision:
            return
        if not self._cover and self._start_page == 1 and posts:
            self._cover = posts[0].display_url
        self._model.replace(slots)

    @Slot(str, int, result=bool)
    def retryPage(self, key: str, revision: int) -> bool:
        slot = self.slot(key, revision)
        if slot is None or self.busy:
            return False
        generation = self._generation
        self._error = ""
        loading = replace(slot, metadata=PoolPostMetadata(slot.reference.post_id, PoolPageState.LOADING))
        slots = tuple(loading if value.key == key else value for value in self._model.slots)
        self._model.replace(slots)
        if not self._current(generation) or self._model.slots is not slots:
            return False
        self._fetch_pages((slot.reference,), generation)
        return True

    def slot(self, key: str, revision: int) -> PoolPageSlot | None:
        if self._closed or revision != self._model.revision:
            return None
        return self._model.by_key(key)

    @Slot(int, result="QVariantMap")
    def rowAt(self, row: int) -> dict:
        return self._model.mapping(row)

    @Slot(str, result="QVariantMap")
    def postState(self, key: str) -> dict:
        slot = self._model.by_key(key)
        state = self._favorites.state(post_identity(str(slot.reference.post_id))) if slot else {"known": False, "saved": False}
        return {**state, "enabled": bool(slot and slot.metadata.post) and state["known"] and not self._favorites.busy}

    @Slot(str, bool, int, result=bool)
    def setPostFavorite(self, key: str, desired: bool, revision: int) -> bool:
        slot = self.slot(key, revision)
        if not slot or not slot.metadata.post:
            return False
        return self._favorites.set_post(slot.metadata.post, desired, guard=lambda: self.slot(key, revision) is slot)

    @Slot(int, bool, result=bool)
    def setPoolFavorite(self, generation: int, desired: bool) -> bool:
        pool = self._pool
        if not pool or pool.category is not PoolCategory.SERIES or not self._current(generation):
            return False
        return self._favorites.set_pool(pool.id, pool.name, desired, guard=lambda: self._current(generation) and self._pool is pool)

    @Slot()
    def retryWindow(self) -> None:
        if self._pool:
            self.showWindow(self._start_page, self._generation)
        elif self._pool_target:
            self.open_pool(self._pool_target)

    @Slot()
    def cancel(self) -> None:
        if self._closed:
            return
        self._generation += 1
        generation = self._generation
        self._pool_request = ""
        self._pool_intent = None
        self._pools.cancel_current()
        if not self._current(generation):
            return
        self._pages.cancel_current()
        self._intent = None
        self._page_request = ""
        self._model.replace(tuple(replace(slot, metadata=PoolPostMetadata(
            slot.reference.post_id, PoolPageState.FAILED, message="页面请求已取消，可重试。",
        )) if slot.metadata.state is PoolPageState.LOADING else slot for slot in self._model.slots))

    def invalidate(self) -> None:
        self._generation += 1
        self.cancel_memberships()
        self.cancel()

    def shutdown(self) -> None:
        self._closed = True
        self._generation += 1
        self._pools.shutdown()
        self._memberships.shutdown()
        self._pages.shutdown()
