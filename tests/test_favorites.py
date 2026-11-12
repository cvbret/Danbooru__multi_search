from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import danbooru_multi_search.storage.favorites as favorites_module
from danbooru_multi_search.danbooru import DanbooruPost
from danbooru_multi_search.core.favorites import (
    FavoriteCandidate,
    FavoriteError,
    FavoriteFilter,
    FavoriteFolderScope,
    FavoriteIdentity,
    FavoriteObjectType,
    FavoritePoolCategory,
    FavoriteSort,
    FavoriteSortDirection,
    FavoriteSortField,
    FavoriteTagCategory,
    FolderDeleteMode,
)
from danbooru_multi_search.storage import (
    FavoriteRepository,
)
from danbooru_multi_search.storage.database import Database


BASE_TIME = datetime(2026, 11, 10, 1, 2, 3, 456789, tzinfo=timezone.utc)


def _post(
    post_id: int,
    *,
    score: int | None = -3,
    fav_count: int | None = 0,
    rating: str = "q",
    artist: str = "sample_artist",
    general: str = "blue_sky",
) -> DanbooruPost:
    return DanbooruPost(
        id=post_id,
        preview_url=f"https://example.invalid/{post_id}/preview.jpg",
        large_file_url=f"https://example.invalid/{post_id}/large.jpg",
        file_url=f"https://example.invalid/{post_id}/original.png",
        score=score,
        fav_count=fav_count,
        rating=rating,
        width=2048,
        height=1024,
        file_ext="png",
        created_at="2026-11-10T01:02:03.456789Z",
        file_size=123456,
        source="source",
        artist_tags=(artist,),
        character_tags=("sample_character",),
        copyright_tags=("sample_series",),
        general_tags=(general,),
        meta_tags=("highres",),
    )


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "application.sqlite3")
    assert value.initialize() == 5
    return value


@pytest.fixture
def repository(database) -> FavoriteRepository:
    return FavoriteRepository(database, clock=lambda: BASE_TIME)


def test_storage_exports_favorite_public_api():
    import danbooru_multi_search.storage as storage

    expected = {
        "FavoriteCandidate",
        "FavoriteError",
        "FavoriteFilter",
        "FavoriteFolder",
        "FavoriteFolderScope",
        "FavoriteIdentity",
        "FavoriteItem",
        "FavoriteObjectType",
        "FavoritePoolCategory",
        "FavoriteRepository",
        "FavoriteSort",
        "FavoriteSortDirection",
        "FavoriteSortField",
        "FavoriteTagCategory",
        "FolderDeleteMode",
        "POST_SNAPSHOT_VERSION",
        "PostSnapshotError",
        "decode_post_snapshot",
        "encode_post_snapshot",
    }

    assert expected <= set(storage.__all__)
    assert all(hasattr(storage, name) for name in expected)


def test_six_visible_object_kinds_round_trip_after_reopening(database):
    repository = FavoriteRepository(database, clock=lambda: BASE_TIME)
    candidates = (
        FavoriteCandidate.for_post(_post(1), display_name="作品 1"),
        FavoriteCandidate.for_tag("artist_tag", FavoriteTagCategory.ARTIST),
        FavoriteCandidate.for_tag("character_tag", FavoriteTagCategory.CHARACTER),
        FavoriteCandidate.for_tag("copyright_tag", FavoriteTagCategory.COPYRIGHT),
        FavoriteCandidate.for_tag("general_tag", FavoriteTagCategory.GENERAL),
        FavoriteCandidate.for_pool(99, "系列作品池"),
    )
    created = tuple(repository.add_favorite(candidate) for candidate in candidates)

    reopened = FavoriteRepository(database).list_favorites(
        sort_spec=FavoriteSort(
            FavoriteSortField.FAVORITED_AT, FavoriteSortDirection.ASCENDING
        )
    )

    assert [item.id for item in reopened] == [item.id for item in created]
    assert reopened[0].post == _post(1)
    assert [item.tag_category for item in reopened[1:5]] == list(
        FavoriteTagCategory
    )
    assert reopened[5].pool_category is FavoritePoolCategory.SERIES


