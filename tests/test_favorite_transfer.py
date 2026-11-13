from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import danbooru_multi_search.storage.favorite_transfer as transfer_module
import danbooru_multi_search.storage.favorites as favorites_module
from danbooru_multi_search.danbooru import DanbooruPost
from danbooru_multi_search.core.favorites import (
    FavoriteCandidate,
    FavoriteError,
    FavoriteTagCategory,
)
from danbooru_multi_search.storage import (
    FAVORITE_TRANSFER_DOCUMENT_TYPE,
    FAVORITE_TRANSFER_VERSION,
    FavoriteImportMode,
    FavoriteRepository,
    FavoriteTransferDocument,
    FavoriteTransferError,
    FavoriteTransferFolder,
    FavoriteTransferItem,
    FavoriteTransferService,
    decode_favorite_transfer_document,
    encode_favorite_transfer_document,
    read_favorite_transfer_document,
    write_favorite_transfer_document,
)
from danbooru_multi_search.storage.database import Database


BASE_TIME = datetime(2026, 11, 10, 1, 2, 3, 456789, tzinfo=timezone.utc)


def _post(post_id: int, *, score: int = -3) -> DanbooruPost:
    return DanbooruPost(
        id=post_id,
        preview_url=f"https://example.invalid/{post_id}/preview.jpg",
        large_file_url=f"https://example.invalid/{post_id}/large.jpg",
        file_url=f"https://example.invalid/{post_id}/original.png",
        score=score,
        fav_count=0,
        rating="q",
        width=2048,
        height=1024,
        file_ext="png",
        created_at="2026-11-10T01:02:03.456789Z",
        file_size=123456,
        source="source",
        artist_tags=("sample_artist",),
        character_tags=("sample_character",),
        copyright_tags=("sample_series",),
        general_tags=("blue_sky",),
        meta_tags=("highres",),
    )


def _database(path) -> Database:
    database = Database(path)
    assert database.initialize() == 8
    return database


def _empty_document(*, exported_at: datetime = BASE_TIME) -> FavoriteTransferDocument:
    return FavoriteTransferDocument(exported_at, (), ())


def test_optional_apply_checkpoint_runs_before_repository_mutation(tmp_path, monkeypatch):
    repository = FavoriteRepository(_database(tmp_path / "checkpoint.sqlite3"))
    service = FavoriteTransferService(repository)
    preview = service.preview_import(_empty_document(), FavoriteImportMode.REPLACE)
    writes = []
    original = repository.apply_collection_import
    def apply(**kwargs):
        writes.append(True)
        return original(**kwargs)
    monkeypatch.setattr(repository, "apply_collection_import", apply)
    class Cancelled(Exception):
        pass
    def checkpoint():
        raise Cancelled()
    with pytest.raises(Cancelled):
        service.apply_import(preview, before_apply=checkpoint)
    assert writes == []
    assert repository.capture_collection().revision == 0


def _folder(
    reference: str,
    name: str,
    *,
    created_at: datetime = BASE_TIME,
    updated_at: datetime | None = None,
) -> FavoriteTransferFolder:
    return FavoriteTransferFolder(
        reference,
        name,
        created_at,
        updated_at or created_at,
    )


def _item(
    candidate: FavoriteCandidate,
    folder_reference: str | None = None,
    *,
    favorited_at: datetime = BASE_TIME,
    updated_at: datetime | None = None,
) -> FavoriteTransferItem:
    return FavoriteTransferItem(
        candidate,
        folder_reference,
        favorited_at,
        updated_at or favorited_at,
    )


def _document_payload(document: FavoriteTransferDocument) -> dict:
    return json.loads(encode_favorite_transfer_document(document))


def _json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _business_content(document: FavoriteTransferDocument):
    return document.folders, document.favorites


def _raw_collection_state(database: Database) -> tuple[object, ...]:
    with database.connect() as connection:
        revision = connection.execute(
            "SELECT revision FROM favorite_collection_state WHERE id = 1"
        ).fetchone()[0]
        folders = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT id, name, created_at, updated_at FROM favorite_folders ORDER BY id"
            ).fetchall()
        )
        favorites = tuple(
            tuple(row)
            for row in connection.execute(
                """SELECT id, object_type, object_key, folder_id,
                          favorited_at, updated_at
                   FROM favorite_items ORDER BY id"""
            ).fetchall()
        )
    return revision, folders, favorites


