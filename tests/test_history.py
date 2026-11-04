from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from danbooru_multi_search.storage import (
    GalleryPosition,
    HistoryError,
    HistoryRepository,
    WindowGeometry,
    canonicalize_query_tags,
)
from danbooru_multi_search.storage.database import Database


FIXED_NOW = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)


def _repository(tmp_path, *, clock=lambda: FIXED_NOW):
    database = Database(tmp_path / "application.sqlite3")
    database.initialize()
    return HistoryRepository(database, clock=clock), database


def _record(
    repository: HistoryRepository,
    tag: str,
    *,
    display_text: str | None = None,
    page: int = 1,
    count_tag_usage: bool = True,
    used_at: datetime | None = None,
):
    return repository.record_accepted_query(
        display_text=display_text or tag,
        normalized_tags=(tag,),
        rating="all",
        page_size=40,
        page=page,
        count_tag_usage=count_tag_usage,
        used_at=used_at,
    )


def test_query_identity_deduplicates_and_sorts_tags_while_preserving_display():
    assert canonicalize_query_tags(("Dog", "cat", "dog")) == ("cat", "dog")


def test_repeated_query_updates_submission_without_losing_committed_state(tmp_path):
    repository, _database = _repository(tmp_path)
    first = repository.record_accepted_query(
        display_text="cat dog",
        normalized_tags=("cat", "dog"),
        rating="g",
        page_size=40,
        page=2,
        count_tag_usage=True,
    )
    assert repository.mark_query_committed(first.id, 2)
    assert repository.update_gallery_position(
        GalleryPosition(first.id, 900, 4, -12.5, 320.0)
    )

    repeated = repository.record_accepted_query(
        display_text="dog cat",
        normalized_tags=("dog", "cat", "cat"),
        rating="G",
        page_size=40,
        page=5,
        count_tag_usage=True,
    )

    assert repeated.id == first.id
    assert repeated.display_text == "dog cat"
    assert repeated.normalized_tags == ("cat", "dog")
    assert repeated.rating == "general"
    assert repeated.last_submitted_page == 5
    assert repeated.last_committed_page == 2
    assert repeated.restore_page == 2
    assert repeated.anchor_post_id == 900
    assert repeated.anchor_index == 4
    assert repeated.anchor_offset == -12.5
    assert repeated.scroll_position == 320.0
    assert [(tag.normalized_tag, tag.use_count) for tag in repository.list_tags()] == [
        ("cat", 2),
        ("dog", 2),
    ]


def test_page_refresh_and_retry_do_not_increment_recent_tag_usage(tmp_path):
    repository, _database = _repository(tmp_path)
    first = _record(repository, "cat", count_tag_usage=True)

    for page in (2, 2, 1):
        refreshed = _record(
            repository,
            "cat",
            page=page,
            count_tag_usage=False,
        )

    assert refreshed.id == first.id
    assert refreshed.last_submitted_page == 1
    assert repository.list_tags()[0].use_count == 1


def test_failed_new_query_keeps_active_gallery_query_and_position(tmp_path):
    repository, _database = _repository(tmp_path)
    visible = _record(repository, "visible")
    assert repository.mark_query_committed(visible.id, 3)
    failed = _record(repository, "failed", page=6)

    saved = repository.save_browser_state(
        geometry=WindowGeometry(-1000, 50, 1000, 700),
        is_maximized=False,
        active_query_id=visible.id,
        gallery_position=GalleryPosition(visible.id, 88, 2, -6.0, 190.0),
    )

    assert saved is True
    assert repository.load_session().active_query_id == visible.id
    assert repository.get_query(visible.id).last_committed_page == 3
    assert repository.get_query(visible.id).anchor_post_id == 88
    assert repository.get_query(failed.id).last_committed_page is None
    assert repository.get_query(failed.id).anchor_post_id is None
    assert repository.latest_query().id == failed.id
    assert repository.latest_query().restore_page == 6


