from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from danbooru_multi_search.app.download_submission import DownloadSubmissionService
from danbooru_multi_search.core.downloads import DownloadConflictPolicy, DownloadError
from danbooru_multi_search.core.pools import DanbooruPool, PoolCategory
from danbooru_multi_search.app.settings import SettingsController
from danbooru_multi_search.core.settings import (
    AppSettings,
    SettingsError,
)
from danbooru_multi_search.storage.settings import SettingsStore
from danbooru_multi_search.danbooru.models import DanbooruPost


@pytest.fixture
def submission(tmp_path):
    initial = AppSettings(
        download_directory=str(tmp_path),
        theme="dark",
        tag_csv_path=str(tmp_path / "tags.csv"),
    )
    store = SettingsStore(tmp_path / "settings.json")
    store.save(initial)
    settings = SettingsController(store, initial)
    posts = (
        DanbooruPost(
            9, None, None, "https://example.invalid/9.jpg",
            0, 0, "g", 10, 10, "jpg",
        ),
    )
    pool = DanbooruPool(7, "Original order", PoolCategory.SERIES, (9, 8, 9))
    calls = []
    result = object()

    def enqueue_batch(value, directory, policy):
        calls.append(("posts", value, directory, policy))
        return result

    def start(value, directory, policy):
        calls.append(("pool", value, directory, policy))
        return result

    return SimpleNamespace(
        service=DownloadSubmissionService(settings),
        settings=settings,
        posts=posts,
        pool=pool,
        backend=SimpleNamespace(enqueue_batch=enqueue_batch, start=start),
        calls=calls,
        result=result,
    )


def submit(value, kind, directory, *, save_default=False, source_current=lambda: True):
    method = value.service.submit_posts if kind == "posts" else value.service.submit_pool
    source = value.posts if kind == "posts" else value.pool
    return method(
        value.backend,
        source,
        directory,
        "auto_number",
        save_default,
        source_current=source_current,
    )


@pytest.mark.parametrize("kind", ["posts", "pool"])
def test_confirmation_preserves_source_directory_and_policy_without_default_update(
    submission, tmp_path, kind
):
    destination = tmp_path / "confirmed directory"
    before = submission.settings.settings
    assert submit(submission, kind, destination) is submission.result
    source = submission.posts if kind == "posts" else submission.pool
    assert submission.calls == [
        (kind, source, destination, DownloadConflictPolicy.AUTO_NUMBER)
    ]
    assert submission.settings.settings is before
    assert not destination.exists()


@pytest.mark.parametrize("kind", ["posts", "pool"])
def test_setting_failure_preserves_previous_settings_and_starts_no_work(
    submission, tmp_path, monkeypatch, kind
):
    before = submission.settings.settings

    def fail(_settings):
        raise SettingsError("保存失败")

    monkeypatch.setattr(submission.settings._store, "save", fail)
    with pytest.raises(SettingsError, match="保存失败"):
        submit(submission, kind, tmp_path / "chosen", save_default=True)
    assert submission.settings.settings is before
    assert submission.calls == []


@pytest.mark.parametrize("kind", ["posts", "pool"])
def test_source_changed_by_settings_notification_never_creates_old_work(
    submission, tmp_path, monkeypatch, kind
):
    current = True
    original = submission.settings.set_download_directory

    def save(path):
        nonlocal current
        result = original(path)
        current = False
        return result

    monkeypatch.setattr(submission.settings, "set_download_directory", save)
    destination = tmp_path / "chosen"
    with pytest.raises(DownloadError) as error:
        submit(
            submission,
            kind,
            destination,
            save_default=True,
            source_current=lambda: current,
        )
    assert error.value.code == "stale_download"
    assert submission.calls == []
    saved = submission.settings.settings
    assert saved.download_directory == str(destination)
    assert saved.theme == "dark" and saved.tag_csv_path.endswith("tags.csv")


@pytest.mark.parametrize("kind", ["posts", "pool"])
def test_stale_source_is_rejected_before_saving_settings_or_creating_work(
    submission, tmp_path, kind
):
    before = submission.settings.settings
    with pytest.raises(DownloadError) as error:
        submit(
            submission,
            kind,
            tmp_path / "chosen",
            save_default=True,
            source_current=lambda: False,
        )
    assert error.value.code == "stale_download"
    assert submission.settings.settings is before
    assert submission.calls == []


def test_pure_submission_import_and_persistence_without_qt_or_ui(tmp_path):
    code = """
import importlib.abc
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

class RejectQtAndUi(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("PySide6", "danbooru_multi_search.ui")):
            raise AssertionError(fullname)

sys.meta_path.insert(0, RejectQtAndUi())
from danbooru_multi_search.app.download_submission import DownloadSubmissionService
from danbooru_multi_search.core.downloads import DownloadRequest
from danbooru_multi_search.core.pools import DanbooruPool, PoolCategory
from danbooru_multi_search.app.settings import SettingsController
from danbooru_multi_search.core.settings import AppSettings
from danbooru_multi_search.storage.settings import SettingsStore
from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.downloads import DownloadRepository
from danbooru_multi_search.storage.pool_downloads import PoolDownloadRepository

root = Path(sys.argv[1])
database = Database(root / "state.sqlite3")
database.initialize()
downloads = DownloadRepository(database)
pools = PoolDownloadRepository(database)
service = DownloadSubmissionService(SettingsController(SettingsStore(root / "settings.json"), AppSettings()))
backend = SimpleNamespace(
    enqueue_batch=lambda posts, directory, policy: downloads.create_batch(
        tuple(DownloadRequest(post.id, post.download_url, post.file_ext) for post in posts), directory, policy),
    start=lambda pool, directory, policy: pools.create(pool, "https://example.invalid", directory, policy),
)
post = DanbooruPost(9, None, None, "https://example.invalid/9.jpg", 0, 0, "g", 10, 10, "jpg")
batch = service.submit_posts(backend, (post,), root, "skip", False, source_current=lambda: True)
assert downloads.get_batch(batch.batch_id) == batch
pool = DanbooruPool(7, "Series", PoolCategory.SERIES, (9, 8, 9))
plan = service.submit_pool(backend, pool, root, "skip", False, source_current=lambda: True)
assert pools.get(plan.plan_id) == plan
assert [page.reference.post_id for page in pools.pages(plan.plan_id)] == [9, 8, 9]
for old_module in ("danbooru_multi_search.danbooru.download_manager", "danbooru_multi_search.danbooru.pool_downloads"):
    assert importlib.util.find_spec(old_module) is None
assert not any(name.startswith(("PySide6", "danbooru_multi_search.ui")) for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
