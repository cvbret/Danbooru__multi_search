from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QTimeZone, Signal
from PySide6.QtGui import QGuiApplication

from danbooru_multi_search.core.settings import AppSettings
from danbooru_multi_search.danbooru import (
    DanbooruPostListModel,
    GalleryQueryAccepted,
    GalleryQueryDefinition,
    GalleryQueryOrigin,
    GalleryResultEvent,
    GalleryResultSummary,
    GalleryViewDefinitionChanged,
    QueryExecutionLimits,
    QueryExecutionState,
    QueryMode,
    QueryRating,
    ResultFilterSpec,
    ResultSortDirection,
    ResultSortField,
    ResultSortSpec,
)
from danbooru_multi_search.storage import (
    GalleryPosition,
    HistoryError,
    HistoryRepository,
)
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.ui.session_state import BrowserSessionController
from danbooru_multi_search.ui.query_editor import QueryTreeEditor
from danbooru_multi_search.ui.query_state import (
    M2QueryBridgeController,
    default_query_form_values,
)
from danbooru_multi_search.ui.window_state import ScreenGeometry, WindowGeometry


APPLICATION = QGuiApplication.instance() or QGuiApplication([])
FIXED_NOW = datetime(2026, 11, 6, 4, 30, tzinfo=timezone.utc)


def _definition(
    expression: str | None = "cat AND dog",
    *,
    mode: QueryMode = QueryMode.STANDARD,
    filters: ResultFilterSpec | None = None,
    sorting: ResultSortSpec | None = None,
) -> GalleryQueryDefinition:
    return GalleryQueryDefinition(
        mode=mode,
        normalized_expression=expression,
        rating=QueryRating.GENERAL,
        filter_spec=filters or ResultFilterSpec(),
        sort_spec=sorting or ResultSortSpec(),
        limits=QueryExecutionLimits(
            page_size=40,
            candidate_limit=120,
            branch_page_limit=3,
            active_time_limit_seconds=15,
        ),
    )


def _summary(
    state: QueryExecutionState = QueryExecutionState.COMPLETE,
    *,
    remote_rows: int = 8,
    unique_candidates: int = 7,
    local_matches: int = 6,
    visible_count: int = 5,
) -> GalleryResultSummary:
    return GalleryResultSummary(
        state=state,
        remote_rows=remote_rows,
        unique_candidates=unique_candidates,
        local_matches=local_matches,
        visible_count=visible_count,
        limit_reason=None,
        failure_error_code=None,
    )


class _FakeExecutor(QObject):
    snapshotChanged = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.snapshot = None


class _FakeM2Core(QObject):
    queryAccepted = Signal(object)
    viewDefinitionChanged = Signal(object)
    resultSummaryChanged = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.executor = _FakeExecutor()
        self.posts_model = DanbooruPostListModel()
        self.operation_id: int | None = None
        self.execution_id: int | None = None
        self.view_generation = 0
        self.definition: GalleryQueryDefinition | None = None
        self.reexecute_calls: list[tuple[GalleryQueryDefinition, str]] = []
        self.cancel_calls = 0
        self.cancel_hook = None
        self.reexecute_hook = None

    @property
    def gallery_operation_id(self) -> int | None:
        return self.posts_model.display_operation_id

    def start(self, definition: GalleryQueryDefinition) -> tuple[int, int]:
        self.operation_id = (self.operation_id or 0) + 1
        self.execution_id = (self.execution_id or 0) + 1
        self.view_generation = 0
        self.definition = definition
        return self.operation_id, self.execution_id

    def accept(
        self,
        *,
        origin: GalleryQueryOrigin = GalleryQueryOrigin.STANDARD,
        count_tag_usage: bool = True,
        query_tags: tuple[str, ...] = ("cat", "dog"),
        operation_id: int | None = None,
        execution_id: int | None = None,
        definition: GalleryQueryDefinition | None = None,
    ) -> GalleryQueryAccepted:
        event = GalleryQueryAccepted(
            operation_id if operation_id is not None else self.operation_id,
            execution_id if execution_id is not None else self.execution_id,
            definition or self.definition,
            (definition or self.definition).normalized_expression or "无标签查询",
            origin,
            query_tags,
            FIXED_NOW,
            count_tag_usage,
        )
        self.queryAccepted.emit(event)
        return event

    def result(
        self,
        summary: GalleryResultSummary | None = None,
        *,
        commit_gallery: bool = False,
        operation_id: int | None = None,
        execution_id: int | None = None,
        view_generation: int | None = None,
        definition: GalleryQueryDefinition | None = None,
        gallery_operation_id: int | None = None,
    ) -> GalleryResultEvent:
        actual_operation = (
            operation_id if operation_id is not None else self.operation_id
        )
        if commit_gallery:
            self.posts_model.set_processed_posts(actual_operation, ())
        event = GalleryResultEvent(
            actual_operation,
            execution_id if execution_id is not None else self.execution_id,
            view_generation if view_generation is not None else self.view_generation,
            definition or self.definition,
            summary or _summary(),
            (
                gallery_operation_id
                if gallery_operation_id is not None
                else self.gallery_operation_id
            ),
        )
        self.resultSummaryChanged.emit(event)
        return event

    def change_view(
        self,
        definition: GalleryQueryDefinition,
        *,
        applied: bool,
    ) -> GalleryViewDefinitionChanged:
        self.view_generation += 1
        self.definition = definition
        event = GalleryViewDefinitionChanged(
            self.operation_id,
            self.execution_id,
            self.view_generation,
            definition,
            applied,
        )
        self.viewDefinitionChanged.emit(event)
        return event

    def reexecute_history(
        self,
        definition: GalleryQueryDefinition,
        *,
        display_text: str,
    ) -> int:
        self.reexecute_calls.append((definition, display_text))
        operation_id, _execution_id = self.start(definition)
        if self.reexecute_hook is not None:
            self.reexecute_hook(self)
        return operation_id

    def cancel(self) -> bool:
        self.cancel_calls += 1
        if self.cancel_hook is not None:
            self.cancel_hook()
        return True


