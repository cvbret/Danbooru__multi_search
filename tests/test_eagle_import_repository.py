from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from danbooru_multi_search.storage import database as database_module
from danbooru_multi_search.storage.database import Database, DatabaseError
from danbooru_multi_search.storage.eagle_imports import (
    EagleImportAttempt,
    EagleImportRecord,
    EagleImportRepository,
)


DIGEST = "ac" * 32
OTHER_DIGEST = "1f" * 32
POST = EagleImportRecord("library-a", 15, "item-1", "https://danbooru.donmai.us/posts/15", DIGEST)


@pytest.fixture
def database(tmp_path):
    value = Database(tmp_path / "application.sqlite3")
    value.initialize()
    return value


def test_reopening_keeps_independent_libraries_and_shared_item_hash(database):
    repo = EagleImportRepository(database)
    alias = replace(POST, post_id=81, page_url="https://danbooru.donmai.us/posts/81", sha256=None)
    other_library = replace(POST, library_id="library-b", item_id="item-9", sha256=OTHER_DIGEST)
    repo.save(alias)
    assert repo.find_by_post(alias.library_id, alias.post_id) == alias
    assert repo.find_by_hash(alias.library_id, DIGEST) == ()
    repo.save(POST)
    repo.save(other_library)
    repo.save(replace(POST, sha256=None))

    reopened = EagleImportRepository(Database(database.path))
    assert reopened.find_by_post(POST.library_id, POST.post_id) == POST
    assert reopened.find_by_hash(POST.library_id, DIGEST.upper()) == (POST, replace(alias, sha256=DIGEST))
    assert reopened.find_by_hash(other_library.library_id, DIGEST) == ()
    assert reopened.find_by_post(other_library.library_id, POST.post_id) == other_library


def test_different_items_with_identical_bytes_remain_distinguishable(database):
    repo = EagleImportRepository(database)
    other = replace(POST, post_id=16, item_id="item-2", page_url="https://danbooru.donmai.us/posts/16")
    repo.save(POST)
    repo.save(other)
    assert repo.find_by_hash(POST.library_id, DIGEST) == (POST, other)


def test_two_workers_cannot_reserve_the_same_content(database):
    start = Barrier(2)

    def reserve(post_id):
        repo = EagleImportRepository(Database(database.path))
        start.wait(timeout=10)
        return repo.begin_attempt(POST.library_id, post_id, DIGEST)

    with ThreadPoolExecutor(max_workers=2) as workers:
        tokens = tuple(workers.map(reserve, (15, 16)))
    assert sum(token is not None for token in tokens) == 1
    repo = EagleImportRepository(database)
    (attempt,) = repo.find_attempts(POST.library_id, 17, DIGEST)
    assert attempt.token in tokens
    assert repo.begin_attempt(POST.library_id, attempt.post_id, OTHER_DIGEST) is None
    assert repo.begin_attempt("library-b", attempt.post_id, DIGEST) is not None


def test_uncertain_attempt_survives_restart_and_confirms_only_the_matching_request(database, tmp_path):
    repo = EagleImportRepository(database)
    path = tmp_path / "作品.jpg"
    token = repo.begin_attempt(
        POST.library_id, POST.post_id, DIGEST, file_path=path, page_url=POST.page_url,
    )
    other_token = repo.begin_attempt(POST.library_id, 16, OTHER_DIGEST)
    assert token is not None and other_token is not None
    expected = EagleImportAttempt(token, POST.library_id, POST.post_id, DIGEST, path, POST.page_url)
    other = EagleImportAttempt(other_token, POST.library_id, 16, OTHER_DIGEST)

    reopened = EagleImportRepository(Database(database.path))
    assert reopened.find_attempts(POST.library_id, POST.post_id, OTHER_DIGEST) == (expected, other)
    assert reopened.find_by_post(POST.library_id, POST.post_id) is None
    for stale in (
        replace(POST, library_id="library-b"),
        replace(POST, post_id=16),
        replace(POST, sha256=OTHER_DIGEST),
        replace(POST, page_url="https://example.invalid"),
    ):
        with pytest.raises(DatabaseError, match="请求不匹配"):
            reopened.save(stale, attempt_token=token)
    with pytest.raises(DatabaseError, match="请求不匹配"):
        reopened.save(POST, attempt_token="stale-token")
    assert reopened.find_attempts(POST.library_id, POST.post_id, DIGEST) == (expected,)
    reopened.save(POST, attempt_token=token)
    assert reopened.find_attempts(POST.library_id, POST.post_id, DIGEST) == ()
    assert reopened.find_by_post(POST.library_id, POST.post_id) == POST
    assert reopened.find_attempts(POST.library_id, 16, OTHER_DIGEST) == (other,)
    reopened.release_attempt(other_token)
    assert reopened.find_attempts(POST.library_id, 16, OTHER_DIGEST) == ()


