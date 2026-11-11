"""Source-page navigation and gallery return state for the shared detail view."""

from __future__ import annotations

from dataclasses import dataclass
import math

from PySide6.QtCore import QObject, Property, Signal, Slot

from danbooru_multi_search.danbooru.details import DanbooruDetailController
from danbooru_multi_search.danbooru.gallery import DanbooruPostListModel
from danbooru_multi_search.danbooru.models import DanbooruPost


@dataclass(frozen=True, slots=True)
class GalleryViewSnapshot:
    model: DanbooruPostListModel
    revision: int
    count: int
    post_id: int
    index: int
    content_y: float
    grid_width: float
    grid_height: float
    window_width: float
    window_height: float


class DetailNavigationController(QObject):
    """Keep page-return behavior outside work metadata and media presentation."""

    sourceChanged = Signal()
    gallerySnapshotChanged = Signal()
    tagSearchRequested = Signal(str)

    def __init__(
        self,
        detail_controller: DanbooruDetailController,
        browse_model: DanbooruPostListModel,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._detail_controller = detail_controller
        self._browse_model = browse_model
        self._gallery_snapshot: GalleryViewSnapshot | None = None
        self._source_page = "browse"
        self._generation = 0
        self._closed = False
        detail_controller.tagSearchRequested.connect(self._on_tag_search)

    @property
    def detail_controller(self) -> DanbooruDetailController:
        return self._detail_controller

    @property
    def gallery_snapshot(self) -> GalleryViewSnapshot | None:
        return self._gallery_snapshot

    @Property(str, notify=sourceChanged)
    def sourcePage(self) -> str:
        return self._source_page

    @Slot(int, float, int, float, float, float, float, result=bool)
    def openBrowsePost(
        self,
        row: int,
        content_y: float,
        current_index: int,
        grid_width: float,
        grid_height: float,
        window_width: float,
        window_height: float,
    ) -> bool:
        if self._closed:
            return False
        post = self._browse_model.post_at(row)
        if post is None:
            return False
        values = (content_y, grid_width, grid_height, window_width, window_height)
        if not all(
            isinstance(value, (int, float)) and math.isfinite(value)
            for value in values
        ):
            return False
        snapshot = GalleryViewSnapshot(
            model=self._browse_model,
            revision=self._browse_model.revision,
            count=self._browse_model.count,
            post_id=post.id,
            index=current_index,
            content_y=float(content_y),
            grid_width=float(grid_width),
            grid_height=float(grid_height),
            window_width=float(window_width),
            window_height=float(window_height),
        )
        return self._open(post, "browse", snapshot)

    def open_saved_post(self, post: DanbooruPost, *, source_page: str) -> bool:
        """Open an available snapshot while leaving browse results untouched.

        The source page retains its own list, selection, and scroll state. It can
        use sourcePage after closing details to return to that existing page.
        """

        if not isinstance(source_page, str) or not source_page.strip():
            return False
        return self._open(post, source_page, None)

    def _open(
        self,
        post: DanbooruPost,
        source_page: str,
        snapshot: GalleryViewSnapshot | None,
    ) -> bool:
        if self._closed or not isinstance(post, DanbooruPost):
            return False
        self._generation += 1
        generation = self._generation
        self._source_page = source_page
        self._gallery_snapshot = snapshot
        opened = self._detail_controller.open_post(post)
        if self._closed or self._generation != generation:
            return False
        if not opened:
            self._gallery_snapshot = None
            return False
        self.gallerySnapshotChanged.emit()
        if not self._is_current_open(generation, post):
            return False
        self.sourceChanged.emit()
        return self._is_current_open(generation, post)

    @Slot(result=bool)
    def closeDetail(self) -> bool:
        if self._closed or not self._detail_controller.isOpen:
            return False
        self._generation += 1
        generation = self._generation
        closed = self._detail_controller.closeDetail()
        return (
            closed
            and not self._closed
            and self._generation == generation
            and not self._detail_controller.isOpen
        )

    @Slot(QObject, float, float, float, float, float, float, result="QVariantMap")
    def takeGalleryRestorePlan(
        self,
        current_model: QObject,
        grid_width: float,
        grid_height: float,
        window_width: float,
        window_height: float,
        origin_y: float,
        content_height: float,
    ) -> dict[str, object]:
        unavailable = {
            "available": False,
            "index": -1,
            "contentY": float(origin_y),
        }
        generation = self._generation
        snapshot = self._gallery_snapshot
        self._gallery_snapshot = None
        self.gallerySnapshotChanged.emit()
        if (
            self._closed
            or self._generation != generation
            or self._detail_controller.isOpen
            or self._gallery_snapshot is not None
            or self._source_page != "browse"
        ):
            return unavailable
        if snapshot is None or not isinstance(current_model, DanbooruPostListModel):
            return unavailable
        exact = (
            current_model is snapshot.model
            and current_model.revision == snapshot.revision
            and current_model.count == snapshot.count
            and _same_size(grid_width, snapshot.grid_width)
            and _same_size(grid_height, snapshot.grid_height)
            and _same_size(window_width, snapshot.window_width)
            and _same_size(window_height, snapshot.window_height)
        )
        if exact:
            index = snapshot.index
            content_y = snapshot.content_y
        else:
            index = current_model.row_for_post_id(snapshot.post_id)
            if index < 0:
                index = (
                    -1
                    if current_model.count == 0
                    else max(0, min(snapshot.index, current_model.count - 1))
                )
            maximum_y = max(
                float(origin_y),
                float(origin_y) + max(float(content_height) - float(grid_height), 0.0),
            )
            content_y = max(float(origin_y), min(snapshot.content_y, maximum_y))
        return {
            "available": True,
            "exact": exact,
            "index": index,
            "contentY": content_y,
        }

    def _on_tag_search(self, tag_name: str) -> None:
        if self._closed or self._detail_controller.isOpen:
            return
        self._generation += 1
        generation = self._generation
        self._gallery_snapshot = None
        self._source_page = "browse"
        self.gallerySnapshotChanged.emit()
        if self._closed or self._generation != generation:
            return
        self.sourceChanged.emit()
        if self._closed or self._generation != generation:
            return
        self.tagSearchRequested.emit(tag_name)

    @Slot()
    def shutdown(self) -> None:
        if self._closed:
            return
        self._generation += 1
        self._closed = True
        self._gallery_snapshot = None
        self._detail_controller.shutdown()
        self.gallerySnapshotChanged.emit()

    def _is_current_open(self, generation: int, post: DanbooruPost) -> bool:
        return (
            not self._closed
            and self._generation == generation
            and self._detail_controller.isOpen
            and self._detail_controller.current_post is post
        )


def _same_size(current: float, saved: float) -> bool:
    return math.isclose(float(current), saved, rel_tol=0.0, abs_tol=1e-6)