def test_duplicate_add_is_strictly_idempotent(repository):
    first_folder = repository.create_folder("First", created_at=BASE_TIME)
    second_folder = repository.create_folder(
        "Second", created_at=BASE_TIME + timedelta(seconds=1)
    )
    original = FavoriteCandidate.for_post(_post(2), display_name="原概要")
    changed = FavoriteCandidate.for_post(
        replace(_post(2), score=999, fav_count=888), display_name="新概要"
    )
    first = repository.add_favorite(
        original, folder_id=first_folder.id, favorited_at=BASE_TIME
    )

    duplicate = repository.add_favorite(
        changed,
        folder_id=second_folder.id,
        favorited_at=BASE_TIME + timedelta(days=1),
    )

    assert duplicate == first
    assert duplicate.display_name == "原概要"
    assert duplicate.post == _post(2)
    assert duplicate.folder_id == first_folder.id
    assert duplicate.favorited_at == BASE_TIME
    assert len(repository.list_favorites()) == 1


def test_refresh_post_metadata_preserves_identity_folder_and_first_time(repository):
    folder = repository.create_folder("Posts")
    first = repository.add_favorite(
        FavoriteCandidate.for_post(_post(3), display_name="旧名称"),
        folder_id=folder.id,
        favorited_at=BASE_TIME,
    )
    changed_post = replace(
        _post(3),
        score=50,
        fav_count=70,
        general_tags=("red_sky",),
    )

    refreshed = repository.refresh_metadata(
        FavoriteCandidate.for_post(changed_post, display_name="新名称"),
        updated_at=BASE_TIME + timedelta(hours=1),
    )

    assert refreshed.id == first.id
    assert refreshed.identity == first.identity
    assert refreshed.folder_id == folder.id
    assert refreshed.favorited_at == BASE_TIME
    assert refreshed.updated_at == BASE_TIME + timedelta(hours=1)
    assert refreshed.display_name == "新名称"
    assert refreshed.post == changed_post
    assert [item.id for item in repository.list_favorites(
        FavoriteFilter(exact_tag="red_sky")
    )] == [first.id]
    assert repository.list_favorites(FavoriteFilter(exact_tag="blue_sky")) == ()


def test_refresh_tag_category_reuses_same_identity(repository):
    first = repository.add_favorite(
        FavoriteCandidate.for_tag(
            "SAMPLE_TAG", FavoriteTagCategory.GENERAL, display_name="普通"
        )
    )

    duplicate = repository.add_favorite(
        FavoriteCandidate.for_tag(
            "sample_tag", FavoriteTagCategory.ARTIST, display_name="画师"
        )
    )
    refreshed = repository.refresh_metadata(
        FavoriteCandidate.for_tag(
            "sample_tag", FavoriteTagCategory.ARTIST, display_name="画师"
        ),
        updated_at=BASE_TIME + timedelta(seconds=1),
    )

    assert duplicate == first
    assert refreshed.id == first.id
    assert refreshed.tag_category is FavoriteTagCategory.ARTIST
    assert refreshed.display_name == "画师"
    assert refreshed.favorited_at == first.favorited_at
    assert len(repository.list_favorites()) == 1


def test_two_repositories_concurrently_add_one_identity(database):
    repositories = (
        FavoriteRepository(database, clock=lambda: BASE_TIME),
        FavoriteRepository(database, clock=lambda: BASE_TIME),
    )
    barrier = threading.Barrier(2)
    results = []
    failures = []

    def add(repository):
        try:
            barrier.wait(timeout=5)
            results.append(
                repository.add_favorite(FavoriteCandidate.for_post(_post(4)))
            )
        except Exception as exc:  # pragma: no cover - failure is asserted below
            failures.append(exc)

    threads = [threading.Thread(target=add, args=(value,)) for value in repositories]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not failures
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert results[0].id == results[1].id
    assert len(repositories[0].list_favorites()) == 1