def _valid_payload() -> dict:
    return _document_payload(
        FavoriteTransferDocument(
            BASE_TIME,
            (_folder("folder-000001", "Folder"),),
            (
                _item(
                    FavoriteCandidate.for_post(_post(70)),
                    "folder-000001",
                ),
            ),
        )
    )


def test_public_transfer_api_is_exported():
    import danbooru_multi_search.storage as storage

    names = {
        "FAVORITE_TRANSFER_DOCUMENT_TYPE",
        "FAVORITE_TRANSFER_VERSION",
        "MAX_FAVORITE_TRANSFER_BYTES",
        "FavoriteImportMode",
        "FavoriteImportPreview",
        "FavoriteTransferDocument",
        "FavoriteTransferError",
        "FavoriteTransferFolder",
        "FavoriteTransferItem",
        "FavoriteTransferService",
        "decode_favorite_transfer_document",
        "encode_favorite_transfer_document",
        "read_favorite_transfer_document",
        "write_favorite_transfer_document",
    }

    assert names <= set(storage.__all__)
    assert all(hasattr(storage, name) for name in names)


def test_six_types_export_parse_import_and_reexport_business_content(tmp_path):
    source_database = _database(tmp_path / "source.sqlite3")
    source = FavoriteRepository(source_database, clock=lambda: BASE_TIME)
    chinese = source.create_folder("中文 文件夹", created_at=BASE_TIME)
    spaced = source.create_folder(
        "Space Folder", created_at=BASE_TIME + timedelta(seconds=1)
    )
    candidates = (
        (FavoriteCandidate.for_post(_post(5), display_name="作品 5"), chinese.id),
        (FavoriteCandidate.for_tag("artist_tag", FavoriteTagCategory.ARTIST), spaced.id),
        (FavoriteCandidate.for_tag("character_tag", FavoriteTagCategory.CHARACTER), None),
        (FavoriteCandidate.for_tag("copyright_tag", FavoriteTagCategory.COPYRIGHT), chinese.id),
        (FavoriteCandidate.for_tag("general_tag", FavoriteTagCategory.GENERAL), None),
        (FavoriteCandidate.for_pool(99, "系列作品池"), spaced.id),
    )
    for index, (candidate, folder_id) in enumerate(candidates):
        source.add_favorite(
            candidate,
            folder_id=folder_id,
            favorited_at=BASE_TIME + timedelta(minutes=index),
        )
    source_service = FavoriteTransferService(source, clock=lambda: BASE_TIME)

    exported = source_service.create_export_document()
    encoded = encode_favorite_transfer_document(exported)
    decoded = decode_favorite_transfer_document(encoded)
    target_database = _database(tmp_path / "target.sqlite3")
    target = FavoriteRepository(target_database, clock=lambda: BASE_TIME)
    target_service = FavoriteTransferService(
        target,
        clock=lambda: BASE_TIME + timedelta(days=1),
    )
    target_service.apply_import(
        target_service.preview_import(decoded, FavoriteImportMode.REPLACE)
    )
    reexported = target_service.create_export_document()

    assert encoded.startswith(b'{"document_type"')
    assert not encoded.startswith(b"\xef\xbb\xbf")
    assert decoded == exported
    assert _business_content(reexported) == _business_content(exported)
    assert [folder.name for folder in exported.folders] == [
        "Space Folder",
        "中文 文件夹",
    ]
    assert [
        (item.candidate.identity.object_type.value, item.candidate.identity.object_key)
        for item in exported.favorites
    ] == sorted(
        (
            item.candidate.identity.object_type.value,
            item.candidate.identity.object_key,
        )
        for item in exported.favorites
    )
    assert any(item.folder_reference is None for item in exported.favorites)
    payload = json.loads(encoded)
    assert set(payload) == {
        "document_type",
        "exported_at",
        "favorites",
        "folders",
        "format_version",
    }
    assert all("id" not in folder for folder in payload["folders"])
    assert all("id" not in item for item in payload["favorites"])