class _FakeBridge(QObject):
    draftEdited = Signal()
    viewUpdatePreparing = Signal(int, int, int)
    viewUpdateAborted = Signal(int, int, int)

    def __init__(self, core: _FakeM2Core) -> None:
        super().__init__()
        self.core = core
        self.loaded_definition: GalleryQueryDefinition | None = None
        self.load_calls = 0
        self.reexecute_calls = 0
        self.load_hook = None

    def loadDraftDefinition(self, definition: object) -> bool:
        assert isinstance(definition, GalleryQueryDefinition)
        self.loaded_definition = definition
        self.load_calls += 1
        if self.load_hook is not None:
            self.load_hook()
        return True

    def reexecuteLoadedHistory(self, display_text: str) -> int | None:
        self.reexecute_calls += 1
        if self.loaded_definition is None:
            return None
        return self.core.reexecute_history(
            self.loaded_definition,
            display_text=display_text,
        )


def _build(tmp_path):
    database = Database(tmp_path / "application.sqlite3")
    database.initialize()
    repository = HistoryRepository(database, clock=lambda: FIXED_NOW)
    core = _FakeM2Core()
    bridge = _FakeBridge(core)
    session = BrowserSessionController(
        repository,
        None,
        AppSettings(),
        (ScreenGeometry(WindowGeometry(0, 0, 1920, 1040), primary=True),),
        m2_controller=core,
        query_bridge=bridge,
    )
    return repository, core, bridge, session


def _store_definition(
    repository: HistoryRepository,
    definition: GalleryQueryDefinition,
    *,
    display_text: str = "cat AND dog",
):
    query = repository.record_accepted_definition(
        definition=definition,
        display_text=display_text,
        page=1,
        count_tag_usage=True,
        used_at=FIXED_NOW,
    )
    assert query is not None
    assert repository.mark_query_committed(query.id, 1)
    return query


def test_history_selection_loads_exact_definition_without_request(tmp_path):
    definition = _definition(
        filters=ResultFilterSpec(
            published_from_utc=datetime(
                2026,
                1,
                2,
                3,
                4,
                5,
                678901,
                tzinfo=timezone.utc,
            ),
            minimum_age=timedelta(days=2, seconds=7),
        )
    )
    repository, core, bridge, session = _build(tmp_path)
    stored = _store_definition(repository, definition)
    session._refresh_history()

    session.selectHistory(0)

    assert bridge.loaded_definition == stored.definition == definition
    assert bridge.load_calls == 1
    assert core.reexecute_calls == []


def test_real_bridge_reexecutes_exact_history_until_first_edit():
    definition = _definition(
        filters=ResultFilterSpec(
            published_from_utc=datetime(
                2026,
                1,
                2,
                3,
                4,
                5,
                678901,
                tzinfo=timezone.utc,
            ),
            minimum_age=timedelta(days=2, seconds=7),
        )
    )
    core = _FakeM2Core()
    bridge = M2QueryBridgeController(
        core,  # type: ignore[arg-type]
        query_editor=QueryTreeEditor("cat AND dog"),
        time_zone=QTimeZone.utc(),
    )

    assert bridge.loadDraftDefinition(definition) is True
    assert bridge.loaded_history_definition is definition
    assert bridge.draft_definition is definition
    assert bridge.reexecuteLoadedHistory("精确历史") is True
    assert core.reexecute_calls == [(definition, "精确历史")]
    assert bridge.executionConditionsChanged is False
    assert bridge.viewConditionsChanged is False

    assert bridge.updateDraftForm(default_query_form_values()) is True
    assert bridge.loaded_history_definition is None
    assert bridge.reexecuteLoadedHistory("精确历史") is False