def test_folder_names_are_unicode_case_insensitive_and_can_be_renamed(repository):
    folder = repository.create_folder("  Café  ", created_at=BASE_TIME)

    with pytest.raises(FavoriteError) as duplicate:
        repository.create_folder("CAFE\u0301")
    renamed = repository.rename_folder(
        folder.id, "画师", updated_at=BASE_TIME + timedelta(seconds=1)
    )

    assert duplicate.value.code == "duplicate_folder"
    assert renamed.name == "画师"
    assert renamed.created_at == BASE_TIME
    assert renamed.updated_at == BASE_TIME + timedelta(seconds=1)
    assert repository.list_folders() == (renamed,)


@pytest.mark.parametrize("name", ("", "   ", "bad\x00name", None))
def test_invalid_folder_name_does_not_create_partial_row(repository, name):
    with pytest.raises(FavoriteError):
        repository.create_folder(name)

    assert repository.list_folders() == ()


def test_delete_folder_can_move_items_to_uncategorized(repository):
    folder = repository.create_folder("Folder")
    item = repository.add_favorite(
        FavoriteCandidate.for_pool(7, "Pool"), folder_id=folder.id
    )

    affected = repository.delete_folder(
        folder.id, FolderDeleteMode.MOVE_TO_UNCATEGORIZED
    )

    assert affected == 1
    assert repository.list_folders() == ()
    assert repository.get_favorite(item.id).folder_id is None
    assert [value.id for value in repository.list_favorites(
        FavoriteFilter(folder_scope=FavoriteFolderScope.UNCATEGORIZED)
    )] == [item.id]


def test_delete_folder_can_delete_contained_favorites(repository):
    folder = repository.create_folder("Folder")
    inside = repository.add_favorite(
        FavoriteCandidate.for_tag("inside", FavoriteTagCategory.GENERAL),
        folder_id=folder.id,
    )
    outside = repository.add_favorite(
        FavoriteCandidate.for_tag("outside", FavoriteTagCategory.GENERAL)
    )

    assert repository.delete_folder(
        folder.id, FolderDeleteMode.DELETE_FAVORITES
    ) == 1

    with pytest.raises(FavoriteError, match="找不到"):
        repository.get_favorite(inside.id)
    assert [item.id for item in repository.list_favorites()] == [outside.id]


def test_batch_move_unknown_favorite_or_folder_rolls_back(repository):
    source = repository.create_folder("Source")
    target = repository.create_folder("Target")
    first = repository.add_favorite(
        FavoriteCandidate.for_pool(10, "Ten"), folder_id=source.id
    )
    second = repository.add_favorite(
        FavoriteCandidate.for_pool(11, "Eleven"), folder_id=source.id
    )

    with pytest.raises(FavoriteError) as unknown_item:
        repository.move_favorites((first.id, 9999), target.id)
    with pytest.raises(FavoriteError) as unknown_folder:
        repository.move_favorites((first.id, second.id), 9999)

    assert unknown_item.value.code == "unknown_favorite"
    assert unknown_folder.value.code == "unknown_folder"
    assert repository.get_favorite(first.id).folder_id == source.id
    assert repository.get_favorite(second.id).folder_id == source.id


