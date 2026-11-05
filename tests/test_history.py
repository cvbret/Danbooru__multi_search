from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from danbooru_multi_search.danbooru import (
    GalleryQueryDefinition,
    GalleryResultSummary,
    QueryExecutionErrorCode,
    QueryExecutionLimits,
    QueryExecutionState,
    QueryMode,
    QueryRating,
    ResultFilterSpec,
    ResultSortField,
    ResultSortSpec,
    parse_query_expression,
)
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


def _definition(
    source: str | None,
    *,
    mode: QueryMode = QueryMode.STANDARD,
    filters: ResultFilterSpec | None = None,
    sorting: ResultSortSpec | None = None,
) -> GalleryQueryDefinition:
    expression = (
        parse_query_expression(source).normalized_text
        if source is not None
        else None
    )
    return GalleryQueryDefinition(
        mode,
        expression,
        QueryRating.ALL,
        filters or ResultFilterSpec(),
        sorting or ResultSortSpec(),
        QueryExecutionLimits(page_size=40),
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


def test_complex_definition_identity_and_all_expression_tags_are_persisted(tmp_path):
    repository, _database = _repository(tmp_path)
    definitions = (
        _definition("cat AND dog"),
        _definition("cat OR dog"),
        _definition("cat AND NOT dog"),
    )

    queries = [
        repository.record_accepted_definition(
            definition=definition,
            display_text=definition.normalized_expression,
            page=1,
            count_tag_usage=True,
            used_at=FIXED_NOW + timedelta(seconds=index),
        )
        for index, definition in enumerate(definitions)
    ]

    assert len({query.id for query in queries}) == 3
    assert all(query.normalized_tags == ("cat", "dog") for query in queries)
    assert [query.definition for query in queries] == list(definitions)
    assert {
        tag.normalized_tag: tag.use_count for tag in repository.list_tags()
    } == {"cat": 3, "dog": 3}


def test_tagless_rank_and_latest_have_distinct_history_without_recent_tags(tmp_path):
    repository, _database = _repository(tmp_path)
    rank = _definition(
        None,
        mode=QueryMode.RANK,
        sorting=ResultSortSpec(ResultSortField.RANK),
    )
    latest = _definition(None, mode=QueryMode.LATEST)

    rank_query = repository.record_accepted_definition(
        definition=rank,
        display_text="近期热门",
        page=1,
        count_tag_usage=True,
    )
    latest_query = repository.record_accepted_definition(
        definition=latest,
        display_text="最新作品",
        page=1,
        count_tag_usage=True,
    )

    assert rank_query.id != latest_query.id
    assert rank_query.normalized_tags == ()
    assert latest_query.normalized_tags == ()
    assert repository.list_tags() == ()


def test_view_definition_creates_and_reuses_identity_without_tag_increment(tmp_path):
    repository, _database = _repository(tmp_path)
    source = repository.record_accepted_definition(
        definition=_definition("cat"),
        display_text="cat",
        page=3,
        count_tag_usage=True,
    )
    assert repository.mark_query_committed(source.id, 2)
    assert repository.update_gallery_position(
        GalleryPosition(source.id, 90, 4, -8.0, 240.0)
    )
    target_definition = _definition(
        "cat",
        filters=ResultFilterSpec(minimum_score=10),
    )

    first_target = repository.record_view_definition(
        source_query_id=source.id,
        definition=target_definition,
        used_at=FIXED_NOW + timedelta(minutes=1),
    )
    repeated_target = repository.record_view_definition(
        source_query_id=source.id,
        definition=target_definition,
        used_at=FIXED_NOW + timedelta(minutes=2),
    )

    assert first_target.id != source.id
    assert repeated_target.id == first_target.id
    assert repeated_target.definition == target_definition
    assert repeated_target.last_submitted_page == 3
    assert repeated_target.last_committed_page == 2
    assert repeated_target.anchor_post_id == 90
    assert repository.get_query(source.id).definition == source.definition
    assert repository.list_tags()[0].use_count == 1


def test_result_summary_update_preserves_query_identity_and_history_order(tmp_path):
    repository, database = _repository(tmp_path)
    query = repository.record_accepted_definition(
        definition=_definition("cat"),
        display_text="cat",
        page=1,
        count_tag_usage=False,
    )
    summary = GalleryResultSummary(
        state=QueryExecutionState.PARTIAL_FAILED,
        remote_rows=20,
        unique_candidates=18,
        local_matches=12,
        visible_count=8,
        limit_reason=None,
        failure_error_code=QueryExecutionErrorCode.HTTP,
    )
    with database.connect() as connection:
        before = connection.execute(
            "SELECT query_key, definition_json, last_used_at FROM recent_queries WHERE id = ?",
            (query.id,),
        ).fetchone()

    assert repository.update_result_summary(query.id, summary) is True
    assert repository.update_result_summary(999, summary) is False
    updated = repository.get_query(query.id)
    with database.connect() as connection:
        after = connection.execute(
            "SELECT query_key, definition_json, last_used_at FROM recent_queries WHERE id = ?",
            (query.id,),
        ).fetchone()

    assert updated.definition == query.definition
    assert updated.result_summary == summary
    assert tuple(after) == tuple(before)


def test_summary_validation_and_update_block_a_second_repository_writer(tmp_path):
    repository, database = _repository(tmp_path)
    definition = _definition("cat")
    query = repository.record_accepted_definition(
        definition=definition,
        display_text="cat",
        page=1,
        count_tag_usage=False,
    )
    selected = threading.Event()
    competing_finished = threading.Event()

    class BlockingConnection:
        def __init__(self, connection):
            self._connection = connection

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, *arguments):
            return self._connection.__exit__(*arguments)

        def close(self):
            self._connection.close()

        def execute(self, statement, parameters=()):
            cursor = self._connection.execute(statement, parameters)
            if "SELECT * FROM recent_queries WHERE id" in statement:
                selected.set()
                if not competing_finished.wait(3):
                    raise AssertionError("The competing repository did not finish")
            return cursor

    class BlockingDatabase(Database):
        def connect(self):
            return BlockingConnection(super().connect())

    class NoWaitDatabase(Database):
        def connect(self):
            connection = sqlite3.connect(self.path, timeout=0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 0")
            return connection

    updating_repository = HistoryRepository(BlockingDatabase(database.path))
    competing_repository = HistoryRepository(NoWaitDatabase(database.path))
    summary = GalleryResultSummary(
        QueryExecutionState.COMPLETE,
        1,
        1,
        1,
        1,
        None,
        None,
    )
    update_results: list[bool] = []
    update_errors: list[BaseException] = []

    def update_summary() -> None:
        try:
            update_results.append(
                updating_repository.update_result_summary(query.id, summary)
            )
        except BaseException as exc:
            update_errors.append(exc)

    thread = threading.Thread(target=update_summary)
    thread.start()
    assert selected.wait(3)
    try:
        with pytest.raises(HistoryError, match="locked"):
            competing_repository.record_accepted_definition(
                definition=definition,
                display_text="competing cat",
                page=2,
                count_tag_usage=False,
            )
    finally:
        competing_finished.set()
    thread.join(3)

    assert not thread.is_alive()
    assert update_errors == []
    assert update_results == [True]
    stored = repository.get_query(query.id)
    assert stored.display_text == "cat"
    assert stored.last_submitted_page == 1
    assert stored.result_summary == summary
    assert len(repository.list_queries()) == 1
    assert competing_repository.update_result_summary(query.id, summary) is True


def test_newer_acceptance_clears_old_summary_but_delayed_acceptance_keeps_it(tmp_path):
    repository, _database = _repository(tmp_path)
    definition = _definition("cat")
    query = repository.record_accepted_definition(
        definition=definition,
        display_text="cat",
        page=1,
        count_tag_usage=False,
        used_at=FIXED_NOW,
    )
    summary = GalleryResultSummary(
        QueryExecutionState.COMPLETE,
        1,
        1,
        1,
        1,
        None,
        None,
    )
    assert repository.update_result_summary(query.id, summary)

    delayed = repository.record_accepted_definition(
        definition=definition,
        display_text="old cat",
        page=2,
        count_tag_usage=False,
        used_at=FIXED_NOW - timedelta(minutes=1),
    )
    assert delayed.result_summary == summary
    newer = repository.record_accepted_definition(
        definition=definition,
        display_text="new cat",
        page=3,
        count_tag_usage=False,
        used_at=FIXED_NOW + timedelta(minutes=1),
    )
    assert newer.result_summary is None


@pytest.mark.parametrize(
    "corruption",
    [
        "definition_version",
        "query_mode",
        "query_key",
        "definition_json",
        "normalized_tags",
        "rating",
        "page_size",
        "result_summary",
    ],
)
def test_corrupted_definition_redundancy_or_summary_returns_history_error(
    tmp_path,
    corruption,
):
    repository, database = _repository(tmp_path)
    query = repository.record_accepted_definition(
        definition=_definition("cat AND NOT dog"),
        display_text="cat -dog",
        page=1,
        count_tag_usage=False,
    )
    with database.connect() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        with connection:
            if corruption == "definition_version":
                connection.execute(
                    "UPDATE recent_queries SET definition_version = 2 WHERE id = ?",
                    (query.id,),
                )
            elif corruption == "query_mode":
                connection.execute(
                    "UPDATE recent_queries SET query_mode = 'latest' WHERE id = ?",
                    (query.id,),
                )
            elif corruption == "query_key":
                connection.execute(
                    "UPDATE recent_queries SET query_key = '{}' WHERE id = ?",
                    (query.id,),
                )
            elif corruption == "definition_json":
                damaged = '{"unknown":NaN}'
                connection.execute(
                    """
                    UPDATE recent_queries
                    SET query_key = ?, definition_json = ?
                    WHERE id = ?
                    """,
                    (damaged, damaged, query.id),
                )
            elif corruption == "normalized_tags":
                connection.execute(
                    "UPDATE recent_queries SET normalized_tags = '[\"cat\"]' WHERE id = ?",
                    (query.id,),
                )
            elif corruption == "rating":
                connection.execute(
                    "UPDATE recent_queries SET rating = 'general' WHERE id = ?",
                    (query.id,),
                )
            elif corruption == "page_size":
                connection.execute(
                    "UPDATE recent_queries SET page_size = 41 WHERE id = ?",
                    (query.id,),
                )
            else:
                connection.execute(
                    """
                    UPDATE recent_queries
                    SET result_summary_json = '{"version":1,"version":1}'
                    WHERE id = ?
                    """,
                    (query.id,),
                )

    with pytest.raises(HistoryError):
        repository.get_query(query.id)


@pytest.mark.parametrize("corruption", ["rating", "query_key", "result_summary"])
def test_accepted_definition_rolls_back_when_existing_row_is_corrupt(
    tmp_path,
    corruption,
):
    repository, database = _repository(tmp_path)
    definition = _definition("cat")
    query = repository.record_accepted_definition(
        definition=definition,
        display_text="cat",
        page=1,
        count_tag_usage=True,
    )
    summary = GalleryResultSummary(
        QueryExecutionState.COMPLETE,
        1,
        1,
        1,
        1,
        None,
        None,
    )
    assert repository.update_result_summary(query.id, summary)
    with database.connect() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        with connection:
            if corruption == "rating":
                connection.execute(
                    "UPDATE recent_queries SET rating = 'general' WHERE id = ?",
                    (query.id,),
                )
            elif corruption == "query_key":
                connection.execute(
                    "UPDATE recent_queries SET query_key = '{}' WHERE id = ?",
                    (query.id,),
                )
            else:
                connection.execute(
                    """
                    UPDATE recent_queries
                    SET result_summary_json = '{"version":1,"version":1}'
                    WHERE id = ?
                    """,
                    (query.id,),
                )
        before_queries = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM recent_queries ORDER BY id"
            ).fetchall()
        )
        before_tags = tuple(
            connection.execute(
                "SELECT * FROM recent_tags ORDER BY normalized_tag"
            ).fetchall()[0]
        )

    with pytest.raises(HistoryError):
        repository.record_accepted_definition(
            definition=definition,
            display_text="new cat",
            page=3,
            count_tag_usage=True,
            used_at=FIXED_NOW + timedelta(minutes=1),
        )

    with database.connect() as connection:
        after_queries = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM recent_queries ORDER BY id"
            ).fetchall()
        )
        after_tags = tuple(
            connection.execute(
                "SELECT * FROM recent_tags ORDER BY normalized_tag"
            ).fetchall()[0]
        )
    assert after_queries == before_queries
    assert after_tags == before_tags