def test_accepted_and_result_use_exact_identity_and_page_one(tmp_path):
    repository, core, _bridge, session = _build(tmp_path)
    definition = _definition()
    core.start(definition)
    core.accept()

    stored = repository.latest_query()
    assert stored is not None
    assert stored.last_submitted_page == 1
    assert stored.last_committed_page is None

    core.result(commit_gallery=True)

    committed = repository.get_query(stored.id)
    assert committed is not None
    assert committed.last_committed_page == 1
    assert committed.result_summary == _summary()
    assert repository.load_session().active_query_id == stored.id
    assert session._active_history_id == stored.id
    assert session._displayed_model_revision == core.posts_model.revision


def test_applied_view_switches_history_without_model_revision_change(tmp_path):
    repository, core, _bridge, session = _build(tmp_path)
    definition = _definition()
    core.start(definition)
    core.accept()
    core.result(commit_gallery=True)
    original_revision = core.posts_model.revision
    source_id = session._active_history_id
    observed_active_definitions: list[GalleryQueryDefinition | None] = []

    def observe_history_signal() -> None:
        active_id = session._active_history_id
        active = repository.get_query(active_id) if active_id is not None else None
        observed_active_definitions.append(
            active.definition if active is not None else None
        )

    session.historyChanged.connect(observe_history_signal)

    viewed = replace(
        definition,
        sort_spec=ResultSortSpec(
            ResultSortField.SCORE,
            ResultSortDirection.DESCENDING,
        ),
    )
    core.change_view(viewed, applied=True)

    assert core.posts_model.revision == original_revision
    assert session._active_history_id is not None
    assert session._active_history_id != source_id
    active = repository.get_query(session._active_history_id)
    assert active is not None
    assert active.definition == viewed
    assert active.last_committed_page == 1
    assert repository.load_session().active_query_id == active.id
    assert observed_active_definitions[-1] == viewed


@pytest.mark.parametrize("failure", ["none", "error"])
def test_view_write_failure_never_falls_back_to_source(
    tmp_path,
    monkeypatch,
    failure,
):
    repository, core, _bridge, session = _build(tmp_path)
    definition = _definition()
    core.start(definition)
    core.accept()
    core.result(commit_gallery=True)
    source_id = session._active_history_id
    viewed = replace(
        definition,
        sort_spec=ResultSortSpec(
            ResultSortField.FAV_COUNT,
            ResultSortDirection.DESCENDING,
        ),
    )

    if failure == "none":
        monkeypatch.setattr(repository, "record_view_definition", lambda **_kw: None)
    else:
        def fail(**_kw):
            raise HistoryError("fixed failure")

        monkeypatch.setattr(repository, "record_view_definition", fail)

    core.change_view(viewed, applied=True)
    core.result(definition=viewed, view_generation=1)

    assert session._active_history_id is None
    assert repository.load_session().active_query_id is None
    assert source_id is not None
    assert repository.get_query(source_id).result_summary == _summary()
    assert session._m2_restore_token is None


@pytest.mark.parametrize("failure", ["none", "error"])
def test_unmapped_new_gallery_clears_previous_active_history(
    tmp_path,
    monkeypatch,
    failure,
):
    repository, core, _bridge, session = _build(tmp_path)
    first = _definition("cat")
    core.start(first)
    core.accept(query_tags=("cat",))
    core.result(commit_gallery=True)
    old_history_id = session._active_history_id
    assert old_history_id is not None

    if failure == "none":
        monkeypatch.setattr(
            repository,
            "record_accepted_definition",
            lambda **_kw: None,
        )
    else:
        def fail(**_kw):
            raise HistoryError("fixed failure")

        monkeypatch.setattr(repository, "record_accepted_definition", fail)

    second = _definition("dog")
    core.start(second)
    core.accept(query_tags=("dog",))
    core.result(commit_gallery=True)

    assert core.gallery_operation_id == 2
    assert session._active_history_id is None
    assert session._gallery_position is None
    assert repository.load_session().active_query_id is None
    assert repository.get_query(old_history_id) is not None


def test_summary_update_false_removes_mapping_and_active_identity(
    tmp_path,
    monkeypatch,
):
    repository, core, _bridge, session = _build(tmp_path)
    definition = _definition()
    core.start(definition)
    core.accept()
    stored = repository.latest_query()
    assert stored is not None

    def remove_row(*_args):
        connection = repository._database.connect()
        try:
            with connection:
                connection.execute(
                    "DELETE FROM recent_queries WHERE id = ?",
                    (stored.id,),
                )
        finally:
            connection.close()
        return False

    monkeypatch.setattr(repository, "update_result_summary", remove_row)

    core.result(commit_gallery=True)

    assert session._active_history_id is None
    assert repository.load_session().active_query_id is None
    assert repository.get_query(stored.id) is None
    assert session.historyModel.count == 0


