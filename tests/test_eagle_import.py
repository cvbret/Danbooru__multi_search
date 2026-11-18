from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import struct
from types import SimpleNamespace
import zlib

import pytest

from danbooru_multi_search.app.eagle_import_metadata import annotation_markers, build_annotation
from danbooru_multi_search.app.eagle_import_types import EaglePostImportRequest
from danbooru_multi_search.app.eagle_service import (
    EagleBackend, EagleOperation, EagleRequest, execute_eagle_request,
)
from danbooru_multi_search.core.results import ValueResult
from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.integrations.eagle.contracts import (
    EagleConnectionInfo, EagleFolderInfo, EagleImportResult, EagleItemInfo,
    EagleItemPage, EagleLibraryInfo,
)
from danbooru_multi_search.storage.database import Database, DatabaseError
from danbooru_multi_search.storage.eagle_imports import EagleImportRecord, EagleImportRepository


def _png() -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x01\x02\x03"))
        + chunk(b"IEND", b"")
    )


@dataclass
class _Server:
    current_library: str = "library-A"

    def __post_init__(self):
        self.libraries = {}
        self.writes = []
        self.folders = (EagleFolderInfo("folder-A", "Selected"),)

    @property
    def items(self):
        return self.libraries.setdefault(self.current_library, {})


class _Bridge:
    def __init__(self, server):
        self.server = server
        self.sent = False
        self.search_hook = None
        self.after_write = None
        self.timeout = False
        self.create_before_timeout = True

    def test_connection(self):
        return ValueResult(True, EagleConnectionInfo("4.0.0", build_version="20260401"))

    def get_current_library(self):
        key = self.server.current_library
        return ValueResult(True, EagleLibraryInfo(key, key, Path.cwd()))

    def list_folders(self, **kwargs):
        assert kwargs["maximum_pages"] == 20 and kwargs["maximum_folders"] == 20000
        return ValueResult(True, self.server.folders)

    def get_items(self, *, item_identifier=None, url=None, annotation=None, offset=0, limit=100):
        if self.search_hook is not None:
            result = self.search_hook(item_identifier, url, annotation, offset, limit)
            if result is not None:
                return result
        items = tuple(self.server.items.values())
        if item_identifier is not None:
            items = tuple(item for item in items if item.identifier == item_identifier)
        if url is not None:
            items = tuple(item for item in items if url in item.url)
        if annotation is not None:
            items = tuple(item for item in items if annotation in item.annotation)
        return ValueResult(True, EagleItemPage(items[offset:offset + limit], len(items), offset, limit))

    def add_item_from_file(self, path, *, folder_identifier=None, metadata=None):
        self.sent = True
        self.server.writes.append(("add", path, folder_identifier, metadata))
        identifier = f"item-{len(self.server.writes)}"
        if not self.timeout or self.create_before_timeout:
            self.server.items[identifier] = EagleItemInfo(
                identifier, metadata["name"], metadata["website"],
                tuple(metadata["tags"]), (folder_identifier,) if folder_identifier else (),
                metadata["annotation"], False,
            )
        if self.after_write:
            self.after_write()
        if self.timeout:
            return ValueResult.unavailable("eagle_timeout", "Response was lost after sending.")
        return ValueResult(True, EagleImportResult(identifier))

    def update_item_metadata(self, identifier, *, tags, annotation):
        self.sent = True
        self.server.writes.append(("update", identifier, {"id": identifier, "tags": tags, "annotation": annotation}))
        item = replace(self.server.items[identifier], tags=tags, annotation=annotation)
        self.server.items[identifier] = item
        if self.after_write:
            self.after_write()
        return ValueResult(True, item)


