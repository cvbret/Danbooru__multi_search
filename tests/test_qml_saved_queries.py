from __future__ import annotations

from PySide6.QtCore import QPointF
from PySide6.QtTest import QTest

from danbooru_multi_search.core.bootstrap import initialize_runtime
from danbooru_multi_search.core.saved_queries import SavedQueryDraft
from danbooru_multi_search.storage.saved_queries import SavedQueryRepository
from tests.m2_qml_harness import create_m2_qml_test_engine, close_m2_qml_test_engine
from tests.test_favorite_controller import APPLICATION, post
from tests.test_qml_favorites import NoNetwork, item, click, wait
from tests.test_saved_queries import precise_definition


def start(tmp_path, *, narrow=False):
    runtime = initialize_runtime(tmp_path)
    saved = SavedQueryRepository(runtime.database)
    network = NoNetwork()
    engine, bridge = create_m2_qml_test_engine(APPLICATION, danbooru_base_url="https://example.invalid", runtime=runtime, network_manager=network)
    root = engine.rootObjects()[0]
    root.setWidth(820 if narrow else 1280)
    root.setHeight(600 if narrow else 820)
    bridge.postsModel.set_posts((post(42),))
    root.requestActivate()
    assert QTest.qWaitForWindowExposed(root)
    return engine, root, bridge, bridge.parent(), saved, network


def press(root, name):
    control = item(root, name)
    viewport = item(root, "savedQueryScroll").property("contentItem")
    center = control.mapToScene(QPointF(control.width() / 2, control.height() / 2))
    middle = viewport.mapToScene(QPointF(0, viewport.height() / 2))
    maximum = max(0, viewport.property("contentHeight") - viewport.height())
    viewport.setProperty("contentY", max(0, min(viewport.property("contentY") + center.y() - middle.y(), maximum)))
    APPLICATION.processEvents()
    click(root, control)


def open_panel(root):
    button = item(root, "openQueryDrawerButton")
    if button.property("visible"):
        click(root, button)
    click(root, item(root, "openSavedQueriesButton"))
    assert wait(lambda: item(root, "savedQueryPanel").property("opened"))


def test_narrow_save_rename_delete_confirmation_with_actual_clicks(tmp_path):
    engine, root, bridge, session, saved, network = start(tmp_path, narrow=True)
    try:
        definition = precise_definition()
        assert bridge.loadDraftDefinition(definition)
        before = session.favorite_controller._repository.capture_collection()
        open_panel(root)
        panel = item(root, "savedQueryPanel")
        assert panel.property("width") <= 788 and panel.property("height") <= 568
        item(root, "savedQueryNameInput").setProperty("text", "Exact query")
        press(root, "saveCapturedQueryButton")
        assert wait(lambda: len(saved.list_queries()) == 1)
        record = saved.list_queries()[0]
        assert record.definition == definition
        press(root, "renameSavedQuery_" + record.query_id)
        assert wait(lambda: item(root, "renameSavedQueryDialog").property("opened"))
        item(root, "savedQueryRenameInput").setProperty("text", "Renamed query")
        click(root, item(root, "confirmSavedQueryRenameButton"))
        assert wait(lambda: not item(root, "renameSavedQueryDialog").property("opened"))
        assert saved.list_queries()[0].name == "Renamed query"
        press(root, "deleteSavedQuery_" + record.query_id)
        assert wait(lambda: item(root, "deleteSavedQueryDialog").property("opened"))
        click(root, item(root, "cancelSavedQueryDeleteButton"))
        assert len(saved.list_queries()) == 1
        press(root, "deleteSavedQuery_" + record.query_id)
        assert wait(lambda: item(root, "deleteSavedQueryDialog").property("opened"))
        click(root, item(root, "confirmSavedQueryDeleteButton"))
        assert saved.list_queries() == ()
        assert session.favorite_controller._repository.capture_collection() == before
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


def test_loading_precise_query_changes_only_draft_without_focused_autocomplete(tmp_path):
    engine, root, bridge, session, saved, network = start(tmp_path)
    try:
        definition = precise_definition()
        record = saved.create(SavedQueryDraft("Exact", definition))
        assert bridge.applyAdvancedText("base")
        item(root, "tagsInput").forceActiveFocus()
        open_panel(root)
        posts, revision = bridge.postsModel.posts, bridge.postsModel.revision
        history = session.history_repository.latest_query()
        press(root, "loadSavedQuery_" + record.query_id)
        assert wait(lambda: not item(root, "savedQueryPanel").property("opened"))
        QTest.qWait(450)
        assert bridge.draft_definition == definition
        assert bridge.postsModel.posts is posts and bridge.postsModel.revision == revision
        assert session.history_repository.latest_query() == history is None
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


def test_invalid_empty_draft_opens_panel_with_visible_reason_and_zero_writes(tmp_path):
    engine, root, bridge, session, saved, network = start(tmp_path)
    try:
        assert bridge.draft_definition is None
        open_panel(root)
        error = item(root, "savedQueryError")
        assert error.property("visible") and error.property("text")
        assert session.saved_queries.capturedDraft["token"] == ""
        assert saved.list_queries() == () and network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


def test_history_entry_manages_the_same_records_without_reexecuting(tmp_path):
    engine, root, bridge, session, saved, network = start(tmp_path)
    try:
        record = saved.create(SavedQueryDraft("From history", precise_definition()))
        click(root, item(root, "historyButton"))
        click(root, item(root, "openHistorySavedQueriesButton"))
        assert wait(lambda: item(root, "savedQueryPanel").property("opened"))
        assert session.saved_queries.queries[0]["key"] == record.query_id
        assert not session.saved_queries.capturedDraft["token"]
        press(root, "loadSavedQuery_" + record.query_id)
        assert wait(lambda: not item(root, "savedQueryPanel").property("opened"))
        assert item(root, "workspaceTabs").property("currentIndex") == 0
        assert bridge.draft_definition == record.definition and network.requests == []
        assert session.history_repository.latest_query() is None
    finally:
        close_m2_qml_test_engine(engine)
