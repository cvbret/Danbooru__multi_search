from __future__ import annotations

import pytest

from danbooru_multi_search.core.saved_queries import SavedQueryDraft, SavedQueryError
from danbooru_multi_search.storage.saved_queries import SavedQueryRepository
from danbooru_multi_search.ui.saved_queries import SavedQueryController
from tests.test_favorite_controller import build
from tests.test_query_state import _bridge
from tests.test_saved_queries import precise_definition


def setup(tmp_path, expression="base"):
    favorites, shared = build(tmp_path)
    saved = SavedQueryRepository(favorites._database)
    bridge, core, editor = _bridge(expression)
    controller = SavedQueryController(saved, bridge, shared._writes.access)
    return favorites, shared, saved, controller, bridge, core


def test_capture_and_reload_preserve_exact_unedited_definition(tmp_path):
    favorites, shared, saved, controller, bridge, core = setup(tmp_path)
    definition = precise_definition()
    assert bridge.loadDraftDefinition(definition)
    token = controller.captureDraft()
    controller.setActive(True)
    notifications = []
    shared.collectionChanged.connect(lambda: notifications.append(True))
    before = favorites.capture_collection()
    assert controller.saveCaptured(token, "Exact")
    record = saved.list_queries()[0]
    assert record.definition == definition
    assert bridge.applyAdvancedText("changed")
    assert controller.loadDraft(record.query_id, str(record.revision), controller.generation)
    assert bridge.draft_definition == definition and core.start_calls == []
    assert controller.queries[0]["mode"] == "普通查询"
    assert favorites.capture_collection() == before and notifications == []


def test_invalid_draft_error_survives_opening_the_panel(tmp_path):
    _favorites, _shared, saved, controller, bridge, core = setup(tmp_path, None)
    assert controller.captureDraft() == ""
    error = controller.errorText
    assert error
    controller.setActive(True)
    assert controller.errorText == error and controller.capturedDraft["token"] == ""
    assert saved.list_queries() == () and core.start_calls == []


def test_changed_draft_invalidates_pressed_capture_before_save(tmp_path):
    _favorites, _shared, saved, controller, bridge, core = setup(tmp_path)
    token = controller.captureDraft()
    controller.setActive(True)
    bridge.applyAdvancedText("another")
    assert not controller.saveCaptured(token, "Old")
    assert saved.list_queries() == () and core.start_calls == []


@pytest.mark.parametrize("action", ["save", "rename", "delete"])
def test_import_occupation_refuses_sql_writes(tmp_path, monkeypatch, action):
    _favorites, shared, saved, controller, bridge, core = setup(tmp_path)
    record = saved.create(SavedQueryDraft("Existing", precise_definition()))
    token = controller.captureDraft()
    controller.setActive(True)
    generation = controller.generation
    assert controller.prepareDelete(record.query_id, str(record.revision), generation)
    deletion = controller.deleteConfirmation["token"]
    assert shared._writes.access.begin_external_write("import")
    def forbidden(**kwargs):
        raise AssertionError("GUI must not enter SQL during an external write")
    monkeypatch.setattr(saved, "_connection", forbidden)
    operations = {"save": lambda: controller.saveCaptured(token, "New"),
                  "rename": lambda: controller.rename(record.query_id, str(record.revision), generation, "Changed"),
                  "delete": lambda: controller.confirmDelete(deletion)}
    assert not operations[action]()
    shared._writes.access.finish_external_write("import")


@pytest.mark.parametrize("reentry", ["draft", "reload", "close"])
def test_busy_notification_rechecks_source_and_lifecycle_before_sql(tmp_path, monkeypatch, reentry):
    _favorites, shared, saved, controller, bridge, core = setup(tmp_path)
    token = controller.captureDraft()
    controller.setActive(True)
    entered = []
    monkeypatch.setattr(saved, "create", lambda value: entered.append(value))
    action = {"draft": lambda: bridge.applyAdvancedText("changed"), "reload": controller.reload, "close": controller.shutdown}[reentry]
    shared.busyChanged.connect(lambda: action() if shared.busy else None)
    assert not controller.saveCaptured(token, "New")
    assert entered == [] and not shared.busy