@pytest.fixture
def context(tmp_path):
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    path = tmp_path / "original.png"
    path.write_bytes(_png())
    post = DanbooruPost(
        321, "https://example.invalid/preview.png", None,
        "https://example.invalid/original.png", -2, 0, "g", 1, 1, "png",
        file_size=len(_png()), source="https://artist.invalid/original",
        artist_tags=("artist_a",), character_tags=("character_a",),
        copyright_tags=("series_a",), general_tags=("cat",), meta_tags=("highres",),
    )
    work = EaglePostImportRequest(
        "library-A", "folder-A", post, "https://danbooru.example/posts/321", path,
        query_text="cat AND artist_a", pool_text="7 · series",
    )
    server = _Server()
    return SimpleNamespace(
        database=database, repository=EagleImportRepository(database),
        path=path, work=work, server=server, bridge=_Bridge(server),
        digest=hashlib.sha256(_png()).hexdigest(),
    )


def run(
    context, *, work=None, precheck=False, bridge=None, repository=None,
    cancelled=lambda: False, validate_media=lambda _path, _extension: None,
):
    bridge = bridge or context.bridge
    bridge.sent = False
    backend = EagleBackend(
        bridge, lambda: bridge.sent, repository or context.repository,
        validate_media=validate_media,
    )
    return execute_eagle_request(EagleRequest(
        "one-operation", EagleOperation.PRECHECK_POST if precheck else EagleOperation.IMPORT_POST,
        post_import=work or context.work,
    ), backend, cancelled)


def test_real_repository_precheck_without_hash_is_ready_and_sends_no_write(context):
    result = run(context, work=replace(context.work, file_path=None), precheck=True)
    assert not result.error_code and result.import_result.state == "ready"
    assert context.server.writes == []


def test_import_records_actual_bytes_and_default_duplicate_survives_restart(context):
    first = run(context)
    assert first.import_result.state == "imported"
    item_id = first.import_result.item_identifier
    saved = context.repository.find_by_post("library-A", 321)
    assert saved.item_id == item_id and saved.sha256 == context.digest
    item = context.server.items[item_id]
    assert item.name == "321_artist_a" and item.folders == ("folder-A",)
    assert item.tags == ("danbooru", "artist_a", "character_a", "series_a")
    assert annotation_markers(item.annotation)[0].sha256 == context.digest
    restored = EagleImportRepository(Database(context.database.path))
    second = run(context, precheck=True, repository=restored)
    assert second.import_result.state == "skipped"
    assert second.import_result.item_identifier == item_id
    assert len(context.server.writes) == 1


def test_content_alias_can_skip_after_restart_and_update_without_losing_original_metadata(context):
    first = run(context)
    item_id = first.import_result.item_identifier
    original = context.server.items[item_id]
    other = replace(context.work,
        post=replace(context.work.post, id=322, artist_tags=("artist_b",)),
        post_url="https://danbooru.example/posts/322", query_text="", pool_text="",
    )
    second = run(context, work=other)
    assert second.import_result.state == "skipped" and second.import_result.reason == "file_hash"
    assert context.repository.find_by_post("library-A", 322).sha256 == context.digest
    restored = EagleImportRepository(Database(context.database.path))
    third = run(context, work=replace(other, file_path=None), precheck=True, repository=restored)
    assert third.import_result.state == "skipped" and third.import_result.item_identifier == item_id
    updated = run(context, work=replace(other, update_duplicates=True), repository=restored)
    assert updated.import_result.state == "updated"
    item = context.server.items[item_id]
    assert item.name == original.name and item.url == original.url and item.folders == original.folders
    assert set(original.tags) <= set(item.tags) and "artist_b" in item.tags
    assert len(annotation_markers(item.annotation)) == 2
    assert original.annotation in item.annotation


