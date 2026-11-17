from __future__ import annotations

import sys

if sys.argv[-2:-1] == ["--without-qt"]:
    import builtins
    from pathlib import Path
    original_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.split(".")[0] == "PySide6":
            raise AssertionError(name)
        return original_import(name, *args, **kwargs)
    builtins.__import__ = guarded
    from danbooru_multi_search.storage.database import Database
    from danbooru_multi_search.storage.cache_protection import CacheProtectionRepository
    database = Database(Path(sys.argv[-1]))
    database.initialize()
    repository = CacheProtectionRepository(database)
    repository.remember_redirect("https://example.invalid/a", "https://cdn.invalid/b")
    assert repository.load_snapshot().aliases == (("https://example.invalid/a", "https://cdn.invalid/b"),)
    sys.exit(0)

from dataclasses import replace

import pytest

from danbooru_multi_search.core.favorites import FavoriteCandidate
from danbooru_multi_search.storage.cache_protection import CacheProtectionError, CacheProtectionRepository
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.favorites import FavoriteRepository
from tests.test_favorite_controller import post, controller_for


def build(tmp_path):
    database = Database(tmp_path / "application.sqlite3")
    database.initialize()
    return database, FavoriteRepository(database), CacheProtectionRepository(database)


def test_saved_post_urls_and_multiple_redirects_survive_restart(tmp_path):
    database, favorites, protection = build(tmp_path)
    value = replace(post(42), preview_url="https://example.invalid/original.jpg", file_url="https://example.invalid/full.jpg")
    favorite = favorites.add_favorite(FavoriteCandidate.for_post(value))
    protection.remember_redirect(value.preview_url, "https://cdn.invalid/a.jpg")
    protection.remember_redirect(value.preview_url, "https://cdn.invalid/b.jpg")
    before = protection.load_snapshot()
    assert before.urls == favorites.image_cache_urls()
    assert set(before.aliases) == {(value.preview_url, "https://cdn.invalid/a.jpg"), (value.preview_url, "https://cdn.invalid/b.jpg")}
    assert CacheProtectionRepository(Database(database.path)).load_snapshot() == before
    favorites.delete_favorite(favorite.id)
    assert protection.load_snapshot().urls == frozenset()


def test_locked_snapshot_blocks_other_writer_without_waiting(tmp_path):
    database, favorites, protection = build(tmp_path)
    with protection.locked_snapshot() as snapshot:
        assert snapshot.urls == frozenset()
        with pytest.raises(CacheProtectionError, match="locked"):
            protection.remember_redirect("https://example.invalid/a", "https://cdn.invalid/b")


def test_damaged_post_or_alias_refuses_to_guess_protection(tmp_path):
    database, favorites, protection = build(tmp_path)
    favorite = favorites.add_favorite(FavoriteCandidate.for_post(post(42)))
    with database.connect() as connection:
        connection.execute("UPDATE favorite_items SET summary_json = '{}' WHERE id = ?", (favorite.id,))
    with pytest.raises(CacheProtectionError):
        protection.load_snapshot()


def test_missing_favorite_revision_has_a_defined_protection_error(tmp_path):
    database, favorites, protection = build(tmp_path)
    with database.connect() as connection:
        connection.execute("DELETE FROM favorite_collection_state")
    with pytest.raises(CacheProtectionError, match="缺失"):
        protection.load_snapshot()


def test_unchanged_collection_revision_reuses_decoded_urls(tmp_path, monkeypatch):
    database, favorites, protection = build(tmp_path)
    favorites.add_favorite(FavoriteCandidate.for_post(post(42)))
    previous = protection.load_snapshot()
    calls = []
    original = protection._favorites.image_cache_urls
    def urls(**kwargs):
        calls.append(True)
        return original(**kwargs)
    monkeypatch.setattr(protection._favorites, "image_cache_urls", urls)
    for _ in range(3):
        with protection.locked_snapshot(previous) as current:
            assert current.urls is previous.urls
    assert calls == []
    favorites.add_favorite(FavoriteCandidate.for_post(post(43)))
    with protection.locked_snapshot(previous) as current:
        assert current.favorite_revision > previous.favorite_revision
    assert calls == [True]