def test_commit_false_refreshes_history_model(tmp_path, monkeypatch):
    repository, core, _bridge, session = _build(tmp_path)
    definition = _definition()
    core.start(definition)
    core.accept()
    stored = repository.latest_query()
    assert stored is not None

    def remove_row(*_args, **_kwargs):
        connection = repository._database.connect()
        try:
            with connection:
                connection.execute(
                    "DELETE FROM recent_queries WHERE id = ?",
                    (stored.id,),
                )
        finally:
            connection.close()
        return False

    monkeypatch.setattr(repository, "mark_query_committed", remove_row)
    core.result(commit_gallery=True)

    assert session._active_history_id is None
    assert session.historyModel.count == 0


def test_reexecute_binds_synchronous_accept_result_and_model_revision(tmp_path):
    repository, core, bridge, session = _build(tmp_path)
    definition = _definition()
    stored = _store_definition(repository, definition)
    assert repository.update_gallery_position(
        GalleryPosition(stored.id, None, None, None, 240.0)
    )
    session._refresh_history()
    session.selectHistory(0)
    ready_count = 0

    def on_ready():
        nonlocal ready_count
        ready_count += 1

    session.galleryRestoreReady.connect(on_ready)

    def finish_synchronously(active: _FakeM2Core) -> None:
        active.accept(
            origin=GalleryQueryOrigin.HISTORY,
            count_tag_usage=True,
        )
        active.result(commit_gallery=True)

    core.reexecute_hook = finish_synchronously
    session.restoreSelected()

    assert bridge.reexecute_calls == 1
    assert core.reexecute_calls == [(definition, stored.display_text)]
    assert ready_count == 1
    plan = session.takeGalleryRestorePlan(0.0, 1000.0, 500.0)
    assert plan["available"] is True
    assert plan["absoluteContentY"] == 240.0
    assert plan["modelRevision"] == core.posts_model.revision


def test_history_write_failure_invalidates_unbound_reexecute_token(
    tmp_path,
    monkeypatch,
):
    repository, core, _bridge, session = _build(tmp_path)
    stored = _store_definition(repository, _definition())
    session._refresh_history()
    session.selectHistory(0)
    monkeypatch.setattr(
        repository,
        "record_accepted_definition",
        lambda **_kwargs: None,
    )
    core.reexecute_hook = lambda active: active.accept(
        origin=GalleryQueryOrigin.HISTORY
    )

    session.restoreSelected()

    assert session._m2_restore_token is None
    assert core.cancel_calls == 1
    assert repository.get_query(stored.id) is not None


def test_clear_history_rejects_old_events_and_accepts_new_operation(tmp_path):
    repository, core, _bridge, session = _build(tmp_path)
    old_definition = _definition("cat")
    old_operation, old_execution = core.start(old_definition)
    core.accept(query_tags=("cat",))
    assert len(repository.list_queries()) == 1

    session.clearHistory()
    assert repository.list_queries() == ()

    core.accept(
        operation_id=old_operation,
        execution_id=old_execution,
        definition=old_definition,
        query_tags=("cat",),
    )
    core.result(
        operation_id=old_operation,
        execution_id=old_execution,
        definition=old_definition,
        gallery_operation_id=old_operation,
    )
    assert repository.list_queries() == ()

    new_definition = _definition("dog")
    core.start(new_definition)
    core.accept(query_tags=("dog",))

    queries = repository.list_queries()
    assert len(queries) == 1
    assert queries[0].definition == new_definition


def test_clear_history_keeps_query_started_from_model_reset_signal(tmp_path):
    repository, core, _bridge, session = _build(tmp_path)
    old_definition = _definition("cat")
    core.start(old_definition)
    core.accept(query_tags=("cat",))
    new_definition = _definition("dog")
    started = False

    def start_new_query() -> None:
        nonlocal started
        if started:
            return
        started = True
        core.start(new_definition)
        core.accept(query_tags=("dog",))

    session.historyModel.countChanged.connect(start_new_query)
    session.clearHistory()

    queries = repository.list_queries()
    assert started is True
    assert len(queries) == 1
    assert queries[0].definition == new_definition
    assert session.historyModel.count == 1


def test_clear_history_preserves_query_completed_inside_repository_call(
    tmp_path,
    monkeypatch,
):
    repository, core, _bridge, session = _build(tmp_path)
    old_definition = _definition("cat")
    core.start(old_definition)
    core.accept(query_tags=("cat",))
    core.result(commit_gallery=True)
    new_definition = _definition("dog")
    original_clear = repository.clear_history

    def clear_then_complete_new_query() -> None:
        original_clear()
        core.start(new_definition)
        core.accept(query_tags=("dog",))
        core.result(commit_gallery=True)
        session.updateM2GalleryPosition(
            core.operation_id,
            core.execution_id,
            core.view_generation,
            999,
            0,
            12.0,
            180.0,
            core.posts_model.revision,
        )

    monkeypatch.setattr(
        repository,
        "clear_history",
        clear_then_complete_new_query,
    )
    session.clearHistory()

    queries = repository.list_queries()
    assert len(queries) == 1
    assert queries[0].definition == new_definition
    assert session.historyModel.count == 1
    assert session._active_history_id == queries[0].id
    assert session._m2_display_binding is not None
    assert session._m2_display_binding.identity.operation_id == core.operation_id
    assert session._gallery_position is not None
    assert session._gallery_position.query_id == queries[0].id
    assert session._gallery_position.scroll_position == 180.0