@pytest.mark.parametrize("corruption", ["rating", "query_key", "result_summary"])
def test_view_definition_rolls_back_when_target_row_is_corrupt(
    tmp_path,
    corruption,
):
    repository, database = _repository(tmp_path)
    source = repository.record_accepted_definition(
        definition=_definition("cat"),
        display_text="cat",
        page=1,
        count_tag_usage=False,
    )
    target_definition = _definition(
        "cat",
        filters=ResultFilterSpec(minimum_score=1),
    )
    target = repository.record_view_definition(
        source_query_id=source.id,
        definition=target_definition,
    )
    summary = GalleryResultSummary(
        QueryExecutionState.COMPLETE,
        1,
        1,
        1,
        1,
        None,
        None,
    )
    assert repository.update_result_summary(target.id, summary)
    with database.connect() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        with connection:
            if corruption == "rating":
                connection.execute(
                    "UPDATE recent_queries SET rating = 'general' WHERE id = ?",
                    (target.id,),
                )
            elif corruption == "query_key":
                connection.execute(
                    "UPDATE recent_queries SET query_key = '{}' WHERE id = ?",
                    (target.id,),
                )
            else:
                connection.execute(
                    """
                    UPDATE recent_queries
                    SET result_summary_json = '{"version":1,"version":1}'
                    WHERE id = ?
                    """,
                    (target.id,),
                )
        before = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM recent_queries ORDER BY id"
            ).fetchall()
        )

    with pytest.raises(HistoryError):
        repository.record_view_definition(
            source_query_id=source.id,
            definition=target_definition,
            used_at=FIXED_NOW + timedelta(minutes=1),
        )

    with database.connect() as connection:
        after = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM recent_queries ORDER BY id"
            ).fetchall()
        )
    assert after == before