def test_empty_cache_initialization_keeps_protection_read_failure_visible(tmp_path):
    from danbooru_multi_search.ui.image_cache import ImageCacheController, MINIMUM_IMAGE_CACHE_BYTES
    from danbooru_multi_search.ui.cache_protection import CacheProtectionController
    database, favorites, repository = build(tmp_path)
    with database.connect() as connection:
        connection.execute("DELETE FROM favorite_collection_state")
    images = ImageCacheController(tmp_path / "images")
    shared = controller_for(favorites)
    protection = CacheProtectionController(repository, shared, images, access=shared._writes.access)
    try:
        assert images._ensure_cache() is not None
        assert "缺失" in images.statusText and not protection.can_store()
        assert images.setMaximumBytes(2 * MINIMUM_IMAGE_CACHE_BYTES)
        assert "缺失" in images.statusText
        assert not images.clearCache() and "缺失" in images.statusText
    finally:
        images.shutdown()
        protection.shutdown()


def test_initialization_and_shrink_keep_real_protected_overflow_message(tmp_path):
    from danbooru_multi_search.ui.image_cache import ImageCacheController, MINIMUM_IMAGE_CACHE_BYTES, cache_key
    from danbooru_multi_search.ui.protected_image_cache import ProtectedImageDiskCache
    from danbooru_multi_search.ui.cache_protection import CacheProtectionController
    from tests.test_protected_image_cache import insert, make_cache
    database, favorites, repository = build(tmp_path)
    url = "https://example.invalid/saved.jpg"
    favorites.add_favorite(FavoriteCandidate.for_post(replace(post(42), preview_url=url, file_url=None)))
    initial, _messages = make_cache(tmp_path, {url})
    insert(initial, url)
    class TinyCache(ProtectedImageDiskCache):
        def setMaximumCacheSize(self, value):
            super().setMaximumCacheSize(1000 if value > MINIMUM_IMAGE_CACHE_BYTES else 500)
    images = ImageCacheController(tmp_path / "images", maximum_bytes=2 * MINIMUM_IMAGE_CACHE_BYTES, disk_cache_factory=lambda parent: TinyCache(parent,
        key=cache_key, protection=images._protection_provider, status=images._set_status))
    shared = controller_for(favorites)
    protection = CacheProtectionController(repository, shared, images, access=shared._writes.access)
    try:
        assert images._ensure_cache() is not None
        assert "超过容量上限" in images.statusText
        assert images.setMaximumBytes(MINIMUM_IMAGE_CACHE_BYTES)
        assert "超过容量上限" in images.statusText
    finally:
        images.shutdown()
        protection.shutdown()


def test_local_redirect_is_protected_before_qt_prepare_and_survives_restart(tmp_path):
    from PySide6.QtCore import QObject, QUrl
    from danbooru_multi_search.ui.image_cache import ImageCacheController
    from danbooru_multi_search.ui.cache_protection import CacheProtectionController
    from tests.test_image_cache import ImageHttpServer, _fetch, _dispose
    server = ImageHttpServer(redirect=True)
    database, favorites, repository = build(tmp_path)
    favorites.add_favorite(FavoriteCandidate.for_post(replace(post(42), preview_url=server.url, file_url=None)))
    images = ImageCacheController(tmp_path / "images")
    shared = controller_for(favorites)
    protection = CacheProtectionController(repository, shared, images, access=shared._writes.access)
    parent = QObject()
    manager = images.create_manager(parent)
    cache = images.cache
    original_prepare = cache.prepare
    observed = []
    def prepare(metadata):
        if metadata.url() == QUrl(server.final_url):
            assert (server.url, server.final_url) in repository.load_snapshot().aliases
            observed.append(True)
        return original_prepare(metadata)
    cache.prepare = prepare
    try:
        reply, payload = _fetch(manager, server.url)
        assert payload and observed
        assert cache.metaData(QUrl(server.final_url)).isValid()
        images.shutdown()
        protection.shutdown()
        _dispose(reply, manager, parent, images, protection)
        restarted = ImageCacheController(tmp_path / "images")
        restarted_shared = controller_for(favorites)
        restarted_protection = CacheProtectionController(CacheProtectionRepository(database), restarted_shared, restarted,
            access=restarted_shared._writes.access)
        try:
            assert restarted.clearCache()
            assert restarted.cache.metaData(QUrl(server.final_url)).isValid()
            assert server.paths == ["/redirect", "/image.png"]
        finally:
            restarted.shutdown()
            restarted_protection.shutdown()
    finally:
        server.close()


