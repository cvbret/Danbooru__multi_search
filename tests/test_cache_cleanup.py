from __future__ import annotations

from dataclasses import replace

import pytest

from danbooru_multi_search.core.favorites import FavoriteCandidate
from danbooru_multi_search.danbooru.query_types import QueryExecutionLimits, QueryExecutionState
from danbooru_multi_search.danbooru.result_processing import ResultFilterSpec
from danbooru_multi_search.storage.cache_protection import CacheProtectionRepository
from danbooru_multi_search.ui.cache_protection import CacheProtectionController
from danbooru_multi_search.ui.cache_cleanup import CacheCleanupController
from danbooru_multi_search.ui.image_cache import ImageCacheController
from tests.test_checkpoint_controller import build, close
from tests.test_query_controller import _post
from tests.test_protected_image_cache import insert


def setup(tmp_path):
    values = build(tmp_path)
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    images = ImageCacheController(tmp_path / "images")
    protection = CacheProtectionController(CacheProtectionRepository(database), shared, images, access=shared._writes.access)
    cleanup = CacheCleanupController(history, repository, checkpoints._service, session, shared._writes.access, images)
    harness.controller.start_standard("a", limits=QueryExecutionLimits(page_size=1))
    harness.dispatch()
    harness.succeed((_post(1, "a"),))
    return values, images, protection, cleanup


def finish(data):
    values, images, protection, cleanup = data
    cleanup.shutdown()
    close(values)
    images.shutdown()
    protection.shutdown()


def test_cancel_confirmation_leaves_query_and_records_untouched(tmp_path):
    data = setup(tmp_path)
    values, images, protection, cleanup = data
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    try:
        before = repository.get_for_history(history.latest_query().id)
        assert cleanup.prepareCleanup("history")
        assert cleanup.confirmation["count"] == 1
        token = cleanup.confirmation["token"]
        cleanup.cancelCleanup()
        assert not cleanup.confirmCleanup(token)
        assert repository.get_for_history(before.history_id) == before
        assert harness.executor.is_execution_active(harness.executor.current_execution_id)
    finally:
        finish(data)


def test_checkpoint_clear_accepts_normal_progress_and_keeps_local_filtering_and_history(tmp_path):
    data = setup(tmp_path)
    values, images, protection, cleanup = data
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    try:
        assert cleanup.prepareCleanup("checkpoints")
        token = cleanup.confirmation["token"]
        harness.succeed((_post(2, "a"),))
        before_posts = harness.model.posts
        assert cleanup.confirmCleanup(token), cleanup.noticeText
        assert repository.capture_cleanup() == () and history.latest_query() is not None
        assert harness.executor.snapshot.state is QueryExecutionState.PARTIAL_CANCELLED
        assert harness.model.posts is before_posts
        assert not checkpoints._service._leases
        assert harness.controller.update_view(filter_spec=ResultFilterSpec(minimum_score=0))
        assert repository.capture_cleanup() == () and "保存失败" not in checkpoints.messageText
    finally:
        finish(data)


def test_new_execution_with_reused_history_id_invalidates_old_confirmation(tmp_path):
    data = setup(tmp_path)
    values, images, protection, cleanup = data
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    try:
        assert cleanup.prepareCleanup("history")
        token = cleanup.confirmation["token"]
        identifier = history.latest_query().id
        harness.controller.start_standard("a", limits=harness.controller.definition.limits)
        harness.dispatch()
        new = repository.get_for_history(identifier)
        assert not cleanup.confirmCleanup(token)
        assert history.latest_query().id == identifier and repository.get_for_history(identifier) == new
        assert harness.executor.is_execution_active(harness.executor.current_execution_id)
        assert "归属已改变" in cleanup.noticeText
    finally:
        finish(data)


def test_foreign_claim_invalidates_confirmation_without_overwriting_its_owner(tmp_path):
    data = setup(tmp_path)
    values, images, protection, cleanup = data
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    try:
        harness.controller.cancel()
        before = repository.get_for_history(history.latest_query().id)
        assert cleanup.prepareCleanup("checkpoints")
        token = cleanup.confirmation["token"]
        claimed = repository.claim(before, before.checkpoint.source)
        assert not cleanup.confirmCleanup(token)
        assert repository.get_for_history(before.history_id) == claimed
        assert not checkpoints._service._leases
    finally:
        finish(data)


@pytest.mark.parametrize("kind", ["history", "checkpoints"])
@pytest.mark.parametrize("action", ["view", "close"])
def test_deleted_records_retire_writes_before_first_history_notification(tmp_path, kind, action):
    data = setup(tmp_path)
    values, images, protection, cleanup = data
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    try:
        identifier = history.latest_query().id
        assert cleanup.prepareCleanup(kind)
        observed = []
        def reenter():
            if observed or repository.get_for_history(identifier) is not None:
                return
            observed.append(True)
            assert not checkpoints._service._leases
            if action == "view":
                assert harness.controller.update_view(filter_spec=ResultFilterSpec(minimum_score=0))
            else:
                close(values)
        session.historyChanged.connect(reenter)
        assert cleanup.confirmCleanup(cleanup.confirmation["token"]), cleanup.noticeText
        assert observed == [True] and repository.capture_cleanup() == ()
        assert "保存失败" not in checkpoints.messageText
        if kind == "history":
            assert history.list_queries() == ()
        else:
            assert history.list_queries()
    finally:
        finish(data)


