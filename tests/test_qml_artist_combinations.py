from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtTest import QTest

from danbooru_multi_search.core.bootstrap import initialize_runtime
from danbooru_multi_search.core.artist_combinations import ArtistCombinationDraft, ArtistCombinationMode
from danbooru_multi_search.core.favorites import FavoriteCandidate, FavoriteTagCategory
from danbooru_multi_search.storage.artist_combinations import ArtistCombinationRepository
from danbooru_multi_search.storage.favorites import FavoriteRepository
from tests.m2_qml_harness import create_m2_qml_test_engine, close_m2_qml_test_engine
from tests.test_favorite_controller import APPLICATION, post
from tests.test_qml_favorites import NoNetwork, item, click, wait


def start(tmp_path, *, mode=None, narrow=False):
    runtime = initialize_runtime(tmp_path)
    favorites = FavoriteRepository(runtime.database)
    artists = tuple(favorites.add_favorite(FavoriteCandidate.for_tag(name, FavoriteTagCategory.ARTIST))
                    for name in ("artist_a", "artist_b"))
    favorites.add_favorite(FavoriteCandidate.for_tag("cat", FavoriteTagCategory.GENERAL))
    saved = ArtistCombinationRepository(runtime.database)
    combination = saved.create_combination(ArtistCombinationDraft("Pair", ("artist_b", "artist_a"), mode)) if mode else None
    network = NoNetwork()
    engine, bridge = create_m2_qml_test_engine(APPLICATION, danbooru_base_url="https://example.invalid", runtime=runtime, network_manager=network)
    root = engine.rootObjects()[0]
    root.setWidth(820 if narrow else 1280)
    root.setHeight(600 if narrow else 820)
    bridge.postsModel.set_posts((post(42),))
    root.requestActivate()
    assert QTest.qWaitForWindowExposed(root)
    return engine, root, bridge, bridge.parent(), favorites, saved, artists, combination, network


def press(root, name):
    control = item(root, name)
    viewport = item(root, "artistCombinationScroll").property("contentItem")
    center = control.mapToScene(QPointF(control.width() / 2, control.height() / 2))
    middle = viewport.mapToScene(QPointF(0, viewport.height() / 2))
    maximum = max(0, viewport.property("contentHeight") - viewport.height())
    viewport.setProperty("contentY", max(0, min(viewport.property("contentY") + center.y() - middle.y(), maximum)))
    APPLICATION.processEvents()
    click(root, control)


def open_query(root):
    button = item(root, "openQueryDrawerButton")
    if button.property("visible"):
        click(root, button)
    click(root, item(root, "openQueryArtistCombinationsButton"))
    assert wait(lambda: item(root, "artistCombinationPanel").property("opened"))


def test_narrow_favorite_entry_saves_renames_and_confirms_delete_with_actual_clicks(tmp_path):
    engine, root, bridge, session, favorites, saved, artists, _combination, network = start(tmp_path, narrow=True)
    before = favorites.capture_collection()
    try:
        click(root, item(root, "favoritesNavigationButton"))
        click(root, item(root, "openFavoriteArtistCombinationsButton"))
        assert wait(lambda: item(root, "artistCombinationPanel").property("opened"))
        panel = item(root, "artistCombinationPanel")
        assert panel.property("width") <= 788 and panel.property("height") <= 568
        press(root, "savedArtistChoice_" + str(artists[1].id))
        press(root, "savedArtistChoice_" + str(artists[0].id))
        assert session.artist_combinations.selectedCount == 2
        mode = item(root, "savedArtistMode")
        mode.setProperty("currentIndex", 1)
        item(root, "artistCombinationNameInput").setProperty("text", "My Pair")
        press(root, "saveArtistCombinationButton")
        assert wait(lambda: len(saved.list_combinations()) == 1)
        combination = saved.list_combinations()[0]
        assert combination.artists == ("artist_b", "artist_a") and combination.mode is ArtistCombinationMode.ANY
        key = str(combination.combination_id)
        press(root, "renameArtistCombination_" + key)
        assert wait(lambda: item(root, "renameArtistCombinationDialog").property("opened"))
        item(root, "artistCombinationRenameInput").setProperty("text", "Renamed Pair")
        click(root, item(root, "confirmArtistCombinationRenameButton"))
        assert wait(lambda: not item(root, "renameArtistCombinationDialog").property("opened"))
        assert saved.list_combinations()[0].name == "Renamed Pair"
        press(root, "deleteArtistCombination_" + key)
        assert wait(lambda: item(root, "deleteArtistCombinationDialog").property("opened"))
        click(root, item(root, "cancelArtistCombinationDeleteButton"))
        assert len(saved.list_combinations()) == 1
        press(root, "deleteArtistCombination_" + key)
        assert wait(lambda: item(root, "deleteArtistCombinationDialog").property("opened"))
        click(root, item(root, "confirmArtistCombinationDeleteButton"))
        assert saved.list_combinations() == ()
        assert favorites.capture_collection() == before
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


@pytest.mark.parametrize("mode,expected", [
    (ArtistCombinationMode.ALL, "base AND artist_b AND artist_a"),
    (ArtistCombinationMode.ANY, "base AND (artist_b OR artist_a)"),
    (ArtistCombinationMode.EXCLUDE, "base AND NOT artist_b AND NOT artist_a"),
])
def test_applying_combination_only_edits_draft_without_queries_or_focus_completion(tmp_path, mode, expected):
    engine, root, bridge, session, favorites, saved, artists, combination, network = start(tmp_path, mode=mode)
    try:
        assert bridge.applyAdvancedText("base")
        item(root, "tagsInput").forceActiveFocus()
        open_query(root)
        before_posts, before_revision = bridge.postsModel.posts, bridge.postsModel.revision
        before_history = session.history_repository.latest_query()
        press(root, "applyArtistCombination_" + str(combination.combination_id))
        assert wait(lambda: not item(root, "artistCombinationPanel").property("opened")), (bridge.advancedText, session.artist_combinations.errorText, session.artist_combinations._active, session.artist_combinations.generation)
        QTest.qWait(450)
        APPLICATION.processEvents()
        assert bridge.advancedText == expected
        assert bridge.postsModel.posts is before_posts and bridge.postsModel.revision == before_revision
        assert session.history_repository.latest_query() == before_history is None
        assert network.requests == []
    finally:
        close_m2_qml_test_engine(engine)


def test_empty_exclusion_stays_visible_with_error_then_favorite_selection_returns_to_query(tmp_path):
    engine, root, bridge, session, favorites, saved, artists, combination, network = start(tmp_path, mode=ArtistCombinationMode.EXCLUDE, narrow=True)
    try:
        click(root, item(root, "favoritesNavigationButton"))
        click(root, item(root, "openFavoriteArtistCombinationsButton"))
        assert wait(lambda: item(root, "artistCombinationPanel").property("opened"))
        press(root, "applyArtistCombination_" + str(combination.combination_id))
        assert item(root, "artistCombinationPanel").property("visible")
        assert session.artist_combinations.errorText and bridge.advancedText == ""
        press(root, "savedArtistChoice_" + str(artists[0].id))
        press(root, "applySavedArtistSelectionButton")
        assert wait(lambda: not item(root, "artistCombinationPanel").property("opened"))
        assert root.property("activePage") == "browse"
        assert wait(lambda: item(root, "queryDrawer").property("opened"))
        assert bridge.advancedText == "artist_a"
        QTest.qWait(450)
        assert network.requests == [] and session.history_repository.latest_query() is None
    finally:
        close_m2_qml_test_engine(engine)