def test_query_capacity_uses_stable_order_and_protects_active_gallery(tmp_path):
    repository, _database = _repository(tmp_path)
    records = [
        _record(repository, f"tag_{index:03d}", count_tag_usage=False)
        for index in range(50)
    ]
    protected = records[0]
    assert repository.mark_query_committed(protected.id, 1)

    newest = _record(repository, "tag_050", count_tag_usage=False)

    queries = repository.list_queries()
    ids = {query.id for query in queries}
    assert len(queries) == 50
    assert protected.id in ids
    assert records[1].id not in ids
    assert newest.id in ids
    assert repository.update_gallery_position(
        GalleryPosition(protected.id, 55, 0, 0.0, 0.0)
    )
    assert repository.get_query(protected.id).anchor_post_id == 55


def test_query_at_capacity_can_evict_itself_without_rolling_back_tags(tmp_path):
    repository, _database = _repository(tmp_path)
    for index in range(50):
        recorded = _record(
            repository,
            f"kept_{index:03d}",
            count_tag_usage=False,
            used_at=FIXED_NOW + timedelta(minutes=index + 1),
        )
        assert recorded is not None

    evicted = _record(
        repository,
        "late_event",
        used_at=FIXED_NOW,
    )

    assert evicted is None
    assert len(repository.list_queries()) == 50
    assert all(
        query.display_text != "late_event" for query in repository.list_queries()
    )
    late_tag = next(
        tag
        for tag in repository.list_tags()
        if tag.normalized_tag == "late_event"
    )
    assert late_tag.use_count == 1
    assert late_tag.last_used_at == FIXED_NOW.isoformat(timespec="microseconds")


def test_older_acceptance_is_retained_at_49_then_evicted_at_50(tmp_path):
    repository, _database = _repository(tmp_path)
    for index in range(49):
        assert _record(
            repository,
            f"existing_{index:03d}",
            count_tag_usage=False,
            used_at=FIXED_NOW + timedelta(minutes=index + 1),
        ) is not None

    older = _record(
        repository,
        "older_first",
        count_tag_usage=False,
        used_at=FIXED_NOW,
    )
    assert older is not None
    assert len(repository.list_queries()) == 50

    later = _record(
        repository,
        "later_event",
        count_tag_usage=False,
        used_at=FIXED_NOW + timedelta(hours=2),
    )

    assert later is not None
    assert repository.get_query(older.id) is None
    assert repository.get_query(later.id) == later
    assert len(repository.list_queries()) == 50


def test_out_of_order_acceptance_keeps_active_oldest_and_commits_tag_count(
    tmp_path,
):
    repository, _database = _repository(tmp_path)
    records = []
    for index in range(50):
        record = _record(
            repository,
            f"ordered_{index:03d}",
            count_tag_usage=False,
            used_at=FIXED_NOW + timedelta(minutes=index + 1),
        )
        assert record is not None
        records.append(record)
    assert repository.mark_query_committed(records[0].id, 1)

    newest = _record(
        repository,
        "newest_first",
        used_at=FIXED_NOW + timedelta(hours=2),
    )
    delayed = _record(
        repository,
        "delayed_second",
        used_at=FIXED_NOW,
    )

    assert newest is not None
    assert delayed is None
    retained_ids = {query.id for query in repository.list_queries()}
    assert records[0].id in retained_ids
    assert records[1].id not in retained_ids
    assert newest.id in retained_ids
    tag_counts = {
        tag.normalized_tag: tag.use_count for tag in repository.list_tags()
    }
    assert tag_counts["newest_first"] == 1
    assert tag_counts["delayed_second"] == 1


def test_recent_tag_capacity_uses_stable_eviction_order(tmp_path):
    repository, _database = _repository(tmp_path)
    for index in range(101):
        _record(repository, f"tag_{index:03d}")

    tags = repository.list_tags()

    assert len(tags) == 100
    assert "tag_000" not in {tag.normalized_tag for tag in tags}
    assert "tag_100" in {tag.normalized_tag for tag in tags}