def test_file_export_is_stable_and_round_trips(tmp_path):
    document = FavoriteTransferDocument(
        BASE_TIME,
        (_folder("folder-000001", "中文 文件夹"),),
        (_item(FavoriteCandidate.for_pool(7, "Pool"), "folder-000001"),),
    )
    destination = tmp_path / "收藏 export.json"

    write_favorite_transfer_document(destination, document)

    assert destination.read_bytes() == encode_favorite_transfer_document(document)
    assert read_favorite_transfer_document(destination) == document
    assert list(tmp_path.glob(f".{destination.name}.*.tmp")) == []


def test_merge_preview_reuses_adds_and_preserves_existing_favorite(tmp_path):
    database = _database(tmp_path / "merge.sqlite3")
    repository = FavoriteRepository(database, clock=lambda: BASE_TIME)
    reused_folder = repository.create_folder("ARTISTS")
    existing = repository.add_favorite(
        FavoriteCandidate.for_post(_post(10, score=-50), display_name="原概要"),
        folder_id=reused_folder.id,
        favorited_at=BASE_TIME,
    )
    original = repository.get_favorite(existing.id)
    document = FavoriteTransferDocument(
        BASE_TIME,
        (
            _folder("folder-000001", "artists"),
            _folder("folder-000002", "New Folder"),
        ),
        (
            _item(
                FavoriteCandidate.for_post(
                    _post(10, score=999), display_name="文档新概要"
                ),
                "folder-000002",
            ),
            _item(
                FavoriteCandidate.for_pool(11, "New Pool"),
                "folder-000002",
                favorited_at=BASE_TIME + timedelta(seconds=2),
            ),
        ),
    )
    service = FavoriteTransferService(repository)

    preview = service.preview_import(document, FavoriteImportMode.MERGE)
    applied = service.apply_import(preview)

    assert (
        preview.added_folders,
        preview.reused_folders,
        preview.added_favorites,
        preview.preserved_favorites,
        preview.deleted_folders,
        preview.deleted_favorites,
    ) == (1, 1, 1, 1, 0, 0)
    assert repository.get_favorite(existing.id) == original
    assert len(applied.folders) == 2
    assert len(applied.favorites) == 2
    assert applied.revision == preview.collection_revision + 1
    imported_pool = repository.get_by_identity(
        FavoriteCandidate.for_pool(11, "ignored").identity
    )
    new_folder = next(folder for folder in applied.folders if folder.name == "New Folder")
    assert imported_pool is not None
    assert imported_pool.folder_id == new_folder.id


def test_empty_merge_has_zero_effect_and_keeps_revision(tmp_path):
    database = _database(tmp_path / "empty-merge.sqlite3")
    repository = FavoriteRepository(database, clock=lambda: BASE_TIME)
    repository.add_favorite(FavoriteCandidate.for_pool(20, "Existing"))
    before = repository.capture_collection()
    service = FavoriteTransferService(repository)

    preview = service.preview_import(_empty_document(), FavoriteImportMode.MERGE)
    after = service.apply_import(preview)
    repeated = service.apply_import(preview)

    assert (
        preview.added_folders,
        preview.reused_folders,
        preview.added_favorites,
        preview.preserved_favorites,
        preview.deleted_folders,
        preview.deleted_favorites,
    ) == (0, 0, 0, 0, 0, 0)
    assert after == before
    assert repeated == before


def test_merge_with_only_existing_content_keeps_revision_and_metadata(tmp_path):
    repository = FavoriteRepository(
        _database(tmp_path / "existing-merge.sqlite3"),
        clock=lambda: BASE_TIME,
    )
    folder = repository.create_folder("Existing")
    original = repository.add_favorite(
        FavoriteCandidate.for_pool(21, "Original"),
        folder_id=folder.id,
    )
    service = FavoriteTransferService(repository, clock=lambda: BASE_TIME)
    document = FavoriteTransferDocument(
        BASE_TIME,
        (_folder("folder-000001", "EXISTING"),),
        (
            _item(
                FavoriteCandidate.for_pool(21, "Changed"),
                "folder-000001",
                favorited_at=BASE_TIME + timedelta(days=1),
            ),
        ),
    )
    before = repository.capture_collection()

    preview = service.preview_import(document, FavoriteImportMode.MERGE)
    after = service.apply_import(preview)

    assert (preview.reused_folders, preview.preserved_favorites) == (1, 1)
    assert after.revision == before.revision
    assert repository.get_favorite(original.id) == original


