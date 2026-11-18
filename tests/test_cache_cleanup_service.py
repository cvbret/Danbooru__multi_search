from dataclasses import FrozenInstanceError
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from danbooru_multi_search.app.cache_cleanup import CacheCleanupService
from danbooru_multi_search.app.write_access import SharedWriteAccess
from tests.test_checkpoint_repository import build


@pytest.fixture
def cleanup(tmp_path):
    database, history, checkpoints, query, record = build(tmp_path)
    access = SharedWriteAccess()
    events = []
    progress = SimpleNamespace(
        cancel_if_owned=lambda owners: events.append("cancel"),
        retire_checkpoints=lambda owners: events.append("retire"),
        reconcile_ownership=lambda: events.append("reconcile"),
    )
    service = CacheCleanupService(history, checkpoints, progress, access)
    return SimpleNamespace(
        database=database, history=history, checkpoints=checkpoints, query=query,
        record=record, access=access, events=events, progress=progress, service=service,
    )


def callbacks(value):
    def clear_history(delete, retire):
        assert value.access.busy
        value.events.append("delete")
        delete()
        retire()

    def publish(notice):
        assert value.access.busy
        assert value.access.maintenance_owner == value.access.external_owner
        value.events.append("publish")

    return dict(
        guard=lambda: True,
        clear_history=clear_history,
        refresh_history=lambda: value.events.append("refresh"),
        clear_images=lambda: "cleared",
        publish=publish,
        failed=pytest.fail,
    )


def test_history_cleanup_retires_and_publishes_before_releasing_maintenance(cleanup):
    value = cleanup
    snapshot = value.service.capture("history")
    assert snapshot.count == 1
    with pytest.raises(FrozenInstanceError):
        snapshot.count = 7
    assert value.service.execute(snapshot, **callbacks(value))
    assert value.events == ["cancel", "delete", "retire", "publish"]
    assert value.history.list_queries() == ()
    assert value.checkpoints.get_for_history(value.query.id) is None
    assert not value.access.busy and value.access.maintenance_owner is None


def test_delete_failure_reports_under_maintenance_without_retiring_confirmed_owner(cleanup):
    value = cleanup
    snapshot = value.service.capture("checkpoints")
    with value.database.connect() as connection:
        connection.execute(
            "CREATE TRIGGER refuse_delete BEFORE DELETE ON query_checkpoints "
            "BEGIN SELECT RAISE(ABORT, 'keep confirmed owner'); END"
        )
    options = callbacks(value)

    def failed(error):
        assert value.access.busy and value.access.maintenance_owner is not None
        assert value.checkpoints.get_for_history(value.query.id) == value.record
        value.events.append("failed")

    options["failed"] = failed
    assert not value.service.execute(snapshot, **options)
    assert value.events == ["cancel", "failed"]
    assert not value.access.busy


def test_replaced_owner_invalidates_snapshot_without_deleting_new_record(cleanup):
    value = cleanup
    snapshot = value.service.capture("checkpoints")
    claimed = value.checkpoints.claim(value.record, value.record.checkpoint.source)
    options = callbacks(value)

    def failed(error):
        assert value.access.busy and "归属已改变" in str(error)
        value.events.append("failed")

    options["failed"] = failed
    assert not value.service.execute(snapshot, **options)
    assert value.events == ["reconcile", "failed"]
    assert value.checkpoints.get_for_history(value.query.id) == claimed


@pytest.mark.parametrize("stage", ["reservation", "cancel"])
def test_synchronous_invalidation_prevents_the_delete_and_releases_only_own_access(cleanup, stage):
    value = cleanup
    current = True
    snapshot = value.service.capture("checkpoints")

    def invalidate():
        nonlocal current
        current = False

    if stage == "reservation":
        value.access.changed.connect(
            lambda: invalidate() if value.access.busy else None
        )
    else:
        value.progress.cancel_if_owned = lambda owners: invalidate()
    options = callbacks(value)
    options["guard"] = lambda: current
    options["publish"] = pytest.fail
    assert not value.service.execute(snapshot, **options)
    assert value.checkpoints.get_for_history(value.query.id) == value.record
    assert not value.access.busy


def test_cache_services_import_and_work_without_ui_or_query_controller(tmp_path):
    code = """
import importlib.abc
import sys
from pathlib import Path
from types import SimpleNamespace

class Reject(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("danbooru_multi_search.ui", "danbooru_multi_search.danbooru.query_controller")):
            raise AssertionError(fullname)

sys.meta_path.insert(0, Reject())
from danbooru_multi_search.app.cache_cleanup import CacheCleanupService, delete_history
from danbooru_multi_search.app.cache_protection import CacheProtectionService
from danbooru_multi_search.app.write_access import SharedWriteAccess
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.history import HistoryRepository
from danbooru_multi_search.storage.cache_protection import CacheProtectionRepository
from danbooru_multi_search.storage.query_checkpoints import QueryCheckpointRepository
database = Database(Path(sys.argv[1]) / "cold.sqlite3")
database.initialize()
access = SharedWriteAccess()
history = HistoryRepository(database)
cleanup = CacheCleanupService(history, QueryCheckpointRepository(database), SimpleNamespace(), access)
assert cleanup.capture("history").count == 0
delete_history(history)
protection = CacheProtectionService(CacheProtectionRepository(database), access)
protection.reload()
with protection.protection(lambda snapshot: frozenset()) as keys:
    assert keys == frozenset()
protection.shutdown()
assert protection.closed
assert not any(name.startswith(("danbooru_multi_search.ui", "danbooru_multi_search.danbooru.query_controller")) for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
