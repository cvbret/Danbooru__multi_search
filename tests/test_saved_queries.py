from __future__ import annotations

import sys

if sys.argv[-1:] == ["--pure-core-check"]:
    import builtins
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"PySide6", "sqlite3", "json"} or name.startswith(("danbooru_multi_search.storage", "danbooru_multi_search.ui")):
            raise AssertionError(name)
        return original_import(name, *args, **kwargs)
    builtins.__import__ = guarded_import
    from danbooru_multi_search.core.saved_queries import SavedQueryDraft
    from danbooru_multi_search.danbooru.query_definition import GalleryQueryDefinition, QueryMode
    from danbooru_multi_search.danbooru.query_planning import QueryRating
    from danbooru_multi_search.danbooru.query_types import QueryExecutionLimits
    from danbooru_multi_search.danbooru.result_processing import ResultFilterSpec, ResultSortSpec
    definition = GalleryQueryDefinition(QueryMode.LATEST, None, QueryRating.ALL, ResultFilterSpec(), ResultSortSpec(), QueryExecutionLimits())
    assert SavedQueryDraft("  Cafe\u0301  ", definition).name == "Café"
    sys.exit(0)

import os
import subprocess
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

import pytest

from danbooru_multi_search.core.saved_queries import SavedQuery, SavedQueryDraft, SavedQueryError
from danbooru_multi_search.core.artist_combinations import ArtistCombinationDraft, ArtistCombinationMode
from danbooru_multi_search.core.downloads import DownloadRequest
from danbooru_multi_search.core.favorites import FavoriteCandidate, FavoriteTagCategory
import danbooru_multi_search.storage.database as database_module
from danbooru_multi_search.storage.database import Database, DatabaseError
from danbooru_multi_search.storage.saved_queries import SavedQueryRepository
from danbooru_multi_search.storage.artist_combinations import ArtistCombinationRepository
from danbooru_multi_search.storage.downloads import DownloadRepository
from danbooru_multi_search.storage.favorites import FavoriteRepository
from danbooru_multi_search.storage.favorite_transfer import FavoriteTransferDocument, FavoriteTransferService, FavoriteImportMode
from danbooru_multi_search.storage.history import HistoryRepository
from danbooru_multi_search.danbooru.query_definition import QueryMode
from danbooru_multi_search.danbooru.result_processing import ResultFilterSpec
from tests.test_query_serialization import _definition


def precise_definition(mode=QueryMode.STANDARD):
    return _definition(None if mode is QueryMode.LATEST else "cat AND (dog OR bird)", mode=mode, filters=ResultFilterSpec(
        minimum_score=-7, published_from_utc=datetime(2020, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc),
        minimum_age=timedelta(days=1, seconds=2, microseconds=987654),
        minimum_aspect_ratio=Fraction(123456789, 987654321)))


def repository(tmp_path):
    database = Database(tmp_path / "application.sqlite3")
    database.initialize()
    return database, SavedQueryRepository(database)


def test_model_constructs_without_qt_sqlite_json_or_storage_in_fresh_process():
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--pure-core-check"], env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("mode", tuple(QueryMode))
def test_restart_keeps_full_definition_precision_and_immutable_identity(tmp_path, mode):
    database, saved = repository(tmp_path)
    definition = precise_definition(mode)
    record = saved.create(SavedQueryDraft("  Cafe\u0301  ", definition))
    assert SavedQueryRepository(Database(database.path)).list_queries() == (record,)
    assert record.name == "Café" and record.definition == definition
    with pytest.raises(FrozenInstanceError):
        record.revision = 9


@pytest.mark.parametrize("name", ["", " \t ", "bad\x00name", "bad\nname", 1])
def test_invalid_names_are_rejected(name):
    with pytest.raises(SavedQueryError, match="名称"):
        SavedQueryDraft(name, precise_definition())


def test_duplicate_names_and_stale_revision_cannot_change_a_new_record(tmp_path):
    database, saved = repository(tmp_path)
    first = saved.create(SavedQueryDraft("One", precise_definition()))
    second = saved.create(SavedQueryDraft("Two", precise_definition(QueryMode.LATEST)))
    with pytest.raises(SavedQueryError, match="同名"):
        saved.create(SavedQueryDraft(" ONE ", precise_definition()))
    with pytest.raises(SavedQueryError, match="同名"):
        saved.rename(second, "one")
    renamed = saved.rename(first, "Renamed")
    assert renamed.query_id == first.query_id and renamed.revision == first.revision + 1
    for operation in (lambda: saved.rename(first, "Stale"), lambda: saved.delete(first)):
        with pytest.raises(SavedQueryError) as caught:
            operation()
        assert caught.value.code == "stale_query"
    saved.delete(renamed)
    replacement = saved.create(SavedQueryDraft("One", precise_definition()))
    assert replacement.query_id != first.query_id
    with pytest.raises(SavedQueryError):
        saved.delete(first)
    assert set(saved.list_queries()) == {second, replacement}