def test_new_refresh_and_local_view_use_distinct_position_fallbacks(tmp_path):
    repository, core, _bridge, session = _build(tmp_path)
    definition = _definition("cat")
    core.start(definition)
    core.accept(query_tags=("cat",))
    core.result(commit_gallery=True)

    new_plan = session.takeGalleryRestorePlan(10.0, 1000.0, 300.0)
    assert new_plan["available"] is True
    assert new_plan["absoluteContentY"] == 10.0

    session.updateM2GalleryPosition(
        core.operation_id,
        core.execution_id,
        core.view_generation,
        999,
        4,
        17.0,
        360.0,
        core.posts_model.revision,
    )
    core.start(definition)
    core.accept(
        origin=GalleryQueryOrigin.REFRESH,
        count_tag_usage=False,
        query_tags=("cat",),
    )
    core.result(commit_gallery=True)

    refresh_plan = session.takeGalleryRestorePlan(10.0, 1000.0, 300.0)
    assert refresh_plan["available"] is True
    assert refresh_plan["index"] == -1
    assert refresh_plan["absoluteContentY"] == 10.0

    session.updateM2GalleryPosition(
        core.operation_id,
        core.execution_id,
        core.view_generation,
        998,
        3,
        12.0,
        280.0,
        core.posts_model.revision,
    )
    viewed = replace(
        definition,
        sort_spec=ResultSortSpec(
            ResultSortField.SCORE,
            ResultSortDirection.DESCENDING,
        ),
    )
    core.change_view(viewed, applied=True)

    local_plan = session.takeGalleryRestorePlan(10.0, 1000.0, 300.0)
    assert local_plan["available"] is True
    assert local_plan["index"] == -1
    assert local_plan["absoluteContentY"] == 10.0


def test_unapplied_view_first_gallery_returns_to_top(tmp_path):
    _repository, core, _bridge, session = _build(tmp_path)
    old_definition = _definition("cat")
    core.start(old_definition)
    core.accept(query_tags=("cat",))
    core.result(commit_gallery=True)
    session.takeGalleryRestorePlan(0.0, 1000.0, 300.0)
    session.updateM2GalleryPosition(
        core.operation_id,
        core.execution_id,
        core.view_generation,
        999,
        4,
        17.0,
        360.0,
        core.posts_model.revision,
    )

    new_definition = _definition("dog")
    core.start(new_definition)
    core.accept(query_tags=("dog",))
    viewed = replace(
        new_definition,
        sort_spec=ResultSortSpec(
            ResultSortField.SCORE,
            ResultSortDirection.DESCENDING,
        ),
    )
    core.change_view(viewed, applied=False)
    core.result(commit_gallery=True)

    plan = session.takeGalleryRestorePlan(10.0, 1000.0, 300.0)
    assert plan["available"] is True
    assert plan["index"] == -1
    assert plan["absoluteContentY"] == 10.0


def test_refresh_without_displayed_source_uses_new_gallery_top(tmp_path):
    _repository, core, _bridge, session = _build(tmp_path)
    definition = _definition("cat")
    core.start(definition)
    core.accept(query_tags=("cat",))
    assert session._active_history_id is None

    core.start(definition)
    core.accept(
        origin=GalleryQueryOrigin.REFRESH,
        count_tag_usage=False,
        query_tags=("cat",),
    )
    core.result(commit_gallery=True)

    plan = session.takeGalleryRestorePlan(10.0, 1000.0, 300.0)
    assert plan["available"] is True
    assert plan["index"] == -1
    assert plan["absoluteContentY"] == 10.0