def test_conflicting_confirmation_rolls_back_both_mapping_and_reservation(database):
    repo = EagleImportRepository(database)
    repo.save(POST)
    token = repo.begin_attempt(POST.library_id, POST.post_id, OTHER_DIGEST)
    assert token is not None
    with pytest.raises(DatabaseError, match="不同的文件 SHA256"):
        repo.save(replace(POST, sha256=OTHER_DIGEST), attempt_token=token)
    with pytest.raises(DatabaseError, match="不同的 Eagle 素材编号"):
        repo.save(replace(POST, item_id="item-2", sha256=OTHER_DIGEST), attempt_token=token)
    assert repo.find_by_post(POST.library_id, POST.post_id) == POST
    assert repo.find_by_hash(POST.library_id, OTHER_DIGEST) == ()
    assert repo.find_attempts(POST.library_id, POST.post_id, OTHER_DIGEST)[0].token == token
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM eagle_import_items").fetchone()[0] == 1


def test_forget_uses_the_exact_observed_mapping_and_preserves_other_posts_and_pending_adds(database):
    repo = EagleImportRepository(database)
    unknown_file = replace(POST, sha256=None)
    repo.save(unknown_file)
    repo.save(POST)
    assert not repo.forget(unknown_file)
    alias = replace(POST, post_id=16, page_url="https://danbooru.donmai.us/posts/16")
    repo.save(alias)
    token = repo.begin_attempt(POST.library_id, POST.post_id, OTHER_DIGEST)
    assert repo.forget(POST)
    assert not repo.forget(POST)
    assert repo.find_by_hash(POST.library_id, DIGEST) == (alias,)
    assert repo.find_attempts(POST.library_id, POST.post_id, OTHER_DIGEST)[0].token == token
    assert repo.forget(alias)
    assert repo.find_by_hash(POST.library_id, DIGEST) == ()
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM eagle_import_items").fetchone()[0] == 0


@pytest.mark.parametrize("fail", [True, False])
def test_migrating_version_twelve_preserves_previous_data_and_rolls_back_errors(tmp_path, monkeypatch, fail):
    migrations = database_module.MIGRATIONS[:13]
    database = Database(tmp_path / "application.sqlite3")
    monkeypatch.setattr(database_module, "MIGRATIONS", migrations[:12])
    assert database.initialize() == 12
    with database.connect() as conn:
        conn.execute("INSERT INTO app_metadata VALUES ('existing', '旧数据')")
        conn.execute("INSERT INTO download_batches VALUES ('download-1', '2026-01-01')")
    if fail:
        migration = replace(migrations[-1], statements=migrations[-1].statements + ("invalid SQL",))
        monkeypatch.setattr(database_module, "MIGRATIONS", migrations[:12] + (migration,))
        with pytest.raises(DatabaseError):
            database.initialize()
        assert database.current_version() == 12
        with database.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'eagle_import_%'").fetchone()[0] == 0
    else:
        monkeypatch.setattr(database_module, "MIGRATIONS", migrations)
        assert database.initialize() == database.initialize() == 13
    with database.connect() as conn:
        assert conn.execute("SELECT value FROM app_metadata WHERE key='existing'").fetchone()[0] == "旧数据"
        assert conn.execute("SELECT id FROM download_batches").fetchone()[0] == "download-1"
