"""Independent source, target and shared single/batch entry guards."""

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
from PySide6.QtNetwork import QNetworkAccessManager

from danbooru_multi_search.app.eagle_service import EagleResult, EagleSnapshot
from danbooru_multi_search.danbooru.query_types import QueryExecutionLimits
from danbooru_multi_search.danbooru.result_processing import ResultSortDirection, ResultSortField, ResultSortSpec
from danbooru_multi_search.integrations.eagle.contracts import EagleConnectionInfo, EagleFolderInfo, EagleLibraryInfo
from danbooru_multi_search.ui.detail_navigation import DetailNavigationController
from danbooru_multi_search.ui.details import DanbooruDetailController
from danbooru_multi_search.ui.eagle import EagleController
from danbooru_multi_search.ui.gallery_selection import GallerySelectionController
from danbooru_multi_search.ui.launcher import create_eagle_import_controllers
from danbooru_multi_search.ui.query_state import M2QueryBridgeController
from tests.test_eagle_import_job import Runtime
from tests.test_favorite_controller import build
from tests.test_query_controller import _harness, _post


class _Runtime(Runtime):
    changed = Signal()

    def connect_library(self):
        request = self.active_request
        self.active_request = None
        self.completed.emit(EagleResult(request.request_id, snapshot=EagleSnapshot(
            EagleConnectionInfo("4.0.0", build_version="20260401"),
            EagleLibraryInfo("LIB", "受控素材库", Path.cwd()),
            (EagleFolderInfo("TARGET", "目标"),),
        )))


@dataclass
class _Context:
    query: object
    selection: GallerySelectionController
    eagle: EagleController
    runtime: _Runtime
    single_job: object
    single: object
    batch: object
    detail: DanbooruDetailController

    def capture(self, selected=False):
        return self.batch.capture(selected, self.selection.modelRevision, self.selection.revision)


def options(**changes):
    return {
        "mode": "first", "count": 1, "start": 1, "types": [], "ratings": [],
        "score": "", "favorites": "", "more": False, "quality": "original", "fallback": "",
        "update": False, "general": False, "meta": False, "chinese": False, "query": False,
        **changes,
    }


@pytest.fixture
def context(tmp_path):
    owner = QObject()
    query = _harness(counts={"cat": 10})
    bridge = M2QueryBridgeController(query.controller, parent=owner)
    _, favorites = build(tmp_path)
    selection = GallerySelectionController(query.model, favorites, owner)
    detail = DanbooruDetailController("https://example.invalid", query.store, parent=owner)
    navigation = DetailNavigationController(detail, query.model, parent=owner)
    runtime = _Runtime()
    runtime.setParent(owner)
    eagle = EagleController(runtime, owner)
    network = QNetworkAccessManager(owner)
    single_job, single, batch = create_eagle_import_controllers(
        eagle, navigation, bridge, selection, SimpleNamespace(known_pools_for=lambda _id: ()),
        "https://example.invalid", network, query.queue, tmp_path / "staging", owner,
    )
    assert eagle.refresh()
    runtime.connect_library()
    assert eagle.selectTarget("folder:TARGET", eagle.revision)
    query.controller.start_standard("cat", limits=QueryExecutionLimits(page_size=2))
    query.dispatch()
    query.succeed((_post(1, "cat"), _post(2, "cat")), dispatch=False)
    query.controller.cancel()
    assert len(query.model.posts) == 2
    try:
        yield _Context(query, selection, eagle, runtime, single_job, single, batch, detail)
    finally:
        eagle.shutdown()
        batch.job.begin_shutdown()
        single_job.begin_shutdown()
        if runtime.active_request is not None:
            runtime.cancel()
        batch.job.finish_shutdown()
        single_job.finish_shutdown()
        selection.shutdown()
        favorites.shutdown()
        bridge.beginClosing()
        bridge.detach()
        navigation.shutdown()
        query.controller.shutdown()
        query.queue.shutdown()
        owner.deleteLater()
        QCoreApplication.sendPostedEvents(owner, QEvent.Type.DeferredDelete)


def test_changed_target_rejects_old_confirmation_without_preparing_candidates(context):
    token = context.capture()
    revision = context.eagle.revision
    assert token and context.eagle.selectTarget("root", revision)
    assert not context.batch.confirm(token, revision, options())
    assert not context.batch.busy and context.batch.job.items == []
    assert len(context.runtime.requests) == 1


@pytest.mark.parametrize("change", ["query", "view"])
def test_changed_query_or_view_rejects_old_gallery_confirmation(context, change):
    token = context.capture()
    if change == "query":
        context.query.controller.start_latest()
    else:
        context.query.controller.update_view(sort_spec=ResultSortSpec(
            ResultSortField.SCORE, ResultSortDirection.DESCENDING,
        ))
    assert not context.batch.pending["valid"]
    assert not context.batch.confirm(token, context.eagle.revision, options())
    assert not context.batch.busy and context.batch.job.items == []
    assert len(context.runtime.requests) == 1


def test_changed_manual_selection_rejects_old_selected_confirmation(context):
    assert context.selection.setSelected("1", True, context.selection.modelRevision)
    token = context.capture(selected=True)
    assert token
    context.selection.clearSelection()
    assert not context.batch.confirm(token, context.eagle.revision, options(mode="selected"))
    assert not context.batch.busy and len(context.runtime.requests) == 1


def test_single_download_blocks_batch_even_while_eagle_worker_is_idle(context):
    token = context.capture()
    assert context.detail.open_post(context.query.model.posts[0])
    assert context.single.confirm("1", context.single.sourceRevision, context.eagle.revision,
                                  False, False, False, False, False)
    context.runtime.finish("ready")
    assert context.single_job.stage == "download" and not context.runtime.busy
    assert context.capture() == ""
    assert not context.batch.confirm(token, context.eagle.revision, options())
    assert not context.batch.busy and len(context.runtime.requests) == 2


def test_batch_preparation_blocks_single_even_while_eagle_worker_is_idle(context):
    token = context.capture()
    assert context.batch.confirm(token, context.eagle.revision, options(count=20, more=True))
    assert context.batch.job.phase == "preparing" and not context.runtime.busy
    assert context.detail.open_post(context.query.model.posts[0])
    assert not context.single.confirm("1", context.single.sourceRevision, context.eagle.revision,
                                      False, False, False, False, False)
    assert not context.single_job.stage and len(context.runtime.requests) == 1
