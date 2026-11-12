from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from danbooru_multi_search.core.downloads import DownloadConflictPolicy, DownloadError
from danbooru_multi_search.storage.download_files import commit_file, discard_owned_partial, resolve_destination, target_key
from tests.test_download_repository import repository, request


def item(tmp_path, policy=DownloadConflictPolicy.SKIP):
    saved = repository(tmp_path)
    return saved.create_batch((request(),), tmp_path / "中文 下载", policy).items[0]


@pytest.mark.parametrize("policy,expected,skipped", [
    (DownloadConflictPolicy.SKIP, "321.JPG", True),
    (DownloadConflictPolicy.OVERWRITE, "321.JPG", False),
    (DownloadConflictPolicy.AUTO_NUMBER, "321 (1).jpg", False),
])
def test_conflicts_are_case_insensitive_on_every_platform(tmp_path, policy, expected, skipped):
    current = item(tmp_path, policy)
    current.directory.mkdir()
    (current.directory / "321.JPG").write_bytes(b"old")
    decision = resolve_destination(current, set())
    assert decision.path.name == expected and decision.skipped is skipped


def test_runtime_claims_serialize_overwrite_and_keep_auto_number_decisions(tmp_path):
    current = item(tmp_path, DownloadConflictPolicy.OVERWRITE)
    claimed = target_key(current.directory / "321.JPG")
    assert resolve_destination(current, {claimed}).blocked
    numbered = replace(current, policy=DownloadConflictPolicy.AUTO_NUMBER, resolved_name="321 (4).jpg")
    assert resolve_destination(numbered, {claimed}).path.name == "321 (4).jpg"
    assert resolve_destination(replace(numbered, resolved_name=None), {claimed}).path.name == "321 (1).jpg"


def test_late_file_collision_does_not_replace_the_existing_file(tmp_path):
    target = tmp_path / "321.jpg"
    target.write_bytes(b"old")
    partial = tmp_path / ".test.part"
    partial.write_bytes(b"new")
    with pytest.raises(FileExistsError):
        commit_file(partial, target, DownloadConflictPolicy.AUTO_NUMBER)
    assert target.read_bytes() == b"old" and partial.read_bytes() == b"new"


def test_replace_failure_preserves_old_complete_file(tmp_path, monkeypatch):
    import danbooru_multi_search.storage.download_files as files
    target, partial = tmp_path / "321.jpg", tmp_path / ".test.part"
    target.write_bytes(b"old")
    partial.write_bytes(b"new")
    def fail(*args):
        raise OSError("replace failed")
    monkeypatch.setattr(files.os, "replace", fail)
    with pytest.raises(OSError):
        commit_file(partial, target, DownloadConflictPolicy.OVERWRITE)
    assert target.read_bytes() == b"old"


def test_owned_cleanup_never_deletes_an_existing_complete_or_other_task_file(tmp_path):
    current = item(tmp_path)
    current.directory.mkdir()
    complete = current.directory / "321.jpg"
    complete.write_bytes(b"old")
    with pytest.raises(DownloadError):
        discard_owned_partial(replace(current, partial_name="321.jpg"))
    assert complete.read_bytes() == b"old"
    name = f".download-{current.item_id}-1.part"
    partial = current.directory / name
    partial.write_bytes(b"partial")
    discard_owned_partial(replace(current, generation=2, partial_name=name))
    assert not partial.exists() and complete.read_bytes() == b"old"


def test_future_attempt_partial_is_never_removed(tmp_path):
    current = item(tmp_path)
    current.directory.mkdir()
    name = f".download-{current.item_id}-3.part"
    path = current.directory / name
    path.write_bytes(b"not owned by an existing attempt")
    with pytest.raises(DownloadError):
        discard_owned_partial(replace(current, generation=2, partial_name=name))
    assert path.exists()


@pytest.mark.parametrize("policy", [DownloadConflictPolicy.AUTO_NUMBER, DownloadConflictPolicy.OVERWRITE])
def test_late_case_variant_is_rejected_before_any_commit_operation(tmp_path, monkeypatch, policy):
    import danbooru_multi_search.storage.download_files as files
    existing = tmp_path / "321.JPG"
    existing.write_bytes(b"previous complete")
    partial = tmp_path / ".owned.part"
    partial.write_bytes(b"new")
    def forbidden(*args):
        raise AssertionError("late case-insensitive collision must be handled before commit")
    monkeypatch.setattr(files.os, "link", forbidden)
    monkeypatch.setattr(files.os, "replace", forbidden)
    with pytest.raises(FileExistsError):
        commit_file(partial, tmp_path / "321.jpg", policy)
    assert existing.read_bytes() == b"previous complete" and partial.exists()