def test_cancel_stale_revision_and_deleted_identity_never_delete_replacement(tmp_path):
    favorites, _shared, saved, controller, bridge, core = setup(tmp_path)
    record = saved.create(SavedQueryDraft("One", precise_definition()))
    controller.setActive(True)
    generation = controller.generation
    assert controller.prepareDelete(record.query_id, str(record.revision), generation)
    token = controller.deleteConfirmation["token"]
    controller.cancelDelete()
    assert not controller.confirmDelete(token)
    assert controller.prepareDelete(record.query_id, str(record.revision), generation)
    token = controller.deleteConfirmation["token"]
    renamed = SavedQueryRepository(favorites._database).rename(record, "Changed elsewhere")
    assert not controller.confirmDelete(token)
    assert not controller.loadDraft(record.query_id, str(record.revision), generation)
    assert bridge.advancedText == "base" and saved.list_queries() == (renamed,)
    controller.reload()
    assert not controller.rename(record.query_id, str(record.revision), generation, "Stale")


def test_failed_save_keeps_capture_and_existing_records(tmp_path, monkeypatch):
    _favorites, shared, saved, controller, bridge, core = setup(tmp_path)
    token = controller.captureDraft()
    controller.setActive(True)
    def fail(value):
        raise SavedQueryError("storage_error", "simulated save failure")
    monkeypatch.setattr(saved, "create", fail)
    assert not controller.saveCaptured(token, "New")
    assert controller.capturedDraft["token"] == token and "failure" in controller.errorText
    assert not shared.busy and saved.list_queries() == ()


def test_more_than_one_page_and_old_actions_after_reload(tmp_path):
    _favorites, _shared, saved, controller, bridge, core = setup(tmp_path)
    for index in range(53):
        saved.create(SavedQueryDraft(f"Query {index:03}", precise_definition()))
    controller.setActive(True)
    first, generation = controller.queries[0], controller.generation
    assert len(controller.queries) == 50 and controller.hasMore
    assert controller.loadMore() and len(controller.queries) == 53 and not controller.hasMore
    controller.reload()
    assert not controller.loadDraft(first["key"], first["revision"], generation)


def test_shutdown_during_load_notification_stops_late_ui_signals(tmp_path):
    _favorites, _shared, saved, controller, bridge, core = setup(tmp_path)
    record = saved.create(SavedQueryDraft("Exact", precise_definition()))
    controller.setActive(True)
    loaded = []
    controller.draftLoaded.connect(lambda: loaded.append(True))
    bridge.draftChanged.connect(controller.shutdown)
    assert not controller.loadDraft(record.query_id, str(record.revision), controller.generation)
    assert loaded == [] and core.start_calls == []


@pytest.mark.parametrize("phase", ["read", "bridge_notification", "controller_notification"])
def test_synchronous_draft_changes_invalidate_old_load(tmp_path, monkeypatch, phase):
    _favorites, _shared, saved, controller, bridge, core = setup(tmp_path)
    record = saved.create(SavedQueryDraft("Exact", precise_definition()))
    controller.setActive(True)
    loaded = []
    controller.draftLoaded.connect(lambda: loaded.append(True))
    def replace_draft():
        if bridge.advancedText != "newer":
            bridge.applyAdvancedText("newer")
    if phase == "read":
        original = saved.list_queries
        def read(**kwargs):
            result = original(**kwargs)
            replace_draft()
            return result
        monkeypatch.setattr(saved, "list_queries", read)
    elif phase == "bridge_notification":
        bridge.draftChanged.connect(replace_draft)
    else:
        controller.changed.connect(replace_draft)
    assert not controller.loadDraft(record.query_id, str(record.revision), controller.generation)
    assert bridge.advancedText == "newer" and loaded == [] and core.start_calls == []


def test_bridge_load_failure_remains_visible_in_saved_query_panel(tmp_path, monkeypatch):
    _favorites, _shared, saved, controller, bridge, core = setup(tmp_path)
    record = saved.create(SavedQueryDraft("Exact", precise_definition()))
    controller.setActive(True)
    monkeypatch.setattr(bridge, "loadDraftDefinition", lambda definition: False)
    assert not controller.loadDraft(record.query_id, str(record.revision), controller.generation)
    assert controller.errorText and core.start_calls == []