def test_local_view_captures_position_before_model_change_and_rejects_old_view(
    tmp_path,
):
    _repository, core, bridge, session = _build(tmp_path)
    definition = _definition("cat")
    core.start(definition)
    core.accept(query_tags=("cat",))
    core.result(commit_gallery=True)
    session.takeGalleryRestorePlan(0.0, 1000.0, 300.0)
    session.updateM2GalleryPosition(
        core.operation_id,
        core.execution_id,
        core.view_generation,
        999,
        4,
        17.0,
        360.0,
        core.posts_model.revision,
    )

    bridge.viewUpdatePreparing.emit(
        core.operation_id,
        core.execution_id,
        core.view_generation,
    )
    token = session._m2_restore_token
    assert token is not None
    assert token.source_query.scroll_position == 360.0

    # A layout callback from the source view can still arrive before the core
    # publishes the new view identity.  It must not change the saved token.
    session.updateM2GalleryPosition(
        core.operation_id,
        core.execution_id,
        core.view_generation,
        997,
        2,
        8.0,
        40.0,
        core.posts_model.revision,
    )
    assert session._m2_restore_token.source_query.scroll_position == 360.0

    viewed = replace(
        definition,
        sort_spec=ResultSortSpec(
            ResultSortField.SCORE,
            ResultSortDirection.DESCENDING,
        ),
    )
    core.change_view(viewed, applied=True)
    position_after_view = session._gallery_position
    session.updateM2GalleryPosition(
        core.operation_id,
        core.execution_id,
        0,
        996,
        1,
        4.0,
        20.0,
        core.posts_model.revision,
    )
    assert session._gallery_position == position_after_view


def test_repeated_summary_does_not_commit_same_identity_twice(
    tmp_path,
    monkeypatch,
):
    repository, core, _bridge, session = _build(tmp_path)
    definition = _definition()
    calls = 0
    original = repository.mark_query_committed

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, "mark_query_committed", counted)
    core.start(definition)
    core.accept()
    core.result(commit_gallery=True)
    core.result(commit_gallery=False)

    assert calls == 1
    assert session._active_history_id is not None


def test_view_event_ignores_old_identity_after_earlier_listener_starts_query(
    tmp_path,
):
    repository, core, _bridge, session = _build(tmp_path)
    definition = _definition("cat")
    core.start(definition)
    core.accept(query_tags=("cat",))
    core.result(commit_gallery=True)
    original_history_id = session._active_history_id
    viewed = replace(
        definition,
        sort_spec=ResultSortSpec(
            ResultSortField.SCORE,
            ResultSortDirection.DESCENDING,
        ),
    )
    replacement = _definition("dog")
    core.viewDefinitionChanged.disconnect(session._m2_view_changed)
    core.viewDefinitionChanged.connect(lambda _event: core.start(replacement))
    core.viewDefinitionChanged.connect(session._m2_view_changed)

    core.change_view(viewed, applied=True)

    assert session._active_history_id == original_history_id
    assert len(repository.list_queries()) == 1


def test_result_and_model_callbacks_recheck_identity_after_earlier_listener(
    tmp_path,
):
    repository, core, _bridge, session = _build(tmp_path)
    definition = _definition("cat")
    core.start(definition)
    core.accept(query_tags=("cat",))
    stored = repository.latest_query()
    assert stored is not None
    replacement = _definition("dog")

    core.posts_model.revisionChanged.disconnect(session._m2_model_revision_changed)
    core.posts_model.revisionChanged.connect(lambda: core.start(replacement))
    core.posts_model.revisionChanged.connect(session._m2_model_revision_changed)
    core.result(commit_gallery=True)

    current = repository.get_query(stored.id)
    assert current is not None
    assert current.last_committed_page is None
    assert current.result_summary is None
    assert session._active_history_id is None


def test_active_clear_error_cannot_overwrite_nested_new_gallery(
    tmp_path,
    monkeypatch,
):
    repository, core, _bridge, session = _build(tmp_path)
    first = _definition("cat")
    second = _definition("dog")
    third = _definition("bird")
    core.start(first)
    core.accept(query_tags=("cat",))
    core.result(commit_gallery=True)

    original_record = repository.record_accepted_definition

    def conditional_record(**values):
        if values["definition"] == second:
            return None
        return original_record(**values)

    monkeypatch.setattr(
        repository,
        "record_accepted_definition",
        conditional_record,
    )
    triggered = False

    def fail_after_new_gallery(_query_id):
        nonlocal triggered
        if not triggered:
            triggered = True
            core.start(third)
            core.accept(query_tags=("bird",))
            core.result(commit_gallery=True)
        raise HistoryError("fixed active clear failure")

    monkeypatch.setattr(repository, "set_active_query", fail_after_new_gallery)
    core.start(second)
    core.accept(query_tags=("dog",))
    core.result(commit_gallery=True)

    assert triggered is True
    active = repository.get_query(session._active_history_id)
    assert active is not None
    assert active.definition == third
    assert repository.load_session().active_query_id == active.id


def test_restore_ready_listener_can_invalidate_plan_synchronously(tmp_path):
    repository, core, _bridge, session = _build(tmp_path)
    definition = _definition()
    stored = _store_definition(repository, definition)
    session._refresh_history()
    session.selectHistory(0)
    session.galleryRestoreReady.connect(session.controlsEdited)

    def finish_synchronously(active: _FakeM2Core) -> None:
        active.accept(origin=GalleryQueryOrigin.HISTORY)
        active.result(commit_gallery=True)

    core.reexecute_hook = finish_synchronously
    session.restoreSelected()

    assert session.takeGalleryRestorePlan(0.0, 1000.0, 500.0) == {
        "available": False
    }
    assert repository.get_query(stored.id) is not None