def test_replace_preview_reports_physical_replacement_and_empty_can_clear(tmp_path):
    database = _database(tmp_path / "replace.sqlite3")
    repository = FavoriteRepository(database, clock=lambda: BASE_TIME)
    first_folder = repository.create_folder("First")
    second_folder = repository.create_folder("Second")
    repository.add_favorite(
        FavoriteCandidate.for_pool(30, "Old Pool"), folder_id=first_folder.id
    )
    repository.add_favorite(
        FavoriteCandidate.for_tag("old_tag", FavoriteTagCategory.GENERAL),
        folder_id=second_folder.id,
    )
    document = FavoriteTransferDocument(
        BASE_TIME,
        (_folder("folder-000001", "Replacement"),),
        (
            _item(
                FavoriteCandidate.for_post(_post(31)),
                "folder-000001",
            ),
        ),
    )
    service = FavoriteTransferService(repository)

    preview = service.preview_import(document, FavoriteImportMode.REPLACE)
    replaced = service.apply_import(preview)

    assert (
        preview.added_folders,
        preview.reused_folders,
        preview.added_favorites,
        preview.preserved_favorites,
        preview.deleted_folders,
        preview.deleted_favorites,
    ) == (1, 0, 1, 0, 2, 2)
    assert [folder.name for folder in replaced.folders] == ["Replacement"]
    assert [item.identity.object_key for item in replaced.favorites] == ["31"]
    assert replaced.revision == preview.collection_revision + 1

    empty_preview = service.preview_import(
        _empty_document(exported_at=BASE_TIME + timedelta(seconds=1)),
        FavoriteImportMode.REPLACE,
    )
    cleared = service.apply_import(empty_preview)

    assert (empty_preview.deleted_folders, empty_preview.deleted_favorites) == (1, 1)
    assert cleared.folders == ()
    assert cleared.favorites == ()
    assert cleared.revision == empty_preview.collection_revision + 1


def test_preview_revision_stays_stale_after_other_repository_restores_data(tmp_path):
    database = _database(tmp_path / "stale.sqlite3")
    repository = FavoriteRepository(database, clock=lambda: BASE_TIME)
    other = FavoriteRepository(database, clock=lambda: BASE_TIME)
    service = FavoriteTransferService(repository)
    preview = service.preview_import(_empty_document(), FavoriteImportMode.MERGE)
    transient = other.add_favorite(FavoriteCandidate.for_pool(40, "Transient"))
    assert other.delete_favorite(transient.id)
    restored = repository.capture_collection()

    with pytest.raises(FavoriteTransferError) as error:
        service.apply_import(preview)

    assert error.value.code == "stale_preview"
    assert restored.folders == ()
    assert restored.favorites == ()
    assert restored.revision > preview.collection_revision


def test_import_does_not_change_cache_or_download_sentinels(tmp_path):
    database = _database(tmp_path / "sentinels.sqlite3")
    repository = FavoriteRepository(database, clock=lambda: BASE_TIME)
    cache = tmp_path / "cache.sentinel"
    download = tmp_path / "download.sentinel"
    cache.write_bytes(b"cache")
    download.write_bytes(b"download")
    service = FavoriteTransferService(repository)
    document = FavoriteTransferDocument(
        BASE_TIME,
        (),
        (_item(FavoriteCandidate.for_pool(41, "Pool")),),
    )

    service.apply_import(service.preview_import(document, FavoriteImportMode.MERGE))

    assert cache.read_bytes() == b"cache"
    assert download.read_bytes() == b"download"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        ("document_type", "unknown", "unknown_document_type"),
        ("format_version", 2, "unknown_format_version"),
        ("format_version", True, "unknown_format_version"),
    ),
)
def test_decode_rejects_unknown_type_or_version(field, value, code):
    payload = _valid_payload()
    payload[field] = value

    with pytest.raises(FavoriteTransferError) as error:
        decode_favorite_transfer_document(_json_bytes(payload))

    assert error.value.code == code