def test_query_tag_and_capacity_update_roll_back_as_one_transaction(tmp_path):
    repository, database = _repository(tmp_path)
    with database.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_recent_tag_insert
            BEFORE INSERT ON recent_tags
            BEGIN
                SELECT RAISE(ABORT, 'simulated tag failure');
            END
            """
        )
        connection.commit()

    with pytest.raises(HistoryError, match="simulated tag failure"):
        _record(repository, "rollback")

    assert repository.list_queries() == ()
    assert repository.list_tags() == ()


def test_browser_state_position_and_geometry_roll_back_together(tmp_path):
    repository, database = _repository(tmp_path)
    query = _record(repository, "cat")
    assert repository.mark_query_committed(query.id, 1)
    original = GalleryPosition(query.id, 1, 0, 0.0, 10.0)
    assert repository.update_gallery_position(original)
    with database.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_session_update
            BEFORE UPDATE ON browser_session
            BEGIN
                SELECT RAISE(ABORT, 'simulated session failure');
            END
            """
        )
        connection.commit()

    with pytest.raises(HistoryError, match="simulated session failure"):
        repository.save_browser_state(
            geometry=WindowGeometry(20, 30, 900, 650),
            is_maximized=True,
            active_query_id=query.id,
            gallery_position=GalleryPosition(query.id, 2, 1, -3.0, 200.0),
        )

    unchanged = repository.get_query(query.id)
    assert unchanged.anchor_post_id == 1
    assert unchanged.scroll_position == 10.0


def test_clear_history_preserves_geometry_and_late_save_does_not_recreate(tmp_path):
    repository, _database = _repository(tmp_path)
    query = _record(repository, "cat")
    assert repository.mark_query_committed(query.id, 1)
    geometry = WindowGeometry(-500, -100, 1100, 720)
    assert repository.save_browser_state(
        geometry=geometry,
        is_maximized=True,
        active_query_id=query.id,
        gallery_position=GalleryPosition(query.id, 10, 0, -2.0, 80.0),
    )

    repository.clear_history()

    assert repository.list_queries() == ()
    assert repository.list_tags() == ()
    session = repository.load_session()
    assert session.geometry == geometry
    assert session.is_maximized is True
    assert session.active_query_id is None
    assert repository.update_gallery_position(
        GalleryPosition(query.id, 99, 4, 0.0, 400.0)
    ) is False
    assert repository.list_queries() == ()


def test_maximized_save_without_new_geometry_preserves_last_normal_geometry(tmp_path):
    repository, _database = _repository(tmp_path)
    geometry = WindowGeometry(-300, 40, 900, 640)
    assert repository.save_browser_state(
        geometry=geometry,
        is_maximized=False,
        active_query_id=None,
        gallery_position=None,
    )

    assert repository.save_browser_state(
        geometry=None,
        is_maximized=True,
        active_query_id=None,
        gallery_position=None,
    )

    session = repository.load_session()
    assert session.geometry == geometry
    assert session.is_maximized is True


def test_committing_new_page_clears_old_position_unless_restore_preserves_it(
    tmp_path,
):
    repository, _database = _repository(tmp_path)
    query = _record(repository, "cat", page=2)
    assert repository.mark_query_committed(query.id, 2)
    assert repository.update_gallery_position(
        GalleryPosition(query.id, 22, 3, -4.0, 240.0)
    )

    assert repository.mark_query_committed(query.id, 3)
    cleared = repository.get_query(query.id)
    assert cleared.last_committed_page == 3
    assert cleared.anchor_post_id is None
    assert cleared.scroll_position == 0

    assert repository.update_gallery_position(
        GalleryPosition(query.id, 33, 5, -6.0, 360.0)
    )
    assert repository.mark_query_committed(
        query.id,
        3,
        preserve_position=True,
    )
    preserved = repository.get_query(query.id)
    assert preserved.anchor_post_id == 33
    assert preserved.anchor_index == 5
    assert preserved.anchor_offset == -6.0
    assert preserved.scroll_position == 360.0


def test_missing_active_query_is_cleared_without_recreating_history(tmp_path):
    repository, _database = _repository(tmp_path)

    saved = repository.save_browser_state(
        geometry=WindowGeometry(0, 0, 820, 600),
        is_maximized=False,
        active_query_id=999,
        gallery_position=GalleryPosition(999, None, None, None, 0.0),
    )

    assert saved is False
    assert repository.load_session().active_query_id is None
    assert repository.list_queries() == ()


def test_schema_constraints_reject_partial_anchor_state(tmp_path):
    _repository_instance, database = _repository(tmp_path)

    with database.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO recent_queries(
                display_text, normalized_tags, rating, page_size,
                last_submitted_page, anchor_post_id, last_used_at
            ) VALUES ('cat', '[\"cat\"]', 'all', 40, 1, 10, 'now')
            """
        )