def test_history_selection_does_not_overwrite_nested_cancel_selection(tmp_path):
    repository, core, bridge, session = _build(tmp_path)
    first = _store_definition(repository, _definition("cat"), display_text="cat")
    second = _store_definition(repository, _definition("dog"), display_text="dog")
    session._refresh_history()
    assert session.historyModel.items[0].id == second.id
    assert session.historyModel.items[1].id == first.id
    session.selectHistory(0)
    session.restoreSelected()
    assert session._m2_restore_token is not None

    core.cancel_hook = lambda: session.selectHistory(1)
    session.selectHistory(0)

    assert core.cancel_calls == 1
    assert session._pending_history_id == first.id
    assert bridge.loaded_definition == first.definition


def test_restore_does_not_execute_after_definition_load_selects_other_history(
    tmp_path,
):
    repository, core, bridge, session = _build(tmp_path)
    first = _store_definition(repository, _definition("cat"), display_text="cat")
    second = _store_definition(repository, _definition("dog"), display_text="dog")
    session._refresh_history()
    session.selectHistory(0)
    assert session._pending_history_id == second.id
    bridge.loaded_definition = None

    def select_other() -> None:
        bridge.load_hook = None
        session.selectHistory(1)

    bridge.load_hook = select_other
    session.restoreSelected()

    assert core.reexecute_calls == []
    assert session._pending_history_id == first.id
    assert bridge.loaded_definition == first.definition


def test_restore_does_not_overwrite_selection_changed_inside_get_query(
    tmp_path,
    monkeypatch,
):
    repository, core, bridge, session = _build(tmp_path)
    first = _store_definition(repository, _definition("cat"), display_text="cat")
    second = _store_definition(repository, _definition("dog"), display_text="dog")
    session._refresh_history()
    session.selectHistory(0)
    assert session._pending_history_id == second.id
    original_get = repository.get_query
    reentered = False

    def get_then_select_other(history_id: int):
        nonlocal reentered
        query = original_get(history_id)
        if not reentered:
            reentered = True
            session.selectHistory(1)
        return query

    monkeypatch.setattr(repository, "get_query", get_then_select_other)
    session.restoreSelected()

    assert reentered is True
    assert core.reexecute_calls == []
    assert session._pending_history_id == first.id
    assert bridge.loaded_definition == first.definition


def test_refresh_get_query_reentry_preserves_new_completed_operation(
    tmp_path,
    monkeypatch,
):
    repository, core, _bridge, session = _build(tmp_path)
    old_definition = _definition("cat")
    refresh_definition = _definition("cat")
    new_definition = _definition("dog")
    core.start(old_definition)
    core.accept(query_tags=("cat",))
    core.result(commit_gallery=True)
    session.takeGalleryRestorePlan(0.0, 1000.0, 300.0)
    original_get = repository.get_query
    reentered = False

    def get_then_complete_new_query(history_id: int):
        nonlocal reentered
        query = original_get(history_id)
        if not reentered:
            reentered = True
            core.start(new_definition)
            core.accept(query_tags=("dog",))
            core.result(commit_gallery=True)
        return query

    monkeypatch.setattr(repository, "get_query", get_then_complete_new_query)
    core.start(refresh_definition)
    core.accept(
        origin=GalleryQueryOrigin.REFRESH,
        count_tag_usage=False,
        query_tags=("cat",),
    )

    assert reentered is True
    assert session._m2_restore_token is None
    assert session._m2_display_binding is not None
    assert session._m2_display_binding.identity.operation_id == core.operation_id
    active = repository.get_query(session._active_history_id)
    assert active is not None
    assert active.definition == new_definition
    plan = session.takeGalleryRestorePlan(10.0, 1000.0, 300.0)
    assert plan["available"] is True
    assert plan["absoluteContentY"] == 10.0


def test_refresh_get_query_edit_invalidation_cannot_rebuild_restore_token(
    tmp_path,
    monkeypatch,
):
    repository, core, _bridge, session = _build(tmp_path)
    definition = _definition("cat")
    core.start(definition)
    core.accept(query_tags=("cat",))
    core.result(commit_gallery=True)
    session.takeGalleryRestorePlan(0.0, 1000.0, 300.0)
    original_get = repository.get_query
    reentered = False

    def get_then_edit_controls(history_id: int):
        nonlocal reentered
        query = original_get(history_id)
        if not reentered:
            reentered = True
            session.controlsEdited()
        return query

    monkeypatch.setattr(repository, "get_query", get_then_edit_controls)
    core.start(definition)
    refresh_operation_id = core.operation_id
    core.accept(
        origin=GalleryQueryOrigin.REFRESH,
        count_tag_usage=False,
        query_tags=("cat",),
    )

    assert reentered is True
    assert session._m2_restore_token is None
    assert all(
        identity.operation_id != refresh_operation_id
        for identity in session._m2_history_by_identity
    )