def test_decode_rejects_duplicate_unknown_and_missing_fields():
    valid = encode_favorite_transfer_document(_empty_document())
    duplicate = valid[:-1] + b',"format_version":1}'
    unknown_payload = _valid_payload()
    unknown_payload["unknown"] = 1
    unknown_nested = _valid_payload()
    unknown_nested["folders"][0]["unknown"] = 1
    missing = _valid_payload()
    del missing["exported_at"]

    for data, code in (
        (duplicate, "duplicate_field"),
        (_json_bytes(unknown_payload), "invalid_fields"),
        (_json_bytes(unknown_nested), "invalid_fields"),
        (_json_bytes(missing), "invalid_fields"),
    ):
        with pytest.raises(FavoriteTransferError) as error:
            decode_favorite_transfer_document(data)
        assert error.value.code == code


def test_decode_rejects_duplicate_folder_names_identities_and_references():
    duplicate_folder = _valid_payload()
    duplicate_folder["folders"].append(
        {
            **duplicate_folder["folders"][0],
            "name": "FOLDER",
            "reference": "folder-000002",
        }
    )
    duplicate_identity = _valid_payload()
    duplicate_identity["favorites"].append(dict(duplicate_identity["favorites"][0]))
    duplicate_reference = _valid_payload()
    duplicate_reference["folders"].append(
        {
            **duplicate_reference["folders"][0],
            "name": "Other",
        }
    )

    for data, code in (
        (_json_bytes(duplicate_folder), "duplicate_folder"),
        (_json_bytes(duplicate_identity), "duplicate_favorite"),
        (_json_bytes(duplicate_reference), "duplicate_folder_reference"),
    ):
        with pytest.raises(FavoriteTransferError) as error:
            decode_favorite_transfer_document(data)
        assert error.value.code == code


def test_decode_rejects_missing_or_malformed_folder_reference():
    missing = _valid_payload()
    missing["favorites"][0]["folder_reference"] = "folder-999999"
    malformed = _valid_payload()
    malformed["folders"][0]["reference"] = "local-database-1"

    for payload in (missing, malformed):
        with pytest.raises(FavoriteTransferError) as error:
            decode_favorite_transfer_document(_json_bytes(payload))
        assert error.value.code == "invalid_folder_reference"


def test_decode_rejects_invalid_time_and_time_order():
    invalid = _valid_payload()
    invalid["exported_at"] = "2026-11-10"
    folder_order = _valid_payload()
    folder_order["folders"][0]["updated_at"] = "2026-11-09T01:02:03.456789Z"
    favorite_order = _valid_payload()
    favorite_order["favorites"][0]["updated_at"] = (
        "2026-11-09T01:02:03.456789Z"
    )

    for payload, code in (
        (invalid, "corrupt_timestamp"),
        (folder_order, "invalid_time_order"),
        (favorite_order, "invalid_time_order"),
    ):
        with pytest.raises(FavoriteTransferError) as error:
            decode_favorite_transfer_document(_json_bytes(payload))
        assert error.value.code == code


def test_decode_rejects_invalid_identity_and_corrupt_summary():
    invalid_identity = _valid_payload()
    invalid_identity["favorites"][0]["object_key"] = "01"
    corrupt_summary = _valid_payload()
    corrupt_summary["favorites"][0]["summary_json"] = "{}"
    mismatched_summary = _valid_payload()
    mismatched_summary["favorites"][0]["object_key"] = "71"

    for payload in (invalid_identity, corrupt_summary, mismatched_summary):
        with pytest.raises(FavoriteTransferError):
            decode_favorite_transfer_document(_json_bytes(payload))


@pytest.mark.parametrize("constant", (b"NaN", b"Infinity", b"-Infinity"))
def test_decode_rejects_nonstandard_constants_even_in_unknown_data(constant):
    valid = encode_favorite_transfer_document(_empty_document())
    data = valid[:-1] + b',"ignored":' + constant + b"}"

    with pytest.raises(FavoriteTransferError) as error:
        decode_favorite_transfer_document(data)

    assert error.value.code == "invalid_json"


def test_decode_rejects_non_utf8_bom_size_depth_and_huge_integer(monkeypatch):
    valid = encode_favorite_transfer_document(_empty_document())
    cases = (
        (b"\xff", "invalid_utf8"),
        (b"\xef\xbb\xbf" + valid, "utf8_bom_not_allowed"),
        (b"[" * 2000 + b"0" + b"]" * 2000, "invalid_json"),
        (
            valid.replace(b'"format_version":1', b'"format_version":' + b"9" * 5000),
            "invalid_json",
        ),
    )
    for data, code in cases:
        with pytest.raises(FavoriteTransferError) as error:
            decode_favorite_transfer_document(data)
        assert error.value.code == code

    monkeypatch.setattr(transfer_module, "MAX_FAVORITE_TRANSFER_BYTES", 8)
    with pytest.raises(FavoriteTransferError) as error:
        decode_favorite_transfer_document(valid)
    assert error.value.code == "document_too_large"