def test_revision_exhaustion_and_paging_keep_confirmed_records(tmp_path):
    database, saved = repository(tmp_path)
    first = saved.create(SavedQueryDraft("A", precise_definition()))
    second = saved.create(SavedQueryDraft("B", precise_definition()))
    assert saved.list_queries(limit=1, offset=1) == (second,)
    with database.connect() as connection:
        connection.execute("UPDATE saved_queries SET revision = ? WHERE id = ?", (2**63 - 1, first.query_id))
    maximum = saved.list_queries(limit=1)[0]
    assert saved.rename(maximum, "A") == maximum
    with pytest.raises(SavedQueryError) as caught:
        saved.rename(maximum, "Changed")
    assert caught.value.code == "revision_exhausted"


@pytest.mark.parametrize("field,value", [("definition_version", 2), ("definition_json", "{}"), ("normalized_name", "wrong")])
def test_damaged_or_future_records_have_an_explicit_error(tmp_path, field, value):
    database, saved = repository(tmp_path)
    record = saved.create(SavedQueryDraft("A", precise_definition()))
    with database.connect() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(f"UPDATE saved_queries SET {field} = ? WHERE id = ?", (value, record.query_id))
    with pytest.raises(SavedQueryError) as caught:
        saved.list_queries()
    assert caught.value.code == "invalid_record"


def test_write_failure_rolls_back_without_overwriting_existing_name(tmp_path):
    database, saved = repository(tmp_path)
    first = saved.create(SavedQueryDraft("One", precise_definition()))
    with database.connect() as connection:
        connection.execute("CREATE TRIGGER reject_rename BEFORE UPDATE ON saved_queries BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
    with pytest.raises(SavedQueryError) as caught:
        saved.rename(first, "Changed")
    assert caught.value.code == "storage_error" and saved.list_queries() == (first,)


def test_named_queries_are_independent_of_history_and_favorite_replace(tmp_path):
    database, saved = repository(tmp_path)
    record = saved.create(SavedQueryDraft("Complete", precise_definition()))
    favorites = FavoriteRepository(database)
    favorites.add_favorite(FavoriteCandidate.for_tag("artist", FavoriteTagCategory.ARTIST))
    before = favorites.capture_collection()
    renamed = saved.rename(record, "Renamed")
    assert favorites.capture_collection() == before
    history = HistoryRepository(database)
    history.record_accepted_definition(definition=record.definition, display_text="Exact", page=1, count_tag_usage=True)
    history.clear_history()
    service = FavoriteTransferService(favorites)
    document = FavoriteTransferDocument(datetime.now(timezone.utc), (), ())
    service.apply_import(service.preview_import(document, FavoriteImportMode.REPLACE))
    assert saved.list_queries() == (renamed,)
    assert "saved_queries" not in repr(service.create_export_document())


@pytest.mark.parametrize("fail", [False, True])
def test_version_eight_only_adds_table_and_rolls_back_atomically(tmp_path, monkeypatch, fail):
    migrations = database_module.MIGRATIONS[:8]
    monkeypatch.setattr(database_module, "MIGRATIONS", migrations[:7])
    database = Database(tmp_path / "application.sqlite3")
    assert database.initialize() == 7
    favorites = FavoriteRepository(database)
    folder = favorites.create_folder("Saved")
    favorites.add_favorite(FavoriteCandidate.for_tag("artist", FavoriteTagCategory.ARTIST), folder_id=folder.id)
    ArtistCombinationRepository(database).create_combination(ArtistCombinationDraft("Pair", ("artist",), ArtistCombinationMode.ALL))
    HistoryRepository(database).record_accepted_definition(definition=precise_definition(), display_text="Exact", page=1, count_tag_usage=True)
    DownloadRepository(database).create_batch((DownloadRequest(42, "https://example.invalid/42.jpg", "jpg"),), tmp_path / "downloads")
    with database.connect() as connection:
        tables = tuple(row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations' ORDER BY name"))
    def capture():
        with database.connect() as connection:
            return {name: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {name} ORDER BY rowid")) for name in tables}
    before = capture()
    if fail:
        broken = replace(migrations[7], statements=migrations[7].statements + ("INSERT INTO missing_table VALUES (1)",))
        monkeypatch.setattr(database_module, "MIGRATIONS", migrations[:7] + (broken,))
        with pytest.raises(DatabaseError):
            database.initialize()
        with database.connect() as connection:
            assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 7
            assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'saved_queries'").fetchone() is None
    else:
        monkeypatch.setattr(database_module, "MIGRATIONS", migrations)
        assert database.initialize() == database.initialize() == 8
        assert SavedQueryRepository(database).list_queries() == ()
    assert capture() == before
