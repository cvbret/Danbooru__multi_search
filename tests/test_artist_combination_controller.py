from __future__ import annotations

import pytest

from danbooru_multi_search.core.artist_combinations import ArtistCombinationDraft, ArtistCombinationError, ArtistCombinationMode
from danbooru_multi_search.core.favorites import FavoriteCandidate, FavoriteTagCategory
from danbooru_multi_search.storage.artist_combinations import ArtistCombinationRepository
from danbooru_multi_search.storage.favorites import FavoriteRepository
from danbooru_multi_search.ui.artist_combinations import ArtistCombinationController
from tests.test_favorite_controller import build
from tests.test_query_state import _bridge


def setup(tmp_path, expression="base"):
    favorites, shared = build(tmp_path)
    artist_a = favorites.add_favorite(FavoriteCandidate.for_tag("artist_a", FavoriteTagCategory.ARTIST))
    artist_b = favorites.add_favorite(FavoriteCandidate.for_tag("artist_b", FavoriteTagCategory.ARTIST))
    ordinary = favorites.add_favorite(FavoriteCandidate.for_tag("cat", FavoriteTagCategory.GENERAL))
    combinations = ArtistCombinationRepository(favorites._database)
    bridge, core, _editor = _bridge(expression)
    controller = ArtistCombinationController(combinations, shared, bridge)
    controller.setActive(True)
    return favorites, shared, combinations, controller, bridge, core, (artist_a, artist_b, ordinary)


def select(controller, *items):
    for item in items:
        assert controller.toggleArtist(str(item.id), controller.generation, True)
    return controller.generation, controller.selectionRevision


@pytest.mark.parametrize("mode,expected", [
    ("all", "base AND artist_b AND artist_a"),
    ("any", "base AND (artist_b OR artist_a)"),
    ("exclude", "base AND NOT artist_b AND NOT artist_a"),
])
def test_saved_artist_selection_uses_existing_tree_and_never_executes(tmp_path, mode, expected):
    favorites, shared, saved, controller, bridge, core, items = setup(tmp_path)
    before = favorites.capture_collection()
    assert [value["name"] for value in controller.artists] == ["artist_b", "artist_a"]
    assert not controller.toggleArtist(str(items[2].id), controller.generation, True)
    generation, revision = select(controller, items[1], items[0])
    applied = []
    controller.draftApplied.connect(lambda: applied.append(True))
    assert controller.applySelection(generation, revision, mode, None)
    assert bridge.canonicalExpression == expected
    assert len(applied) == 1 and core.start_calls == []
    assert favorites.capture_collection() == before


def test_empty_exclusion_uses_original_error_and_preserves_draft(tmp_path):
    _favorites, _shared, _saved, controller, bridge, core, items = setup(tmp_path, None)
    generation, revision = select(controller, items[0])
    assert not controller.applySelection(generation, revision, "exclude", None)
    assert controller.errorText and bridge.advancedText == "" and core.start_calls == []


def test_stale_artist_category_is_rechecked_before_save_or_apply(tmp_path):
    favorites, shared, saved, controller, bridge, core, items = setup(tmp_path)
    generation, revision = select(controller, items[0])
    # A second repository changes the category without delivering a GUI event.
    FavoriteRepository(favorites._database).refresh_metadata(FavoriteCandidate.for_tag("artist_a", FavoriteTagCategory.GENERAL))
    assert not controller.applySelection(generation, revision, "all", None)
    assert not controller.saveSelection(generation, revision, "Wrong", "all")
    assert saved.list_combinations() == () and bridge.advancedText == "base"
    assert "已改变" in controller.errorText


def test_saved_combination_is_independent_after_artist_favorites_are_removed(tmp_path):
    favorites, shared, saved, controller, bridge, core, items = setup(tmp_path)
    generation, revision = select(controller, items[1], items[0])
    notifications = []
    shared.collectionChanged.connect(lambda: notifications.append(True))
    before_revision = favorites.capture_collection().revision
    assert controller.saveSelection(generation, revision, "Pair", "any")
    combination = saved.list_combinations()[0]
    assert combination.artists == ("artist_b", "artist_a")
    assert favorites.capture_collection().revision == before_revision and notifications == []
    favorites.delete_favorites((items[0].id, items[1].id))
    shared.collectionChanged.emit()
    assert controller.artists == []
    assert controller.applyCombination(str(combination.combination_id), controller.generation, None)
    assert bridge.advancedText == "base AND (artist_b OR artist_a)" and core.start_calls == []


