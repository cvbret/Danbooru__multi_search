from dataclasses import replace
import sqlite3

import pytest

from danbooru_multi_search.core.downloads import DownloadConflictPolicy, DownloadError
from danbooru_multi_search.core.pool_pages import PoolPageBatch, PoolPageSlot, PoolPageState, PoolPostMetadata
from danbooru_multi_search.core.pools import DanbooruPool, PoolCategory
from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.downloads import DownloadRepository
from danbooru_multi_search.storage.pool_downloads import PoolDownloadRepository


def pool(count=3, ids=None):
    return DanbooruPool(7, '中文 系列', PoolCategory.SERIES, ids or tuple(range(1, count + 1)))


def batch(value, refs, *, missing=()):
    def metadata(ref):
        if ref.page_number in missing:
            return PoolPostMetadata(ref.post_id, PoolPageState.NOT_RETURNED, message='未返回此作品')
        post = DanbooruPost(id=ref.post_id, preview_url='https://example.invalid/preview.jpg',
            large_file_url=None, file_url=f'https://example.invalid/{ref.post_id}.png', score=0,
            fav_count=0, rating='g', width=10, height=10, file_ext='png', general_tags=())
        return PoolPostMetadata(ref.post_id, PoolPageState.AVAILABLE, post)
    return PoolPageBatch(tuple(PoolPageSlot(ref, metadata(ref)) for ref in refs))


def build(tmp_path):
    database = Database(tmp_path / 'state.sqlite3')
    database.initialize()
    return database, PoolDownloadRepository(database), DownloadRepository(database)


def create(saved, value, tmp_path):
    return saved.create(value, 'https://example.invalid', tmp_path / '下载 中文', DownloadConflictPolicy.SKIP)


def test_maximum_manifest_preserves_all_original_positions_and_reopens_without_files(tmp_path):
    database, saved, downloads = build(tmp_path)
    value = pool(25000)
    plan = create(saved, value, tmp_path)
    assert saved.summaries()[0].plan == plan
    assert [page.reference.page_number for page in saved.pages(plan.plan_id, offset=24960)] == list(range(24961, 25001))
    reopened = PoolDownloadRepository(Database(database.path))
    reopened.recover_unfinished()
    assert reopened.get(plan.plan_id).state.value == 'paused'
    assert not plan.directory.exists() and downloads.list_batches() == ()


def test_atomic_association_rolls_back_then_retries_once_with_gaps_and_duplicate_ids(tmp_path):
    database, saved, downloads = build(tmp_path)
    value = pool(ids=(9, 8, 9))
    plan = create(saved, value, tmp_path)
    saved.record_metadata(plan, batch(value, value.pages, missing=(2,)))
    with database.connect() as connection:
        connection.execute("CREATE TRIGGER fail_link BEFORE UPDATE OF item_id ON pool_download_pages BEGIN SELECT RAISE(ABORT,'link failed'); END")
    with pytest.raises(DownloadError, match='link failed'):
        saved.commit_group(plan, 1)
    assert downloads.list_batches() == ()
    assert saved.pages(plan.plan_id)[0].state.value == 'ready'
    with database.connect() as connection:
        connection.execute('DROP TRIGGER fail_link')
    result = saved.commit_group(plan, 1)
    assert [item.request.filename for item in result.items] == ['00001_9.png', '00003_9.png']
    assert saved.commit_group(plan, 1) is None
    saved.complete(plan)
    current = saved.get(plan.plan_id)
    retried = saved.resume(current, current.source, retry_missing=True)
    work = saved.next_work(retried)
    assert len(work) == 1 and work[0].reference.page_number == 2
    assert len(downloads.list_batches()) == 1


def test_locked_writer_and_stale_generation_fail_without_partial_changes(tmp_path):
    database, saved, downloads = build(tmp_path)
    value = pool()
    plan = create(saved, value, tmp_path)
    paused = saved.stop(plan)
    with pytest.raises(DownloadError, match='改变'):
        saved.record_metadata(plan, batch(value, value.pages))
    with pytest.raises(DownloadError, match='来源'):
        saved.resume(paused, 'https://other.invalid')
    with database.connect() as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        with pytest.raises(DownloadError, match='locked'):
            saved.resume(paused, plan.source)
        blocker.rollback()
    assert saved.get(plan.plan_id) == paused and downloads.list_batches() == ()


def test_missing_original_never_uses_preview_and_failed_page_keeps_its_number(tmp_path):
    _db, saved, downloads = build(tmp_path)
    value = pool()
    plan = create(saved, value, tmp_path)
    good = batch(value, value.pages)
    first = good.slots[0]
    first = replace(first, metadata=replace(first.metadata, post=replace(first.metadata.post, file_url=None)))
    saved.record_metadata(plan, PoolPageBatch((first,) + good.slots[1:]))
    result = saved.commit_group(plan, 1)
    assert [item.request.filename for item in result.items] == ['00002_2.png', '00003_3.png']
    assert '原文件' in saved.pages(plan.plan_id)[0].message


@pytest.mark.parametrize('fail', [False, True])
def test_v11_to_v12_preserves_progress_and_download_rows_and_rolls_back(tmp_path, monkeypatch, fail):
    import danbooru_multi_search.storage.database as module
    from danbooru_multi_search.core.pool_progress import PoolReadingProgress
    from danbooru_multi_search.storage.pool_progress import PoolProgressRepository
    from datetime import datetime, timezone
    from danbooru_multi_search.core.downloads import DownloadRequest
    migrations = module.MIGRATIONS
    with monkeypatch.context() as previous:
        previous.setattr(module, 'MIGRATIONS', migrations[:11])
        database, _, downloads = build(tmp_path)
    old = downloads.create_batch((DownloadRequest(9, 'https://example.invalid/9.png', 'png'),), tmp_path)
    progress = PoolProgressRepository(database)
    record = progress.save(PoolReadingProgress('https://example.invalid', 7, 9, 2, datetime.now(timezone.utc)), expected=None)
    if fail:
        broken = replace(migrations[11], statements=migrations[11].statements + ('INSERT INTO missing_table VALUES (1)',))
        monkeypatch.setattr(module, 'MIGRATIONS', migrations[:11] + (broken,))
        with pytest.raises(module.DatabaseError):
            database.initialize()
        with database.connect() as connection:
            assert connection.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 11
            assert not connection.execute("SELECT name FROM sqlite_master WHERE name='pool_download_pages'").fetchone()
    else:
        monkeypatch.setattr(module, 'MIGRATIONS', migrations[:12])
        assert database.initialize() == database.initialize() == 12
    assert downloads.get_batch(old.batch_id) == old and progress.get(record.source, 7) == record