def test_update_keeps_first_import_time_query_pool_and_optional_tag_policy(context):
    first = run(context)
    item_id = first.import_result.item_identifier
    item = context.server.items[item_id]
    marker_text = item.annotation
    import json
    payload = json.loads(marker_text.split("\n", 1)[1].rsplit("\n", 1)[0])
    original_time = payload["details"]["导入时间"]
    marker_text = marker_text.replace(original_time, "2001-02-03T04:05:06+00:00")
    context.server.items[item_id] = replace(item, annotation="user note\n\n" + marker_text, tags=("user", *item.tags))
    result = run(context, work=replace(
        context.work, update_duplicates=True, query_text="", pool_text="未取得",
        include_general=True, include_meta=True, include_chinese=True,
        translations=(("cat", ("猫",)), ("artist_a", ("画师",))),
    ))
    assert result.import_result.state == "updated"
    updated = context.server.items[item_id]
    assert updated.annotation.startswith("user note\n\n")
    assert "2001-02-03T04:05:06+00:00" in updated.annotation
    assert "cat AND artist_a" in updated.annotation and "7 · series" in updated.annotation
    assert set(("user", "cat", "highres", "猫", "画师")) <= set(updated.tags)
    assert set(context.server.writes[-1][2]) == {"id", "tags", "annotation"}


def test_url_only_duplicate_update_does_not_claim_download_hash_for_existing_file(context):
    context.server.items["manual"] = EagleItemInfo(
        "manual", "user name", context.work.post_url, ("user",), ("old-folder",), "user note", False,
    )
    result = run(context, work=replace(context.work, update_duplicates=True))
    assert result.import_result.state == "updated"
    saved = context.repository.find_by_post("library-A", 321)
    assert saved.item_id == "manual" and saved.sha256 is None
    item = context.server.items["manual"]
    marker = annotation_markers(item.annotation)[0]
    assert marker.sha256 is None and marker.source_sha256 == context.digest
    assert item.name == "user name" and item.url == context.work.post_url and item.folders == ("old-folder",)


def test_claim_rechecks_after_another_repository_completed_and_released(context, monkeypatch):
    second_repository = EagleImportRepository(Database(context.database.path))
    second_bridge = _Bridge(context.server)
    original_begin = second_repository.begin_attempt

    def completed_between_lookup_and_claim(*args, **kwargs):
        first = run(context)
        assert first.import_result.state == "imported"
        return original_begin(*args, **kwargs)

    monkeypatch.setattr(second_repository, "begin_attempt", completed_between_lookup_and_claim)
    second = run(context, bridge=second_bridge, repository=second_repository)
    assert second.import_result.state == "skipped"
    assert len(context.server.writes) == 1
    assert second_repository.find_attempts("library-A", 321, context.digest) == ()


def test_conflicting_marker_hashes_cannot_be_combined_into_false_duplicate(context):
    first_work = replace(context.work, post=replace(context.work.post, id=1), post_url="https://danbooru.example/posts/1")
    second_work = replace(context.work, post=replace(context.work.post, id=2), post_url="https://danbooru.example/posts/2")
    notes = build_annotation(first_work, sha256="a" * 64, attempt_token="first") + "\n\n" + build_annotation(second_work, sha256=context.digest, attempt_token="second")
    context.server.items["conflict"] = EagleItemInfo("conflict", "name", first_work.post_url, (), (), notes, False)
    result = run(context)
    assert result.error_code == "eagle_duplicate_conflict"
    assert context.server.writes == []


@pytest.mark.parametrize("mode", ["read_error", "incomplete", "library_changed"])
def test_failed_or_incomplete_reads_and_switches_never_send_mutation(context, mode):
    def hook(_id, url, _annotation, offset, limit):
        if mode == "read_error":
            return ValueResult.unavailable("eagle_http_error", "read failed")
        if mode == "incomplete":
            return ValueResult(True, EagleItemPage((), 1, offset, limit))
        if url is not None:
            context.server.current_library = "library-B"
        return None

    context.bridge.search_hook = hook
    result = run(context)
    assert result.error_code in {"eagle_http_error", "eagle_search_incomplete", "eagle_library_changed"}
    assert context.server.writes == [] and context.path.exists()


