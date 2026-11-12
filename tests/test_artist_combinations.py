from __future__ import annotations

import sys

# Run this same file as a fresh-process dependency check, without pytest/storage.
if sys.argv[-1:] == ["--pure-core-check"]:
    import builtins
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"PySide6", "sqlite3", "json"} or name.startswith(("danbooru_multi_search.storage", "danbooru_multi_search.ui")):
            raise AssertionError(name)
        return original_import(name, *args, **kwargs)
    builtins.__import__ = guarded_import
    from danbooru_multi_search.core.artist_combinations import ArtistCombinationDraft, ArtistCombinationMode
    assert ArtistCombinationDraft(" Pair ", ("Artist_A", "artist_b", "artist_a"), ArtistCombinationMode.ANY).artists == ("artist_a", "artist_b")
    sys.exit(0)

import os
import subprocess
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from danbooru_multi_search.core.artist_combinations import (
    ArtistCombinationDraft, ArtistCombinationError, ArtistCombinationMode,
)
from danbooru_multi_search.core.favorites import FavoriteCandidate, FavoriteTagCategory
from danbooru_multi_search.storage.artist_combinations import ArtistCombinationRepository
import danbooru_multi_search.storage.database as database_module
from danbooru_multi_search.storage.database import Database, DatabaseError
from danbooru_multi_search.storage.favorite_transfer import FavoriteImportMode, FavoriteTransferDocument, FavoriteTransferService
from danbooru_multi_search.storage.favorites import FavoriteRepository
from danbooru_multi_search.storage.history import HistoryRepository
from danbooru_multi_search.ui.window_state import WindowGeometry


def repository(tmp_path):
    database = Database(tmp_path / "application.sqlite3")
    database.initialize()
    return database, ArtistCombinationRepository(database)


def test_model_constructs_in_fresh_process_without_qt_storage_json_or_sqlite():
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--pure-core-check"], env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("mode", tuple(ArtistCombinationMode))
def test_repository_restart_preserves_order_mode_and_unicode_name(tmp_path, mode):
    database, saved = repository(tmp_path)
    draft = ArtistCombinationDraft("  Cafe\u0301  ", ("Artist_B", "artist_a", "artist_b"), mode)
    item = saved.create_combination(draft)
    assert item.name == "Café" and item.artists == ("artist_b", "artist_a")
    assert ArtistCombinationRepository(Database(database.path)).list_combinations() == (item,)
    with pytest.raises(FrozenInstanceError):
        item.name = "changed"


@pytest.mark.parametrize("name,artists,mode", [
    (" ", ("artist",), ArtistCombinationMode.ALL),
    ("bad\x00name", ("artist",), ArtistCombinationMode.ALL),
    ("Name", (), ArtistCombinationMode.ALL),
    ("Name", ("rating:g",), ArtistCombinationMode.ALL),
    ("Name", (True,), ArtistCombinationMode.ALL),
    ("Name", ("artist",), "all"),
])
def test_invalid_values_never_reach_storage(name, artists, mode):
    with pytest.raises(ArtistCombinationError):
        ArtistCombinationDraft(name, artists, mode)


def test_duplicate_names_and_stale_records_do_not_change_existing_combinations(tmp_path):
    database, saved = repository(tmp_path)
    first = saved.create_combination(ArtistCombinationDraft("One", ("artist_a",), ArtistCombinationMode.ALL))
    second = saved.create_combination(ArtistCombinationDraft("Two", ("artist_b",), ArtistCombinationMode.ANY))
    with pytest.raises(ArtistCombinationError, match="同名"):
        saved.create_combination(ArtistCombinationDraft(" one ", ("artist_c",), ArtistCombinationMode.EXCLUDE))
    with pytest.raises(ArtistCombinationError, match="同名"):
        saved.rename_combination(second, "ONE")
    renamed = saved.rename_combination(first, "Renamed")
    with pytest.raises(ArtistCombinationError) as caught:
        saved.delete_combination(first)
    assert caught.value.code == "stale_combination"
    assert set(saved.list_combinations()) == {renamed, second}
    saved.delete_combination(renamed)
    saved.delete_combination(second)
    replacement = saved.create_combination(ArtistCombinationDraft("New", ("artist_c",), ArtistCombinationMode.ALL))
    assert replacement.combination_id > second.combination_id


def test_combinations_survive_favorite_removal_and_replace_without_changing_revision_or_json(tmp_path):
    database, saved = repository(tmp_path)
    favorites = FavoriteRepository(database)
    artist = favorites.add_favorite(FavoriteCandidate.for_tag("artist_a", FavoriteTagCategory.ARTIST))
    service = FavoriteTransferService(favorites)
    before = favorites.capture_collection()
    combination = saved.create_combination(ArtistCombinationDraft("Artists", ("artist_a",), ArtistCombinationMode.EXCLUDE))
    renamed = saved.rename_combination(combination, "Artists renamed")
    assert favorites.capture_collection() == before
    from danbooru_multi_search.storage.favorite_transfer import encode_favorite_transfer_document
    encoded = encode_favorite_transfer_document(service.create_export_document())
    assert b'artist_combinations' not in encoded and b'Artists renamed' not in encoded
    favorites.delete_favorite(artist.id)
    empty = FavoriteTransferDocument(datetime.now(timezone.utc), (), ())
    service.apply_import(service.preview_import(empty, FavoriteImportMode.REPLACE))
    assert saved.list_combinations() == (renamed,)
    revision = favorites.capture_collection().revision
    saved.delete_combination(renamed)
    assert favorites.capture_collection().revision == revision


def test_version_six_preserves_all_version_five_rows_and_is_idempotent(tmp_path, monkeypatch):
    migrations = database_module.MIGRATIONS
    monkeypatch.setattr(database_module, "MIGRATIONS", migrations[:5])
    database = Database(tmp_path / "application.sqlite3")
    assert database.initialize() == 5
    favorites = FavoriteRepository(database)
    folder = favorites.create_folder("Saved")
    favorites.add_favorite(FavoriteCandidate.for_tag("artist_a", FavoriteTagCategory.ARTIST), folder_id=folder.id)
    history = HistoryRepository(database)
    query = history.record_accepted_query(display_text="cat", normalized_tags=("cat",), rating="all", page_size=40, page=2, count_tag_usage=True)
    history.save_browser_state(geometry=WindowGeometry(10, 20, 900, 700), is_maximized=False, active_query_id=query.id, gallery_position=None)
    tables = ("app_metadata", "recent_queries", "recent_tags", "browser_session", "favorite_folders", "favorite_items", "favorite_post_tags", "favorite_collection_state")
    def capture():
        with database.connect() as connection:
            return {name: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {name} ORDER BY rowid")) for name in tables}
    before = capture()
    monkeypatch.setattr(database_module, "MIGRATIONS", migrations)
    assert database.initialize() == database.initialize() == 6
    assert capture() == before
    assert ArtistCombinationRepository(database).list_combinations() == ()


def test_failed_version_six_rolls_back_new_table_and_keeps_version_five(tmp_path, monkeypatch):
    migrations = database_module.MIGRATIONS
    monkeypatch.setattr(database_module, "MIGRATIONS", migrations[:5])
    database = Database(tmp_path / "application.sqlite3")
    assert database.initialize() == 5
    failure = replace(migrations[5], statements=migrations[5].statements + ("INSERT INTO missing_table VALUES (1)",))
    monkeypatch.setattr(database_module, "MIGRATIONS", migrations[:5] + (failure,))
    with pytest.raises(DatabaseError):
        database.initialize()
    assert database.current_version() == 5
    with database.connect() as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'artist_combinations'").fetchall()