def test_batch_move_and_delete_have_explicit_empty_and_atomic_rules(repository):
    folder = repository.create_folder("Folder")
    first = repository.add_favorite(
        FavoriteCandidate.for_pool(20, "Twenty"), folder_id=folder.id
    )
    second = repository.add_favorite(
        FavoriteCandidate.for_pool(21, "Twenty One"), folder_id=folder.id
    )

    assert repository.delete_favorites(()) == 0
    with pytest.raises(FavoriteError) as empty_move:
        repository.move_favorites((), None)
    with pytest.raises(FavoriteError) as unknown_delete:
        repository.delete_favorites((first.id, 9999))
    assert empty_move.value.code == "empty_batch"
    assert unknown_delete.value.code == "unknown_favorite"
    assert {item.id for item in repository.list_favorites()} == {first.id, second.id}

    assert repository.move_favorites((first.id, second.id), None) == 2
    assert repository.delete_favorites((first.id, second.id)) == 2
    assert repository.list_favorites() == ()


def test_list_filters_by_folder_object_type_and_tag_category(repository):
    folder = repository.create_folder("Folder")
    post = repository.add_favorite(
        FavoriteCandidate.for_post(_post(30)), folder_id=folder.id
    )
    artist = repository.add_favorite(
        FavoriteCandidate.for_tag("artist_only", FavoriteTagCategory.ARTIST),
        folder_id=folder.id,
    )
    repository.add_favorite(
        FavoriteCandidate.for_tag("general_only", FavoriteTagCategory.GENERAL)
    )

    in_folder = repository.list_favorites(
        FavoriteFilter(
            folder_scope=FavoriteFolderScope.FOLDER, folder_id=folder.id
        )
    )
    artists = repository.list_favorites(
        FavoriteFilter(
            object_type=FavoriteObjectType.TAG,
            tag_category=FavoriteTagCategory.ARTIST,
        )
    )

    assert {item.id for item in in_folder} == {post.id, artist.id}
    assert [item.id for item in artists] == [artist.id]


def test_exact_post_tag_artist_and_rating_filters_do_not_use_partial_matches(
    repository,
):
    matching = repository.add_favorite(
        FavoriteCandidate.for_post(
            _post(40, rating="e", artist="exact_artist", general="blue_sky")
        )
    )
    repository.add_favorite(
        FavoriteCandidate.for_post(
            _post(41, rating="g", artist="exact_artist_2", general="blue_sky_day")
        )
    )

    assert [item.id for item in repository.list_favorites(
        FavoriteFilter(exact_tag="BLUE_SKY")
    )] == [matching.id]
    assert [item.id for item in repository.list_favorites(
        FavoriteFilter(exact_artist="EXACT_ARTIST")
    )] == [matching.id]
    assert [item.id for item in repository.list_favorites(
        FavoriteFilter(rating="e")
    )] == [matching.id]
    assert repository.list_favorites(FavoriteFilter(exact_tag="blue")) == ()
    assert repository.list_favorites(FavoriteFilter(exact_artist="artist")) == ()


@pytest.mark.parametrize(
    ("field", "direction", "expected"),
    (
        (FavoriteSortField.SCORE, FavoriteSortDirection.ASCENDING, (51, 50, 52, 53)),
        (FavoriteSortField.SCORE, FavoriteSortDirection.DESCENDING, (50, 52, 51, 53)),
        (FavoriteSortField.FAV_COUNT, FavoriteSortDirection.ASCENDING, (50, 51, 52, 53)),
        (FavoriteSortField.FAV_COUNT, FavoriteSortDirection.DESCENDING, (52, 50, 51, 53)),
    ),
)
def test_numeric_sort_keeps_missing_values_last_and_id_breaks_ties(
    repository, field, direction, expected
):
    values = (
        _post(50, score=0, fav_count=0),
        _post(51, score=-2, fav_count=0),
        _post(52, score=0, fav_count=9),
        _post(53, score=None, fav_count=None),
    )
    items = {
        post.id: repository.add_favorite(FavoriteCandidate.for_post(post))
        for post in values
    }

    sorted_items = repository.list_favorites(
        sort_spec=FavoriteSort(field, direction)
    )

    assert [item.id for item in sorted_items] == [items[key].id for key in expected]