@pytest.mark.parametrize("mode", ["html", "json", "truncated", "wrong_size"])
def test_invalid_original_file_is_rejected_before_any_mutation(context, mode):
    body = {"html": b"<html>error</html>", "json": b'{"error":true}', "truncated": _png()[:-1], "wrong_size": _png()}[mode]
    context.path.write_bytes(body)
    expected = len(body) + 1 if mode == "wrong_size" else None
    work = replace(context.work, post=replace(context.work.post, file_size=expected))
    result = run(context, work=work)
    assert result.error_code == "eagle_file_invalid"
    assert context.server.writes == [] and context.path.read_bytes() == body


@pytest.mark.parametrize("mode", ["lost_response", "database_error"])
def test_confirmed_remote_item_recovers_pending_after_restart_without_reposting(context, monkeypatch, mode):
    original_save = context.repository.save
    if mode == "lost_response":
        context.bridge.timeout = True
    else:
        def fail_save(record, *, attempt_token=None):
            raise DatabaseError("injected confirmation write failure")
        monkeypatch.setattr(context.repository, "save", fail_save)
    first = run(context)
    assert first.uncertain and first.import_result.state == "uncertain"
    assert len(context.repository.find_attempts("library-A", 321, context.digest)) == 1
    assert context.path.read_bytes() == _png() and len(context.server.writes) == 1
    context.bridge.timeout = False
    monkeypatch.setattr(context.repository, "save", original_save)
    restored = EagleImportRepository(Database(context.database.path))
    recovered = run(context, work=replace(context.work, file_path=None), precheck=True, repository=restored)
    assert recovered.import_result.state == "skipped"
    assert recovered.import_result.recovered_paths == (context.path,)
    assert restored.find_attempts("library-A", 321, None) == ()
    assert len(context.server.writes) == 1 and context.path.exists()


def test_unresolved_previous_write_reports_original_path_without_repeating(context):
    context.bridge.timeout = True
    context.bridge.create_before_timeout = False
    first = run(context)
    assert first.uncertain
    restored = EagleImportRepository(Database(context.database.path))
    result = run(context, work=replace(context.work, file_path=None), precheck=True, repository=restored)
    assert result.error_code == "eagle_unresolved_attempt" and result.uncertain
    assert not result.post_started
    assert result.import_result.file_path == context.path and result.import_result.sha256 == context.digest
    assert len(context.server.writes) == 1 and context.path.exists()


@pytest.mark.parametrize("boundary", ["validation", "claim"])
def test_cancel_before_mutation_keeps_file_and_releases_only_own_attempt(context, monkeypatch, boundary):
    cancelled = False

    def validate_media(_path, _extension):
        nonlocal cancelled
        if boundary == "validation":
            cancelled = True

    original_begin = context.repository.begin_attempt

    def begin(*args, **kwargs):
        nonlocal cancelled
        token = original_begin(*args, **kwargs)
        if boundary == "claim":
            cancelled = True
        return token

    monkeypatch.setattr(context.repository, "begin_attempt", begin)
    result = run(context, cancelled=lambda: cancelled, validate_media=validate_media)
    assert result.error_code == "eagle_cancelled" and not result.uncertain
    assert not result.post_started and context.server.writes == []
    assert context.repository.find_attempts("library-A", 321, None) == ()
    assert context.path.read_bytes() == _png()


@pytest.mark.parametrize("boundary", ["cancel", "library_changed"])
def test_post_write_change_keeps_identity_and_recovers_without_second_mutation(context, boundary):
    cancelled = False

    def after_write():
        nonlocal cancelled
        if boundary == "cancel":
            cancelled = True
        else:
            context.server.current_library = "library-B"

    context.bridge.after_write = after_write
    first = run(context, cancelled=lambda: cancelled)
    assert first.error_code == ("eagle_cancelled" if boundary == "cancel" else "eagle_library_changed")
    assert first.uncertain and first.post_started and first.created_identifier == "item-1"
    assert len(context.repository.find_attempts("library-A", 321, None)) == 1
    assert context.path.read_bytes() == _png()
    context.bridge.after_write = None
    context.server.current_library = "library-A"
    restored = EagleImportRepository(Database(context.database.path))
    recovered = run(context, work=replace(context.work, file_path=None), precheck=True, repository=restored)
    assert recovered.import_result.state == "skipped"
    assert recovered.import_result.recovered_paths == (context.path,)
    assert restored.find_attempts("library-A", 321, None) == ()
    assert len(context.server.writes) == 1


