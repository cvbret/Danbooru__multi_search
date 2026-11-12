from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Event

import pytest

import danbooru_multi_search.storage.favorite_transfer_jobs as jobs
from danbooru_multi_search.core.favorites import FavoriteCandidate
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.favorites import FavoriteRepository
from danbooru_multi_search.storage.favorite_transfer import (
    FavoriteImportMode, FavoriteTransferService, FavoriteTransferError,
    read_favorite_transfer_document,
)


def database(path):
    value = Database(path)
    value.initialize()
    return value


def source_file(tmp_path):
    source = FavoriteRepository(database(tmp_path / "source.sqlite3"))
    source.add_favorite(FavoriteCandidate.for_pool(1, "Source pool"))
    path = tmp_path / "source.json"
    FavoriteTransferService(source).export_file(path)
    target = database(tmp_path / "target.sqlite3")
    return path, target


def request(operation, target, path=None, preview=None):
    return jobs.TransferRequest("job", operation, target.path, path, FavoriteImportMode.MERGE, preview)


def test_immutable_jobs_round_trip_original_preview_and_export(tmp_path):
    path, target = source_file(tmp_path)
    preview_request = request(jobs.TransferOperation.PREVIEW, target, path)
    preview = jobs.execute_transfer(preview_request, lambda: False)
    assert preview.status == "success"
    assert preview.preview.added_favorites == 1
    assert FavoriteRepository(target).list_favorites() == ()
    with pytest.raises(FrozenInstanceError):
        preview_request.path = Path("other")
    with pytest.raises(FrozenInstanceError):
        preview.message = "other"
    imported = jobs.execute_transfer(request(jobs.TransferOperation.APPLY, target, preview=preview.preview), lambda: False)
    assert imported.status == "success"
    assert len(imported.snapshot.favorites) == 1
    exported = tmp_path / "exported.json"
    result = jobs.execute_transfer(request(jobs.TransferOperation.EXPORT, target, exported), lambda: False)
    assert result.status == "success" and result.exported_count == 1
    assert len(read_favorite_transfer_document(exported).favorites) == 1


@pytest.mark.parametrize("stage", ["read", "decode", "preview"])
def test_preview_cancel_stops_before_the_next_step(tmp_path, monkeypatch, stage):
    path, target = source_file(tmp_path)
    cancelled = Event()
    calls = []
    for name, owner, attribute in (
        ("read", jobs, "_read_transfer_bytes"),
        ("decode", jobs, "decode_favorite_transfer_document"),
        ("preview", FavoriteTransferService, "preview_import"),
    ):
        original = getattr(owner, attribute)
        def wrap(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            result = _original(*args, **kwargs)
            if _name == stage:
                cancelled.set()
            return result
        monkeypatch.setattr(owner, attribute, wrap)
    result = jobs.execute_transfer(request(jobs.TransferOperation.PREVIEW, target, path), cancelled.is_set)
    assert result.status == "cancelled"
    assert calls == ["read", "decode", "preview"][:["read", "decode", "preview"].index(stage) + 1]
    assert FavoriteRepository(target).capture_collection().revision == 0


@pytest.mark.parametrize("stage", ["snapshot", "encode"])
def test_export_cancel_stops_before_file_creation(tmp_path, monkeypatch, stage):
    path, target = source_file(tmp_path)
    cancelled = Event()
    owner, attribute = (FavoriteTransferService, "create_export_document") if stage == "snapshot" else (jobs, "encode_favorite_transfer_document")
    original = getattr(owner, attribute)
    def cancel_after(*args, **kwargs):
        result = original(*args, **kwargs)
        cancelled.set()
        return result
    monkeypatch.setattr(owner, attribute, cancel_after)
    destination = tmp_path / "never-written.json"
    result = jobs.execute_transfer(request(jobs.TransferOperation.EXPORT, target, destination), cancelled.is_set)
    assert result.status == "cancelled"
    assert not destination.exists()
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_cancel_during_apply_preparation_prevents_transaction(tmp_path, monkeypatch):
    path, target = source_file(tmp_path)
    preview = jobs.execute_transfer(request(jobs.TransferOperation.PREVIEW, target, path), lambda: False).preview
    cancelled = Event()
    import danbooru_multi_search.storage.favorite_transfer as transfer
    original = transfer._document_digest
    def digest(document):
        result = original(document)
        cancelled.set()
        return result
    monkeypatch.setattr(transfer, "_document_digest", digest)
    result = jobs.execute_transfer(request(jobs.TransferOperation.APPLY, target, preview=preview), cancelled.is_set)
    assert result.status == "cancelled"
    assert FavoriteRepository(target).capture_collection().revision == 0


def test_existing_error_code_and_residual_path_are_returned_unchanged(tmp_path, monkeypatch):
    path, target = source_file(tmp_path)
    missing = jobs.execute_transfer(request(jobs.TransferOperation.PREVIEW, target, tmp_path / "missing.json"), lambda: False)
    assert missing.error_code == "invalid_file"
    residual = tmp_path / ".export.json.tmp"
    def fail(*args):
        raise FavoriteTransferError("file_cleanup_error", "cleanup failed", residual_path=residual)
    monkeypatch.setattr(jobs, "_write_transfer_bytes", fail)
    failed = jobs.execute_transfer(request(jobs.TransferOperation.EXPORT, target, tmp_path / "out.json"), lambda: False)
    assert failed.status == "error"
    assert failed.error_code == "file_cleanup_error"
    assert failed.message == "cleanup failed"
    assert failed.residual_path == residual