def test_favorited_time_sort_is_stable_and_paginated(repository):
    items = []
    for post_id, moment in (
        (60, BASE_TIME),
        (61, BASE_TIME),
        (62, BASE_TIME + timedelta(seconds=1)),
    ):
        items.append(
            repository.add_favorite(
                FavoriteCandidate.for_post(_post(post_id)), favorited_at=moment
            )
        )

    ascending = repository.list_favorites(
        sort_spec=FavoriteSort(
            FavoriteSortField.FAVORITED_AT, FavoriteSortDirection.ASCENDING
        )
    )
    descending = repository.list_favorites(
        sort_spec=FavoriteSort(
            FavoriteSortField.FAVORITED_AT, FavoriteSortDirection.DESCENDING
        )
    )
    second = repository.list_favorites(
        sort_spec=FavoriteSort(
            FavoriteSortField.FAVORITED_AT, FavoriteSortDirection.ASCENDING
        ),
        limit=1,
        offset=1,
    )

    assert [item.id for item in ascending] == [item.id for item in items]
    assert [item.id for item in descending] == [
        items[2].id,
        items[0].id,
        items[1].id,
    ]
    assert second == (items[1],)


@pytest.mark.parametrize(
    "call",
    (
        lambda: FavoriteCandidate.for_pool(True, "Pool"),
        lambda: FavoriteCandidate.for_pool(0, "Pool"),
        lambda: FavoriteCandidate.for_pool(2**63, "Pool"),
        lambda: FavoriteCandidate.for_post(replace(_post(1), score=2**63)),
        lambda: FavoriteCandidate.for_tag("bad tag", FavoriteTagCategory.GENERAL),
        lambda: FavoriteIdentity(FavoriteObjectType.POST, "01"),
        lambda: FavoriteIdentity("post", "1"),
        lambda: FavoriteCandidate(
            FavoriteIdentity(FavoriteObjectType.TAG, "tag"), "Tag"
        ),
    ),
)
def test_invalid_identity_or_category_is_rejected(call):
    with pytest.raises(FavoriteError):
        call()


def test_unknown_folder_and_invalid_batches_leave_database_unchanged(repository):
    with pytest.raises(FavoriteError) as unknown_folder:
        repository.add_favorite(FavoriteCandidate.for_pool(80, "Pool"), folder_id=999)
    with pytest.raises(FavoriteError):
        repository.move_favorites((True,), None)
    with pytest.raises(FavoriteError):
        repository.delete_favorites((1, 1))

    assert unknown_folder.value.code == "unknown_folder"
    assert repository.list_favorites() == ()


@pytest.mark.parametrize(
    ("column", "value", "code"),
    (
        ("summary_json", "{}", "invalid_summary"),
        ("score", 777, "summary_mismatch"),
        ("favorited_at", "bad-time", "corrupt_timestamp"),
    ),
)
def test_corrupt_favorite_row_returns_stable_error(
    database, repository, column, value, code
):
    item = repository.add_favorite(FavoriteCandidate.for_post(_post(90)))
    with database.connect() as connection:
        with connection:
            connection.execute(
                f"UPDATE favorite_items SET {column} = ? WHERE id = ?",
                (value, item.id),
            )

    with pytest.raises(FavoriteError) as error:
        repository.get_favorite(item.id)

    assert error.value.code == code


def test_corrupt_post_tag_index_returns_stable_error(database, repository):
    item = repository.add_favorite(FavoriteCandidate.for_post(_post(91)))
    with database.connect() as connection:
        with connection:
            connection.execute(
                """DELETE FROM favorite_post_tags
                   WHERE favorite_id = ? AND tag_category = 'artist'""",
                (item.id,),
            )

    with pytest.raises(FavoriteError) as error:
        repository.get_favorite(item.id)

    assert error.value.code == "summary_mismatch"