def test_encode_converts_memory_error_during_payload_construction(monkeypatch):
    def fail(_value):
        raise MemoryError("injected allocation failure")

    monkeypatch.setattr(transfer_module, "_encode_time", fail)

    with pytest.raises(FavoriteTransferError) as error:
        encode_favorite_transfer_document(_empty_document())

    assert error.value.code == "document_encode_error"


def test_decode_converts_memory_error_while_freezing_folders(monkeypatch):
    def fail(_values):
        raise MemoryError("injected allocation failure")

    monkeypatch.setattr(transfer_module, "_freeze_tuple", fail)

    with pytest.raises(FavoriteTransferError) as error:
        decode_favorite_transfer_document(
            encode_favorite_transfer_document(_empty_document())
        )

    assert error.value.code == "document_too_complex"


def test_decode_converts_memory_error_while_freezing_favorites(monkeypatch):
    original_freeze = transfer_module._freeze_tuple
    calls = 0

    def fail_second(values):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise MemoryError("injected allocation failure")
        return original_freeze(values)

    monkeypatch.setattr(transfer_module, "_freeze_tuple", fail_second)

    with pytest.raises(FavoriteTransferError) as error:
        decode_favorite_transfer_document(
            encode_favorite_transfer_document(_empty_document())
        )

    assert calls == 2
    assert error.value.code == "document_too_complex"


def test_read_rejects_file_larger_than_limit_before_decode(tmp_path, monkeypatch):
    source = tmp_path / "large.json"
    source.write_bytes(b"123456789")
    monkeypatch.setattr(transfer_module, "MAX_FAVORITE_TRANSFER_BYTES", 8)

    with pytest.raises(FavoriteTransferError) as error:
        read_favorite_transfer_document(source)

    assert error.value.code == "document_too_large"


@pytest.mark.parametrize(
    ("operation", "code"),
    (
        ("write", "file_write_error"),
        ("fsync", "file_sync_error"),
        ("replace", "file_replace_error"),
    ),
)
def test_atomic_export_failure_preserves_target_and_removes_temporary_file(
    tmp_path,
    monkeypatch,
    operation,
    code,
):
    target = tmp_path / "favorites.json"
    target.write_bytes(b"original")
    document = FavoriteTransferDocument(
        BASE_TIME,
        (),
        (_item(FavoriteCandidate.for_pool(80, "Pool")),),
    )

    def fail(*_args, **_kwargs):
        raise OSError("injected failure")

    monkeypatch.setattr(transfer_module.os, operation, fail)

    with pytest.raises(FavoriteTransferError) as error:
        write_favorite_transfer_document(target, document)

    assert error.value.code == code
    assert target.read_bytes() == b"original"
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


def test_atomic_export_single_close_failure_retries_cleanup(tmp_path, monkeypatch):
    target = tmp_path / "favorites.json"
    target.write_bytes(b"original")
    real_close = transfer_module.os.close
    calls = 0

    def fail_once(descriptor):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected close failure")
        return real_close(descriptor)

    monkeypatch.setattr(transfer_module.os, "close", fail_once)

    with pytest.raises(FavoriteTransferError) as error:
        write_favorite_transfer_document(target, _empty_document())

    assert error.value.code == "file_close_error"
    assert calls == 2
    assert error.value.residual_path is None
    assert target.read_bytes() == b"original"
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


