from dataclasses import replace
from pathlib import Path

import pytest

from danbooru_multi_search.core.downloads import DownloadConflictPolicy, DownloadError
from danbooru_multi_search.core.pool_downloads import (
    PoolDownloadPage, PoolDownloadPageState, PoolDownloadPlan, PoolDownloadState, pool_download_request,
)
from danbooru_multi_search.core.pools import PoolPageReference


def plan(tmp_path):
    return PoolDownloadPlan('a' * 32, 'https://example.invalid/api', 7, '系列', 25000,
                            tmp_path, DownloadConflictPolicy.SKIP, PoolDownloadState.PREPARING)


def test_original_pages_sort_with_duplicates_gaps_and_original_formats():
    requests = [pool_download_request(PoolPageReference(7, page, identifier),
                f'https://example.invalid/{identifier}.{ext}', ext)
                for page, identifier, ext in [(1, 9, 'PNG'), (3, 9, 'gif'), (1001, 5, 'webm'), (25000, 4, 'mp4')]]
    assert [value.filename for value in requests] == ['00001_9.png', '00003_9.gif', '01001_5.webm', '25000_4.mp4']
    assert sorted(value.filename for value in requests) == [value.filename for value in requests]


@pytest.mark.parametrize('changes', [{'total': 25001}, {'total': True}, {'generation': 1.5},
    {'source': 'https://user:secret@example.invalid'}, {'state': 'preparing'}, {'directory': Path('relative')}])
def test_invalid_plan_cannot_be_persisted(tmp_path, changes):
    with pytest.raises((DownloadError, ValueError)):
        replace(plan(tmp_path), **changes)


def test_page_identity_and_ready_request_must_match_original_position():
    ref = PoolPageReference(7, 2, 99)
    request = pool_download_request(ref, 'https://example.invalid/99.jpg', 'jpg')
    assert PoolDownloadPage(ref, PoolDownloadPageState.READY, request).request is request
    with pytest.raises(DownloadError):
        PoolDownloadPage(ref, PoolDownloadPageState.READY, replace(request, label='00001'))
    with pytest.raises(DownloadError):
        PoolDownloadPage(ref, PoolDownloadPageState.ENQUEUED, request)


def test_pool_download_core_and_repository_can_be_used_without_qt(tmp_path):
    import subprocess
    import sys
    code = '''
import importlib.abc, sys
class BlockQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('PySide6'):
            raise AssertionError('pure pool downloads imported Qt')
sys.meta_path.insert(0, BlockQt())
from pathlib import Path
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.pool_downloads import PoolDownloadRepository
from danbooru_multi_search.core.pools import DanbooruPool, PoolCategory
from danbooru_multi_search.core.downloads import DownloadConflictPolicy
db = Database(Path(sys.argv[1]) / 'pure.sqlite3')
db.initialize()
repo = PoolDownloadRepository(db)
pool = DanbooruPool(7, 'series', PoolCategory.SERIES, (9, 9))
plan = repo.create(pool, 'https://example.invalid', Path(sys.argv[1]), DownloadConflictPolicy.SKIP)
assert [p.reference.page_number for p in repo.pages(plan.plan_id)] == [1, 2]
assert not any(name.startswith('PySide6') for name in sys.modules)
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