def test_failed_refresh_of_corrupt_record_does_not_repair_it(database, repository):
    item = repository.add_favorite(FavoriteCandidate.for_post(_post(92)))
    with database.connect() as connection:
        with connection:
            connection.execute(
                "UPDATE favorite_items SET summary_json = '{}' WHERE id = ?",
                (item.id,),
            )

    with pytest.raises(FavoriteError):
        repository.refresh_metadata(
            FavoriteCandidate.for_post(replace(_post(92), score=100))
        )
    with database.connect() as connection:
        encoded = connection.execute(
            "SELECT summary_json FROM favorite_items WHERE id = ?", (item.id,)
        ).fetchone()["summary_json"]

    assert encoded == "{}"


def test_delete_one_is_idempotent_and_cascades_post_tags(database, repository):
    item = repository.add_favorite(FavoriteCandidate.for_post(_post(93)))

    assert repository.delete_favorite(item.id) is True
    assert repository.delete_favorite(item.id) is False
    with database.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM favorite_post_tags WHERE favorite_id = ?",
            (item.id,),
        ).fetchone()["count"]

    assert count == 0


def test_refresh_unknown_favorite_is_stable_error(repository):
    with pytest.raises(FavoriteError) as error:
        repository.refresh_metadata(FavoriteCandidate.for_pool(100, "Unknown"))

    assert error.value.code == "unknown_favorite"


def test_duplicate_add_ignores_unknown_requested_folder(repository):
    original = repository.add_favorite(FavoriteCandidate.for_pool(101, "Pool"))

    duplicate = repository.add_favorite(
        FavoriteCandidate.for_pool(101, "Changed"), folder_id=9999
    )

    assert duplicate == original


def test_one_read_transaction_keeps_post_and_tag_index_on_one_snapshot(
    database, monkeypatch
):
    reader = FavoriteRepository(database, clock=lambda: BASE_TIME)
    writer = FavoriteRepository(database, clock=lambda: BASE_TIME)
    original_post = _post(110, general="old_tag")
    item = reader.add_favorite(FavoriteCandidate.for_post(original_post))
    refreshed_post = replace(original_post, general_tags=("new_tag",), score=99)
    reader_reached_tags = threading.Event()
    writer_finished = threading.Event()
    writer_failures = []
    reader_thread = threading.current_thread()
    original_validator = favorites_module._validate_post_tag_rows

    def pause_reader(connection, favorite_id, post):
        if threading.current_thread() is reader_thread:
            reader_reached_tags.set()
            assert writer_finished.wait(timeout=5)
        return original_validator(connection, favorite_id, post)

    monkeypatch.setattr(favorites_module, "_validate_post_tag_rows", pause_reader)

    def refresh():
        try:
            assert reader_reached_tags.wait(timeout=5)
            writer.refresh_metadata(FavoriteCandidate.for_post(refreshed_post))
        except Exception as exc:  # pragma: no cover - asserted below
            writer_failures.append(exc)
        finally:
            writer_finished.set()

    thread = threading.Thread(target=refresh)
    thread.start()
    observed = reader.get_favorite(item.id)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert not writer_failures
    assert observed.post == original_post
    assert reader.get_favorite(item.id).post == refreshed_post


@pytest.mark.parametrize(
    ("mode", "damage_sql"),
    (
        (
            FolderDeleteMode.MOVE_TO_UNCATEGORIZED,
            "UPDATE favorite_items SET summary_json = '{}' WHERE id = ?",
        ),
        (
            FolderDeleteMode.DELETE_FAVORITES,
            "DELETE FROM favorite_post_tags WHERE favorite_id = ?",
        ),
    ),
)
def test_delete_folder_validates_all_favorites_before_either_strategy(
    database, repository, mode, damage_sql
):
    folder = repository.create_folder("Damaged")
    item = repository.add_favorite(
        FavoriteCandidate.for_post(_post(111)), folder_id=folder.id
    )
    with database.connect() as connection:
        with connection:
            connection.execute(damage_sql, (item.id,))

    with pytest.raises(FavoriteError):
        repository.delete_folder(folder.id, mode)

    with database.connect() as connection:
        folder_count = connection.execute(
            "SELECT COUNT(*) FROM favorite_folders WHERE id = ?", (folder.id,)
        ).fetchone()[0]
        stored_folder_id = connection.execute(
            "SELECT folder_id FROM favorite_items WHERE id = ?", (item.id,)
        ).fetchone()[0]
    assert folder_count == 1
    assert stored_folder_id == folder.id