def test_atomic_export_persistent_close_failure_reports_residual_path(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "favorites.json"
    target.write_bytes(b"original")
    real_close = transfer_module.os.close
    descriptors: list[int] = []

    def fail(descriptor):
        descriptors.append(descriptor)
        raise OSError("injected persistent close failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(transfer_module.os, "close", fail)
        with pytest.raises(FavoriteTransferError) as error:
            write_favorite_transfer_document(target, _empty_document())

    residual = error.value.residual_path
    assert error.value.code == "file_cleanup_error"
    assert residual is not None
    assert str(residual) in str(error.value)
    assert residual.is_file()
    assert target.read_bytes() == b"original"
    assert len(descriptors) == 3

    real_close(descriptors[-1])
    residual.unlink()


def test_atomic_export_delete_failure_reports_residual_path(tmp_path, monkeypatch):
    target = tmp_path / "favorites.json"
    target.write_bytes(b"original")
    real_unlink = Path.unlink

    def fail_replace(*_args, **_kwargs):
        raise OSError("injected replace failure")

    def fail_temporary_unlink(path, *args, **kwargs):
        if path.name.startswith(f".{target.name}."):
            raise OSError("injected delete failure")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(transfer_module.os, "replace", fail_replace)
        scoped.setattr(Path, "unlink", fail_temporary_unlink)
        with pytest.raises(FavoriteTransferError) as error:
            write_favorite_transfer_document(target, _empty_document())

    residual = error.value.residual_path
    assert error.value.code == "file_cleanup_error"
    assert residual is not None
    assert str(residual) in str(error.value)
    assert residual.is_file()
    assert target.read_bytes() == b"original"

    residual.unlink()


def test_oversized_export_fails_before_replacing_target(tmp_path, monkeypatch):
    target = tmp_path / "favorites.json"
    target.write_bytes(b"original")
    document = FavoriteTransferDocument(
        BASE_TIME,
        (),
        (_item(FavoriteCandidate.for_pool(81, "Pool")),),
    )
    monkeypatch.setattr(transfer_module, "MAX_FAVORITE_TRANSFER_BYTES", 8)

    with pytest.raises(FavoriteTransferError) as error:
        write_favorite_transfer_document(target, document)

    assert error.value.code == "document_too_large"
    assert target.read_bytes() == b"original"
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


def test_corrupt_local_collection_cannot_be_exported_previewed_or_replaced(tmp_path):
    database = _database(tmp_path / "corrupt.sqlite3")
    repository = FavoriteRepository(database, clock=lambda: BASE_TIME)
    service = FavoriteTransferService(repository, clock=lambda: BASE_TIME)
    item = repository.add_favorite(FavoriteCandidate.for_post(_post(82)))
    preview = service.preview_import(_empty_document(), FavoriteImportMode.REPLACE)
    with database.connect() as connection:
        with connection:
            connection.execute(
                "UPDATE favorite_items SET summary_json = '{}' WHERE id = ?",
                (item.id,),
            )

    operations = (
        service.create_export_document,
        lambda: service.preview_import(_empty_document(), FavoriteImportMode.REPLACE),
        lambda: service.apply_import(preview),
    )
    for operation in operations:
        with pytest.raises(FavoriteTransferError):
            operation()

    with database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM favorite_items").fetchone()[0]
    assert count == 1


@pytest.mark.parametrize("damaged_record", ("folder", "favorite"))
def test_early_local_updated_time_blocks_export_and_replace_preview(
    tmp_path,
    damaged_record,
):
    database = _database(tmp_path / f"early-before-preview-{damaged_record}.sqlite3")
    repository = FavoriteRepository(database, clock=lambda: BASE_TIME)
    service = FavoriteTransferService(repository, clock=lambda: BASE_TIME)
    folder = repository.create_folder("Folder", created_at=BASE_TIME)
    item = repository.add_favorite(
        FavoriteCandidate.for_pool(90, "Pool"),
        folder_id=folder.id,
        favorited_at=BASE_TIME,
    )
    with database.connect() as connection:
        with connection:
            if damaged_record == "folder":
                connection.execute(
                    "UPDATE favorite_folders SET updated_at = ? WHERE id = ?",
                    ("2026-11-09T01:02:03.456789Z", folder.id),
                )
            else:
                connection.execute(
                    "UPDATE favorite_items SET updated_at = ? WHERE id = ?",
                    ("2026-11-09T01:02:03.456789Z", item.id),
                )
    damaged_state = _raw_collection_state(database)

    for operation in (
        service.create_export_document,
        lambda: service.preview_import(_empty_document(), FavoriteImportMode.REPLACE),
    ):
        with pytest.raises(FavoriteTransferError) as error:
            operation()
        assert error.value.code == "invalid_time_order"
        assert _raw_collection_state(database) == damaged_state


@pytest.mark.parametrize("damaged_record", ("folder", "favorite"))
def test_early_local_updated_time_after_preview_blocks_replace_apply(
    tmp_path,
    damaged_record,
):
    database = _database(tmp_path / f"early-after-preview-{damaged_record}.sqlite3")
    repository = FavoriteRepository(database, clock=lambda: BASE_TIME)
    service = FavoriteTransferService(repository, clock=lambda: BASE_TIME)
    folder = repository.create_folder("Folder", created_at=BASE_TIME)
    item = repository.add_favorite(
        FavoriteCandidate.for_pool(91, "Pool"),
        folder_id=folder.id,
        favorited_at=BASE_TIME,
    )
    preview = service.preview_import(_empty_document(), FavoriteImportMode.REPLACE)
    with database.connect() as connection:
        with connection:
            if damaged_record == "folder":
                connection.execute(
                    "UPDATE favorite_folders SET updated_at = ? WHERE id = ?",
                    ("2026-11-09T01:02:03.456789Z", folder.id),
                )
            else:
                connection.execute(
                    "UPDATE favorite_items SET updated_at = ? WHERE id = ?",
                    ("2026-11-09T01:02:03.456789Z", item.id),
                )
    damaged_state = _raw_collection_state(database)

    with pytest.raises(FavoriteTransferError) as error:
        service.apply_import(preview)

    assert error.value.code == "invalid_time_order"
    assert _raw_collection_state(database) == damaged_state


def test_import_failure_rolls_back_items_folders_and_revision(tmp_path, monkeypatch):
    database = _database(tmp_path / "rollback.sqlite3")
    repository = FavoriteRepository(database, clock=lambda: BASE_TIME)
    service = FavoriteTransferService(repository)
    document = FavoriteTransferDocument(
        BASE_TIME,
        (_folder("folder-000001", "Imported"),),
        (
            _item(FavoriteCandidate.for_pool(83, "First"), "folder-000001"),
            _item(FavoriteCandidate.for_pool(84, "Second"), "folder-000001"),
        ),
    )
    preview = service.preview_import(document, FavoriteImportMode.MERGE)
    original_insert = favorites_module._insert_favorite
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise FavoriteError("injected_failure", "injected")
        return original_insert(*args, **kwargs)

    monkeypatch.setattr(favorites_module, "_insert_favorite", fail_second)

    with pytest.raises(FavoriteTransferError) as error:
        service.apply_import(preview)

    snapshot = repository.capture_collection()
    assert error.value.code == "injected_failure"
    assert snapshot.revision == preview.collection_revision
    assert snapshot.folders == ()
    assert snapshot.favorites == ()


def test_preview_binding_rejects_changed_mode_document_or_statistics(tmp_path):
    repository = FavoriteRepository(_database(tmp_path / "binding.sqlite3"))
    service = FavoriteTransferService(repository)
    document = FavoriteTransferDocument(
        BASE_TIME,
        (),
        (_item(FavoriteCandidate.for_pool(85, "Pool")),),
    )
    preview = service.preview_import(document, FavoriteImportMode.MERGE)
    changed_document = FavoriteTransferDocument(
        BASE_TIME,
        (),
        (_item(FavoriteCandidate.for_pool(86, "Other")),),
    )

    for changed in (
        replace(preview, mode=FavoriteImportMode.REPLACE),
        replace(preview, document=changed_document),
        replace(preview, added_favorites=preview.added_favorites + 1),
    ):
        with pytest.raises(FavoriteTransferError) as error:
            service.apply_import(changed)
        assert error.value.code == "invalid_preview"
    assert repository.capture_collection().favorites == ()


def test_complete_snapshot_and_export_are_not_limited_to_one_thousand(tmp_path):
    repository = FavoriteRepository(_database(tmp_path / "many.sqlite3"))
    service = FavoriteTransferService(repository, clock=lambda: BASE_TIME)
    document = FavoriteTransferDocument(
        BASE_TIME,
        (),
        tuple(
            _item(FavoriteCandidate.for_pool(pool_id, f"Pool {pool_id}"))
            for pool_id in range(1, 1002)
        ),
    )

    applied = service.apply_import(
        service.preview_import(document, FavoriteImportMode.MERGE)
    )
    exported = service.create_export_document()

    assert len(applied.favorites) == 1001
    assert len(exported.favorites) == 1001
    assert applied.revision == 1