@pytest.mark.parametrize("boundary", ["folder_removed", "file_changed"])
def test_final_target_recheck_rejects_changes_before_post(context, monkeypatch, boundary):
    original_list = context.bridge.list_folders
    calls = 0

    def folders(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            if boundary == "folder_removed":
                return ValueResult(True, ())
            context.path.write_bytes(b"changed after verification")
        return original_list(**kwargs)

    monkeypatch.setattr(context.bridge, "list_folders", folders)
    result = run(context)
    assert calls == 2
    assert result.error_code == ("eagle_folder_not_found" if boundary == "folder_removed" else "eagle_file_changed")
    assert not result.uncertain and not result.post_started
    assert context.repository.find_attempts("library-A", 321, None) == ()
    assert context.server.writes == [] and context.path.exists()


@pytest.mark.parametrize("corruption", ["hash", "url"])
def test_pending_recovery_rejects_conflict_before_confirming_or_releasing(context, corruption):
    context.bridge.timeout = True
    assert run(context).uncertain
    item = context.server.items["item-1"]
    if corruption == "hash":
        other = replace(
            context.work, post=replace(context.work.post, id=322),
            post_url="https://danbooru.example/posts/322",
        )
        extra = build_annotation(other, sha256="a" * 64, attempt_token="other")
        item = replace(item, annotation=item.annotation + "\n\n" + extra)
    else:
        item = replace(item, url="https://unrelated.example/posts/321")
    context.server.items[item.identifier] = item
    context.bridge.timeout = False
    result = run(context, work=replace(context.work, file_path=None), precheck=True)
    assert result.uncertain and result.error_code == "eagle_duplicate_conflict"
    assert context.repository.find_by_post("library-A", 321) is None
    assert len(context.repository.find_attempts("library-A", 321, None)) == 1
    assert result.import_result.recovered_paths == ()
    assert len(context.server.writes) == 1 and context.path.read_bytes() == _png()


@pytest.mark.parametrize("duplicate", ["content", "url"])
def test_pending_update_recovers_content_alias_and_url_only_without_claiming_wrong_hash(context, duplicate):
    if duplicate == "content":
        item_id = run(context).import_result.item_identifier
        work = replace(
            context.work, post=replace(context.work.post, id=322),
            post_url="https://danbooru.example/posts/322", update_duplicates=True,
        )
    else:
        item_id = "manual"
        context.server.items[item_id] = EagleItemInfo(
            item_id, "user name", context.work.post_url, (), (), "user note", False,
        )
        work = replace(context.work, update_duplicates=True)
    original = context.server.items[item_id]
    cancelled = False

    def after_write():
        nonlocal cancelled
        cancelled = True

    context.bridge.after_write = after_write
    result = run(context, work=work, cancelled=lambda: cancelled)
    assert result.uncertain and result.created_identifier == item_id
    context.bridge.after_write = None
    recovered = run(
        context, work=replace(work, file_path=None, update_duplicates=False), precheck=True,
        repository=EagleImportRepository(Database(context.database.path)),
    )
    assert recovered.import_result.state == "skipped"
    assert recovered.import_result.recovered_paths == (context.path,)
    saved = context.repository.find_by_post("library-A", work.post.id)
    assert saved.item_id == item_id
    assert saved.sha256 == (context.digest if duplicate == "content" else None)
    assert context.server.items[item_id].url == original.url
    assert context.repository.find_attempts("library-A", work.post.id, None) == ()
    assert len(context.server.writes) == (2 if duplicate == "content" else 1)