@pytest.mark.parametrize("invalid", ("1", True))
def test_public_numeric_identifiers_reject_text_and_booleans(repository, invalid):
    folder = repository.create_folder("Valid")
    item = repository.add_favorite(FavoriteCandidate.for_pool(112, "Valid"))

    with pytest.raises(FavoriteError):
        FavoriteCandidate.for_pool(invalid, "Pool")
    with pytest.raises(FavoriteError):
        FavoriteCandidate.for_post(replace(_post(113), id=invalid))
    with pytest.raises(FavoriteError):
        repository.get_folder(invalid)
    with pytest.raises(FavoriteError):
        repository.rename_folder(invalid, "Other")
    with pytest.raises(FavoriteError):
        repository.delete_folder(invalid, FolderDeleteMode.DELETE_FAVORITES)
    with pytest.raises(FavoriteError):
        repository.get_favorite(invalid)
    with pytest.raises(FavoriteError):
        repository.delete_favorite(invalid)
    with pytest.raises(FavoriteError):
        repository.add_favorite(
            FavoriteCandidate.for_pool(114, "New"), folder_id=invalid
        )
    with pytest.raises(FavoriteError):
        repository.move_favorites((item.id,), invalid)
    with pytest.raises(FavoriteError):
        repository.move_favorites((invalid,), folder.id)
    with pytest.raises(FavoriteError):
        repository.delete_favorites((invalid,))
    with pytest.raises(FavoriteError):
        FavoriteFilter(
            folder_scope=FavoriteFolderScope.FOLDER,
            folder_id=invalid,
        )


@pytest.mark.parametrize(
    "keyword",
    (
        {"limit": 2**63},
        {"offset": 2**63},
    ),
)
def test_list_pagination_rejects_values_outside_sqlite_integer_range(
    repository, keyword
):
    with pytest.raises(FavoriteError) as error:
        repository.list_favorites(**keyword)

    assert error.value.code == "invalid_pagination"


def test_collection_revision_changes_once_per_actual_repository_mutation(repository):
    assert repository.capture_collection().revision == 0
    folder = repository.create_folder("One")
    assert repository.capture_collection().revision == 1
    repository.rename_folder(folder.id, "One")
    assert repository.capture_collection().revision == 1
    folder = repository.rename_folder(folder.id, "Renamed")
    assert repository.capture_collection().revision == 2

    candidate = FavoriteCandidate.for_post(_post(120))
    item = repository.add_favorite(candidate, folder_id=folder.id)
    assert repository.capture_collection().revision == 3
    repository.add_favorite(candidate)
    repository.refresh_metadata(candidate)
    repository.move_favorites((item.id,), folder.id)
    assert repository.capture_collection().revision == 3

    changed = FavoriteCandidate.for_post(replace(_post(120), score=50))
    repository.refresh_metadata(changed)
    assert repository.capture_collection().revision == 4
    target = repository.create_folder("Target")
    assert repository.capture_collection().revision == 5
    assert repository.move_favorites((item.id,), target.id) == 1
    assert repository.capture_collection().revision == 6
    assert repository.delete_favorites(()) == 0
    assert repository.delete_favorite(9999) is False
    assert repository.capture_collection().revision == 6
    assert repository.delete_favorites((item.id,)) == 1
    assert repository.capture_collection().revision == 7
    repository.delete_folder(target.id, FolderDeleteMode.DELETE_FAVORITES)
    assert repository.capture_collection().revision == 8