def test_external_import_occupation_blocks_all_combination_writes(tmp_path, monkeypatch):
    _favorites, shared, saved, controller, _bridge, _core, items = setup(tmp_path)
    existing = saved.create_combination(ArtistCombinationDraft("Pair", ("artist_a",), ArtistCombinationMode.ALL))
    controller.reload()
    generation, revision = select(controller, items[0])
    assert controller.prepareDeleteCombination(str(existing.combination_id), generation)
    token = controller.deleteConfirmation["token"]
    assert shared.begin_external_write("import")
    def forbidden(*args, **kwargs):
        raise AssertionError("GUI must not wait for the import's SQLite write lock")
    monkeypatch.setattr(saved, "_connection", forbidden)
    assert not controller.saveSelection(generation, revision, "New", "all")
    assert not controller.renameCombination(str(existing.combination_id), generation, "Changed")
    assert not controller.confirmDelete(token)
    shared.finish_external_write("import")


@pytest.mark.parametrize("reentry", ["clear", "reload", "close"])
def test_busy_notification_reentry_prevents_sql_before_transaction(tmp_path, monkeypatch, reentry):
    _favorites, shared, saved, controller, _bridge, _core, items = setup(tmp_path)
    generation, revision = select(controller, items[0])
    entered = []
    monkeypatch.setattr(saved, "create_combination", lambda _draft: entered.append(True))
    action = {"clear": controller.clearSelection, "reload": controller.reload, "close": controller.shutdown}[reentry]
    shared.busyChanged.connect(lambda: action() if shared.busy else None)
    assert not controller.saveSelection(generation, revision, "New", "all")
    assert entered == [] and not shared.busy


def test_delete_confirmation_cancel_and_foreign_change_do_not_delete(tmp_path):
    favorites, shared, saved, controller, _bridge, _core, _items = setup(tmp_path)
    original = saved.create_combination(ArtistCombinationDraft("One", ("artist_a",), ArtistCombinationMode.ALL))
    controller.reload()
    key, generation = str(original.combination_id), controller.generation
    assert controller.prepareDeleteCombination(key, generation)
    token = controller.deleteConfirmation["token"]
    controller.cancelDelete()
    assert not controller.confirmDelete(token)
    assert controller.prepareDeleteCombination(key, generation)
    token = controller.deleteConfirmation["token"]
    renamed = ArtistCombinationRepository(favorites._database).rename_combination(original, "Changed elsewhere")
    assert not controller.confirmDelete(token)
    assert saved.list_combinations() == (renamed,)
    controller.reload()
    assert not controller.renameCombination(key, generation, "Stale click")


def test_failed_save_keeps_selection_and_confirmed_favorite_facts(tmp_path, monkeypatch):
    favorites, shared, saved, controller, _bridge, _core, items = setup(tmp_path)
    shared.observe_items(items)
    before, before_state = favorites.capture_collection(), shared.state(items[0].identity)
    generation, revision = select(controller, items[0])
    def fail(*args):
        raise ArtistCombinationError("storage_error", "simulated failure")
    monkeypatch.setattr(saved, "create_combination", fail)
    assert not controller.saveSelection(generation, revision, "New", "all")
    assert controller.selectedCount == 1 and controller.generation == generation
    assert controller.errorText == "simulated failure"
    assert favorites.capture_collection() == before and shared.state(items[0].identity) == before_state
    assert not shared.busy


def test_selection_inserts_into_requested_group_and_invalid_draft_keeps_previous_tree(tmp_path):
    _favorites, _shared, _saved, controller, bridge, core, items = setup(tmp_path, "base AND (cat OR dog)")
    generation, revision = select(controller, items[0])
    assert controller.applySelection(generation, revision, "all", [1])
    assert bridge.advancedText == "base AND (cat OR dog OR artist_a)"
    previous = bridge.canonicalExpression
    assert not bridge.applyAdvancedText("cat AND")
    assert not controller.applySelection(generation, revision, "any", None)
    assert bridge.advancedText == "cat AND" and bridge.canonicalExpression == previous
    assert core.start_calls == []


def test_favorite_source_loads_more_than_one_thousand_and_old_selection_is_invalidated(tmp_path):
    favorites, shared = build(tmp_path)
    favorites.add_favorites(tuple(FavoriteCandidate.for_tag(f"artist_{index}", FavoriteTagCategory.ARTIST) for index in range(1001)))
    bridge, _core, _editor = _bridge("base")
    controller = ArtistCombinationController(ArtistCombinationRepository(favorites._database), shared, bridge)
    controller.setActive(True)
    assert len(controller.artists) == 1001
    row = controller.artists[-1]
    assert controller.toggleArtist(row["key"], controller.generation, True)
    generation, revision = controller.generation, controller.selectionRevision
    shared.collectionChanged.emit()
    assert controller.selectedCount == 0
    assert not controller.applySelection(generation, revision, "all", None)
