from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from danbooru_multi_search.danbooru.query_checkpoint import CheckpointError
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.history import HistoryRepository
from danbooru_multi_search.storage.query_checkpoints import QueryCheckpointRepository
from danbooru_multi_search.ui.query_checkpoints import QueryCheckpointController
from tests.test_query_checkpoints import checkpoint


def build(tmp_path):
    database = Database(tmp_path / "application.sqlite3")
    database.initialize()
    history, saved = HistoryRepository(database), QueryCheckpointRepository(database)
    value = checkpoint()
    records = []
    query = history.record_accepted_definition(definition=value.definition, display_text="a AND b", page=1, count_tag_usage=True,
        transaction_hook=lambda connection, query: records.append(saved.save_in_transaction(connection, query.id, uuid4().hex, 0, value)))
    return database, history, saved, query, records[0]


def test_restart_reads_complete_checkpoint_without_requests(tmp_path):
    database, history, saved, query, record = build(tmp_path)
    assert QueryCheckpointRepository(Database(database.path)).get_for_history(query.id) == record
    assert history.get_query(query.id).definition == record.checkpoint.definition


def test_claim_is_revision_checked_and_old_owner_cannot_overwrite_new_owner(tmp_path):
    database, history, saved, query, record = build(tmp_path)
    claimed = saved.claim(record, "https://example.invalid")
    assert claimed.owner != record.owner and claimed.revision == record.revision + 1
    with pytest.raises(CheckpointError, match="改变"):
        saved.claim(record, "https://example.invalid")
    summary = QueryCheckpointController._summary(record.checkpoint)
    with pytest.raises(CheckpointError):
        history.update_result_summary(query.id, summary, transaction_hook=lambda connection, query: saved.save_in_transaction(
            connection, query.id, record.owner, 0, record.checkpoint, expected=record))
    assert saved.get_for_history(query.id) == claimed


def test_summary_and_checkpoint_failure_roll_back_together(tmp_path):
    database, history, saved, query, record = build(tmp_path)
    with database.connect() as connection:
        connection.execute("CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON query_checkpoints BEGIN SELECT RAISE(ABORT, 'failure'); END")
    with pytest.raises(Exception, match="failure"):
        history.update_result_summary(query.id, QueryCheckpointController._summary(record.checkpoint), transaction_hook=lambda connection, query: saved.save_in_transaction(
            connection, query.id, record.owner, 0, record.checkpoint, expected=record))
    assert saved.get_for_history(query.id) == record and history.get_query(query.id).result_summary is None


def test_source_change_and_abnormal_active_checkpoint_cannot_be_claimed(tmp_path):
    database, history, saved, query, record = build(tmp_path)
    with pytest.raises(CheckpointError, match="来源"):
        saved.claim(record, "https://other.invalid")
    from danbooru_multi_search.danbooru.query_types import QueryExecutionState
    active = replace(record.checkpoint, execution=replace(record.checkpoint.execution, active=True, state=QueryExecutionState.RUNNING))
    changed = []
    history.update_result_summary(query.id, QueryCheckpointController._summary(active), transaction_hook=lambda connection, query: changed.append(saved.save_in_transaction(connection, query.id, record.owner, 0, active, expected=record)))
    with pytest.raises(CheckpointError, match="异常结束"):
        saved.claim(changed[0], "https://example.invalid")


def test_history_deletion_cascades_and_old_write_cannot_recreate_checkpoint(tmp_path):
    database, history, saved, query, record = build(tmp_path)
    history.clear_history()
    assert saved.get_for_history(query.id) is None
    with saved._connection(write=True) as connection:
        with pytest.raises(CheckpointError):
            saved.save_in_transaction(connection, query.id, record.owner, 0, record.checkpoint, expected=record)