def test_delete_transaction_failure_keeps_current_lease_and_checkpoint(tmp_path):
    data = setup(tmp_path)
    values, images, protection, cleanup = data
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    try:
        assert cleanup.prepareCleanup("checkpoints")
        with database.connect() as connection:
            connection.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON query_checkpoints BEGIN SELECT RAISE(ABORT, 'delete failure'); END")
        assert not cleanup.confirmCleanup(cleanup.confirmation["token"])
        assert repository.capture_cleanup() and checkpoints._service.current_ownership is not None
        assert not checkpoints._service._retired_executions
        with database.connect() as connection:
            connection.execute("DROP TRIGGER reject_delete")
    finally:
        finish(data)


def test_image_clear_uses_latest_favorites_and_does_not_cancel_query_or_download_files(tmp_path):
    data = setup(tmp_path)
    values, images, protection, cleanup = data
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    try:
        favorite, ordinary = "https://example.invalid/saved.jpg", "https://example.invalid/ordinary.jpg"
        cache = images._ensure_cache()
        insert(cache, favorite)
        insert(cache, ordinary)
        sentinel = tmp_path / "images" / "download.part"
        sentinel.write_bytes(b"download")
        assert cleanup.prepareCleanup("images")
        # No state/collection notification is sent: confirmation must read current SQL facts.
        shared._repository.add_favorite(FavoriteCandidate.for_post(replace(_post(9, "a"), preview_url=favorite, file_url=None)))
        execution_id = harness.executor.current_execution_id
        assert cleanup.confirmCleanup(cleanup.confirmation["token"]), cleanup.noticeText
        from PySide6.QtCore import QUrl
        assert cache.metaData(QUrl(favorite)).isValid() and not cache.metaData(QUrl(ordinary)).isValid()
        assert harness.executor.is_execution_active(execution_id)
        assert sentinel.read_bytes() == b"download" and not shared.busy
    finally:
        finish(data)


def test_close_from_image_cleanup_notice_allows_final_query_save_under_own_occupation(tmp_path):
    data = setup(tmp_path)
    values, images, protection, cleanup = data
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    try:
        cache = images._ensure_cache()
        insert(cache, "https://example.invalid/ordinary.jpg")
        assert cleanup.prepareCleanup("images")
        closed = []
        def shutdown():
            if closed or not images.statusText.startswith("已清理"):
                return
            closed.append(True)
            cleanup.shutdown()
            close(values)
            images.shutdown()
            protection.shutdown()
        images.statusChanged.connect(shutdown)
        cleanup.confirmCleanup(cleanup.confirmation["token"])
        assert closed == [True] and checkpoints._closed
        saved = repository.get_for_history(history.latest_query().id)
        assert not saved.checkpoint.execution.active
        assert saved.checkpoint.execution.state is QueryExecutionState.PARTIAL_CANCELLED
    finally:
        finish(data)


@pytest.mark.parametrize("kind", ["history", "checkpoints"])
def test_new_query_from_cancel_notification_is_not_deleted_by_old_confirmation(tmp_path, kind):
    data = setup(tmp_path)
    values, images, protection, cleanup = data
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    try:
        assert cleanup.prepareCleanup(kind)
        token = cleanup.confirmation["token"]
        identifier = history.latest_query().id
        original_limits = harness.controller.definition.limits
        started = []
        def new_query():
            if not started and harness.executor.snapshot.state is QueryExecutionState.PARTIAL_CANCELLED:
                started.append(True)
                harness.controller.start_standard("a", limits=original_limits)
        session.historyChanged.connect(new_query)
        assert not cleanup.confirmCleanup(token)
        harness.dispatch()
        assert started == [True]
        current = repository.get_for_history(identifier)
        assert current is not None and current.checkpoint.execution.active
        assert checkpoints._service.current_ownership.owner == current.owner
        assert harness.executor.is_execution_active(harness.executor.current_execution_id)
    finally:
        finish(data)


@pytest.mark.parametrize("kind", ["history", "checkpoints"])
def test_new_query_from_own_reservation_notification_keeps_its_checkpoint_and_network(tmp_path, kind):
    data = setup(tmp_path)
    values, images, protection, cleanup = data
    harness, database, history, shared, bridge, repository, checkpoints, session = values
    try:
        assert cleanup.prepareCleanup(kind)
        token = cleanup.confirmation["token"]
        original_limits = harness.controller.definition.limits
        started = []
        def new_query():
            if shared.busy and not started:
                started.append(True)
                harness.controller.start_standard("a", limits=original_limits)
        shared.busyChanged.connect(new_query)
        assert not cleanup.confirmCleanup(token)
        harness.dispatch()
        current = repository.get_for_history(history.latest_query().id)
        assert started == [True] and current.checkpoint.execution.active
        assert harness.executor.is_execution_active(harness.executor.current_execution_id)
        assert checkpoints._service.current_ownership.owner == current.owner
        assert "保存失败" not in bridge.errorText
    finally:
        finish(data)