def test_storage_constructs_and_persists_without_qt_in_fresh_process(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--without-qt", str(tmp_path / "pure.sqlite3")],
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_successful_state_read_does_not_reload_collection_and_cancel_or_import_release_protection(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from PySide6.QtCore import QUrl
    from danbooru_multi_search.ui.image_cache import ImageCacheController
    from danbooru_multi_search.ui.cache_protection import CacheProtectionController
    from danbooru_multi_search.ui.favorites import post_identity
    from danbooru_multi_search.storage.favorite_transfer import FavoriteTransferService, FavoriteTransferDocument, FavoriteImportMode
    from tests.test_protected_image_cache import insert
    database, favorites, repository = build(tmp_path)
    url = "https://example.invalid/image.jpg"
    value = replace(post(42), preview_url=url, file_url=None)
    shared = controller_for(favorites)
    images = ImageCacheController(tmp_path / "images")
    protection = CacheProtectionController(repository, shared, images, access=shared._writes.access)
    reads = []
    original = repository.load_snapshot
    def read():
        reads.append(True)
        return original()
    monkeypatch.setattr(repository, "load_snapshot", read)
    try:
        shared.ensure((post_identity("42"),), refresh=True)
        assert reads == []
        cache = images._ensure_cache()
        insert(cache, url)
        assert shared.set_post(value, True, guard=lambda: True)
        assert images.clearCache() and cache.metaData(QUrl(url)).isValid()
        assert shared.set_post(value, False, guard=lambda: True)
        assert images.clearCache() and not cache.metaData(QUrl(url)).isValid()
        insert(cache, url)
        assert shared.set_post(value, True, guard=lambda: True)
        service = FavoriteTransferService(favorites)
        preview = service.preview_import(FavoriteTransferDocument(datetime.now(timezone.utc), (), ()), FavoriteImportMode.REPLACE)
        assert shared._writes.access.begin_external_write("import")
        service.apply_import(preview)
        assert shared.publish_external_snapshot("import", favorites.capture_collection())
        shared._writes.access.finish_external_write("import")
        assert images.clearCache() and not cache.metaData(QUrl(url)).isValid()
    finally:
        images.shutdown()
        protection.shutdown()


def test_alias_write_failure_does_not_cache_untracked_redirect_and_can_retry_later(tmp_path):
    from PySide6.QtCore import QObject, QUrl
    from danbooru_multi_search.ui.image_cache import ImageCacheController
    from danbooru_multi_search.ui.cache_protection import CacheProtectionController
    from tests.test_image_cache import ImageHttpServer, _fetch, _dispose
    server = ImageHttpServer(redirect=True)
    database, favorites, repository = build(tmp_path)
    favorites.add_favorite(FavoriteCandidate.for_post(replace(post(42), preview_url=server.url, file_url=None)))
    with database.connect() as connection:
        connection.execute("CREATE TRIGGER reject_alias BEFORE INSERT ON image_cache_redirects BEGIN SELECT RAISE(ABORT, 'alias failure'); END")
    images = ImageCacheController(tmp_path / "images")
    shared = controller_for(favorites)
    protection = CacheProtectionController(repository, shared, images, access=shared._writes.access)
    parent = QObject()
    manager = images.create_manager(parent)
    try:
        reply, payload = _fetch(manager, server.url)
        assert payload and not images.cache.metaData(QUrl(server.final_url)).isValid()
        assert "alias failure" in images.statusText and not protection.can_store()
        with database.connect() as connection:
            connection.execute("DROP TRIGGER reject_alias")
        protection._busy_changed()
        assert protection.can_store() and (server.url, server.final_url) in repository.load_snapshot().aliases
        assert server.paths == ["/redirect", "/image.png"]
        _dispose(reply)
    finally:
        images.shutdown()
        protection.shutdown()
        _dispose(manager, parent, images, protection)
        server.close()


def test_empty_alias_flush_does_not_clear_unresolved_collection_read_error(tmp_path):
    from danbooru_multi_search.ui.image_cache import ImageCacheController
    from danbooru_multi_search.ui.cache_protection import CacheProtectionController
    database, favorites, repository = build(tmp_path)
    with database.connect() as connection:
        connection.execute("DELETE FROM favorite_collection_state")
    shared = controller_for(favorites)
    images = ImageCacheController(tmp_path / "images")
    protection = CacheProtectionController(repository, shared, images, access=shared._writes.access)
    try:
        assert shared._writes.access.begin_external_write("other")
        shared._writes.access.finish_external_write("other")
        protection.remember_redirect("https://example.invalid/a", "https://cdn.invalid/b")
        assert not protection.can_store() and protection._error
        assert "缺失" in images.statusText
    finally:
        images.shutdown()
        protection.shutdown()