def test_summary_update_rolls_back_when_target_row_is_corrupt(tmp_path):
    repository, database = _repository(tmp_path)
    query = repository.record_accepted_definition(
        definition=_definition("cat"),
        display_text="cat",
        page=1,
        count_tag_usage=False,
    )
    old_summary = GalleryResultSummary(
        QueryExecutionState.WAITING,
        0,
        0,
        0,
        0,
        None,
        None,
    )
    assert repository.update_result_summary(query.id, old_summary)
    with database.connect() as connection:
        with connection:
            connection.execute(
                "UPDATE recent_queries SET rating = 'general' WHERE id = ?",
                (query.id,),
            )
        before = tuple(
            connection.execute(
                "SELECT * FROM recent_queries WHERE id = ?",
                (query.id,),
            ).fetchone()
        )

    with pytest.raises(HistoryError):
        repository.update_result_summary(
            query.id,
            GalleryResultSummary(
                QueryExecutionState.COMPLETE,
                2,
                2,
                2,
                2,
                None,
                None,
            ),
        )

    with database.connect() as connection:
        after = tuple(
            connection.execute(
                "SELECT * FROM recent_queries WHERE id = ?",
                (query.id,),
            ).fetchone()
        )
    assert after == before


def test_view_definition_write_failure_rolls_back_and_preserves_source(tmp_path):
    repository, database = _repository(tmp_path)
    source = repository.record_accepted_definition(
        definition=_definition("cat"),
        display_text="cat",
        page=1,
        count_tag_usage=False,
    )
    with database.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_view_definition
            BEFORE INSERT ON recent_queries
            BEGIN
                SELECT RAISE(ABORT, 'simulated view failure');
            END
            """
        )
        connection.commit()

    with pytest.raises(HistoryError, match="simulated view failure"):
        repository.record_view_definition(
            source_query_id=source.id,
            definition=_definition(
                "cat",
                filters=ResultFilterSpec(minimum_score=1),
            ),
        )

    assert repository.list_queries() == (source,)


def test_schema_constraints_reject_partial_anchor_state(tmp_path):
    _repository_instance, database = _repository(tmp_path)

    with database.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO recent_queries(
                query_key, definition_version, query_mode, definition_json,
                display_text, normalized_tags, rating, page_size,
                last_submitted_page, anchor_post_id, last_used_at
            ) VALUES (
                'cat-definition', 1, 'standard', 'cat-definition',
                'cat', '[\"cat\"]', 'all', 40, 1, 10, 'now'
            )
            """
        )
