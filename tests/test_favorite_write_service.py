from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from danbooru_multi_search.app.favorites import FavoriteWriteService
from danbooru_multi_search.app.write_access import SharedWriteAccess
from danbooru_multi_search.core.favorites import FavoriteCandidate, FavoriteError
from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.favorites import FavoriteRepository


@pytest.fixture
def writing(tmp_path):
    database = Database(tmp_path / "favorites.sqlite3")
    database.initialize()
    repository = FavoriteRepository(database)
    access = SharedWriteAccess()
    return repository, access, FavoriteWriteService(repository, access)


def post(identifier=1):
    return DanbooruPost(
        identifier, None, None, f"https://example.invalid/{identifier}.jpg",
        0, 17, "g", 10, 10, "jpg",
    )


def test_committed_facts_are_published_while_the_write_is_still_held(writing):
    repository, access, service = writing
    facts = []

    def publish(values):
        assert access.busy and access.local_write_active
        facts.extend(values.values())
        assert all(repository.get_by_identity(item.identity) == item for item in values.values())
        return True

    assert service.set_post(
        post(), True, guard=lambda: True, publish=publish, failed=pytest.fail
    )
    assert len(facts) == 1 and repository.capture_collection().revision == 1
    assert not access.busy


def test_failure_callback_runs_before_releasing_and_does_not_publish(writing, monkeypatch):
    repository, access, service = writing
    errors = []

    def fail(*_args):
        raise FavoriteError("storage_error", "write failed")

    def report(error):
        assert access.local_write_active and access.busy
        errors.append(error.code)

    monkeypatch.setattr(repository, "add_favorites", fail)
    assert not service.set_post(
        post(), True, guard=lambda: True, publish=pytest.fail, failed=report
    )
    assert errors == ["storage_error"]
    assert not access.busy and repository.capture_collection().revision == 0


def test_acquisition_notification_invalidates_guard_before_any_transaction(writing):
    repository, access, service = writing
    current = True

    def invalidate():
        nonlocal current
        if access.busy:
            current = False

    access.changed.connect(invalidate)
    assert not service.set_post(
        post(), True, guard=lambda: current, publish=pytest.fail, failed=pytest.fail
    )
    assert repository.capture_collection().revision == 0


def test_candidate_validation_rechecks_guard_before_any_transaction(writing, monkeypatch):
    repository, _access, service = writing
    original = FavoriteCandidate.for_post
    current = True

    def validate(value):
        nonlocal current
        result = original(value)
        current = False
        return result

    monkeypatch.setattr(FavoriteCandidate, "for_post", validate)
    assert not service.set_post(
        post(), True, guard=lambda: current, publish=pytest.fail, failed=pytest.fail
    )
    assert repository.capture_collection().revision == 0


def test_cancel_uses_identity_even_when_current_post_cannot_be_saved(writing):
    repository, _access, service = writing
    observed = []
    callbacks = dict(guard=lambda: True, publish=lambda values: observed.append(values) or True, failed=pytest.fail)
    work = post()
    assert service.set_post(work, True, **callbacks)
    restricted = replace(work, file_url=None)
    assert service.set_post(restricted, False, **callbacks)
    assert service.set_post(restricted, False, **callbacks)
    assert repository.capture_collection().favorites == ()
    assert repository.capture_collection().revision == 2
    assert work.fav_count == 17


def test_batch_failure_rolls_back_before_reporting_under_write_access(writing):
    repository, access, service = writing
    with repository._database.connect() as connection:
        connection.execute(
            "CREATE TRIGGER fail_second BEFORE INSERT ON favorite_items "
            "WHEN NEW.object_key = '2' BEGIN SELECT RAISE(ABORT, 'second failed'); END"
        )
    errors = []

    def report(error):
        assert access.busy
        assert repository.capture_collection().revision == 0
        assert repository.list_favorites() == ()
        errors.append(str(error))

    assert not service.save_posts(
        (post(1), post(2)), guard=lambda: True, publish=pytest.fail, failed=report
    )
    assert len(errors) == 1 and "second failed" in errors[0]


def test_folder_revision_failure_keeps_committed_folder_and_releases_access(writing):
    repository, access, service = writing
    published = []
    callbacks = dict(guard=lambda: True, publish=lambda value: published.append(value) or True, failed=pytest.fail)
    assert service.create_folder("original", **callbacks)
    folder = repository.list_folders()[0]
    revision = repository.capture_collection().revision
    repository.create_folder("foreign")
    errors = []
    assert not service.rename_folder(
        folder.id, "replacement", revision, guard=lambda: True,
        publish=pytest.fail,
        failed=lambda error: errors.append((access.busy, error.code)),
    )
    assert len(published) == 1
    assert errors == [(True, "stale_preview")]
    assert next(value for value in repository.list_folders() if value.id == folder.id).name == "original"
    assert not access.busy


def test_service_import_and_real_mutation_do_not_load_ui(tmp_path):
    code = """
import importlib.abc
import sys
from pathlib import Path

class RejectUi(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("danbooru_multi_search.ui"):
            raise AssertionError(fullname)

sys.meta_path.insert(0, RejectUi())
from danbooru_multi_search.app.favorites import FavoriteWriteService
from danbooru_multi_search.app.write_access import SharedWriteAccess
from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.favorites import FavoriteRepository
database = Database(Path(sys.argv[1]) / "cold.sqlite3")
database.initialize()
repository = FavoriteRepository(database)
access = SharedWriteAccess()
service = FavoriteWriteService(repository, access)
post = DanbooruPost(1, None, None, "https://example.invalid/1.jpg", 0, 0, "g", 10, 10, "jpg")
def publish(values):
    assert access.busy and len(values) == 1
    return True
def fail(error):
    raise AssertionError(error)
assert service.set_post(post, True, guard=lambda: True, publish=publish, failed=fail)
assert len(repository.list_favorites()) == 1 and not access.busy
assert not any(name.startswith("danbooru_multi_search.ui") for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