@pytest.mark.parametrize(
    "failure",
    ["summary_error", "commit_false", "view_none"],
)
def test_reexecute_return_rejects_identity_whose_history_mapping_failed(
    tmp_path,
    monkeypatch,
    failure,
):
    repository, core, _bridge, session = _build(tmp_path)
    definition = _definition()
    _store_definition(repository, definition)
    session._refresh_history()
    session.selectHistory(0)

    if failure == "summary_error":
        def fail_summary(*_args):
            raise HistoryError("fixed summary failure")

        monkeypatch.setattr(repository, "update_result_summary", fail_summary)
    elif failure == "commit_false":
        monkeypatch.setattr(
            repository,
            "mark_query_committed",
            lambda *_args, **_kwargs: False,
        )
    else:
        monkeypatch.setattr(
            repository,
            "record_view_definition",
            lambda **_kwargs: None,
        )

    def finish_synchronously(active: _FakeM2Core) -> None:
        active.accept(origin=GalleryQueryOrigin.HISTORY)
        active.result(commit_gallery=True)
        if failure == "view_none":
            active.change_view(
                replace(
                    definition,
                    sort_spec=ResultSortSpec(
                        ResultSortField.SCORE,
                        ResultSortDirection.DESCENDING,
                    ),
                ),
                applied=True,
            )

    core.reexecute_hook = finish_synchronously
    session.restoreSelected()

    assert session._m2_restore_token is None
    assert session.takeGalleryRestorePlan(0.0, 1000.0, 300.0) == {
        "available": False
    }


def test_old_accept_is_ignored_after_synchronous_new_query(tmp_path):
    repository, core, _bridge, _session = _build(tmp_path)
    old_definition = _definition("cat")
    new_definition = _definition("dog")
    core.start(old_definition)

    def replace_operation(_event) -> None:
        core.start(new_definition)

    core.queryAccepted.disconnect()
    core.queryAccepted.connect(replace_operation)
    # Recreate the session after the earlier listener to force Qt synchronous
    # re-entry before the session receives the old accepted event.
    bridge = _FakeBridge(core)
    session = BrowserSessionController(
        repository,
        None,
        AppSettings(),
        (ScreenGeometry(WindowGeometry(0, 0, 1920, 1040), primary=True),),
        m2_controller=core,
        query_bridge=bridge,
    )

    core.accept(
        operation_id=1,
        execution_id=1,
        definition=old_definition,
        query_tags=("cat",),
    )

    assert repository.list_queries() == ()
    assert session._active_history_id is None


def test_result_arriving_before_session_accept_slot_is_replayed(tmp_path):
    database = Database(tmp_path / "application.sqlite3")
    database.initialize()
    repository = HistoryRepository(database, clock=lambda: FIXED_NOW)
    core = _FakeM2Core()
    bridge = _FakeBridge(core)
    definition = _definition("cat")
    core.start(definition)
    core.queryAccepted.connect(lambda _event: core.result(commit_gallery=True))
    session = BrowserSessionController(
        repository,
        None,
        AppSettings(),
        (ScreenGeometry(WindowGeometry(0, 0, 1920, 1040), primary=True),),
        m2_controller=core,
        query_bridge=bridge,
    )
    active_when_history_changed: list[int | None] = []
    session.historyChanged.connect(
        lambda: active_when_history_changed.append(session._active_history_id)
    )

    core.accept(query_tags=("cat",))

    stored = repository.latest_query()
    assert stored is not None
    assert stored.result_summary == _summary()
    assert stored.last_committed_page == 1
    assert session._active_history_id == stored.id
    assert active_when_history_changed[-1] == stored.id


def test_view_arriving_before_session_accept_slot_is_replayed(tmp_path):
    database = Database(tmp_path / "application.sqlite3")
    database.initialize()
    repository = HistoryRepository(database, clock=lambda: FIXED_NOW)
    core = _FakeM2Core()
    bridge = _FakeBridge(core)
    definition = _definition("cat")
    viewed = replace(
        definition,
        sort_spec=ResultSortSpec(
            ResultSortField.SCORE,
            ResultSortDirection.DESCENDING,
        ),
    )
    core.start(definition)
    core.queryAccepted.connect(
        lambda _event: core.change_view(viewed, applied=False)
    )
    session = BrowserSessionController(
        repository,
        None,
        AppSettings(),
        (ScreenGeometry(WindowGeometry(0, 0, 1920, 1040), primary=True),),
        m2_controller=core,
        query_bridge=bridge,
    )

    core.accept(query_tags=("cat",))
    core.result(commit_gallery=True)

    active = repository.get_query(session._active_history_id)
    assert active is not None
    assert active.definition == viewed
    assert active.last_committed_page == 1
