from __future__ import annotations

from contextlib import nullcontext

from PySide6.QtCore import QByteArray, QDateTime, QUrl
from PySide6.QtNetwork import QNetworkCacheMetaData

from danbooru_multi_search.ui.image_cache import cache_key
from danbooru_multi_search.ui.protected_image_cache import ProtectedImageDiskCache
from tests.test_image_cache import APPLICATION


def insert(cache, url, body=b"image" * 800):
    metadata = QNetworkCacheMetaData()
    metadata.setUrl(QUrl(url))
    metadata.setSaveToDisk(True)
    metadata.setExpirationDate(QDateTime.currentDateTimeUtc().addSecs(3600))
    metadata.setRawHeaders([(QByteArray(b"Content-Type"), QByteArray(b"image/jpeg")),
                            (QByteArray(b"Content-Length"), QByteArray(str(len(body)).encode())),
                            (QByteArray(b"Cache-Control"), QByteArray(b"public, max-age=3600"))])
    device = cache.prepare(metadata)
    assert device is not None
    assert device.write(body) == len(body)
    cache.insert(device)
    APPLICATION.processEvents()


def make_cache(tmp_path, protected, maximum=100000):
    messages = []
    cache = ProtectedImageDiskCache(key=cache_key, protection=lambda: nullcontext(frozenset(cache_key(url) for url in protected)), status=messages.append)
    cache.setCacheDirectory(str(tmp_path / "images"))
    cache.setMaximumCacheSize(maximum)
    return cache, messages


def test_real_qt_clear_keeps_favorite_and_never_deletes_unknown_or_download_files(tmp_path):
    favorite, ordinary = "https://example.invalid/favorite.jpg", "https://example.invalid/ordinary.jpg"
    cache, messages = make_cache(tmp_path, {favorite})
    insert(cache, favorite)
    insert(cache, ordinary)
    unknown = tmp_path / "images" / "download.part"
    unknown.write_bytes(b"unfinished download")
    outside = tmp_path / "finished.jpg"
    outside.write_bytes(b"formal download")
    cache.clear()
    assert cache.metaData(QUrl(favorite)).isValid()
    assert not cache.metaData(QUrl(ordinary)).isValid()
    assert bytes(cache.data(QUrl(favorite)).readAll()) == b"image" * 800
    assert unknown.read_bytes() == b"unfinished download" and outside.read_bytes() == b"formal download"
    assert cache.removed_count == 1 and cache.retained_count == 1


def test_real_qt_shrink_and_expire_keep_protected_bytes_even_above_limit(tmp_path):
    favorite, ordinary = "https://example.invalid/favorite.jpg", "https://example.invalid/ordinary.jpg"
    cache, messages = make_cache(tmp_path, {favorite})
    insert(cache, favorite)
    insert(cache, ordinary)
    cache.setMaximumCacheSize(1000)
    assert cache.metaData(QUrl(favorite)).isValid()
    assert not cache.metaData(QUrl(ordinary)).isValid()
    assert cache.cacheSize() > cache.maximumCacheSize()
    assert any("超过容量上限" in message for message in messages)
    cache.expire()
    assert cache.metaData(QUrl(favorite)).isValid()


def test_real_qt_insert_capacity_eviction_uses_protection(tmp_path):
    favorite = "https://example.invalid/old-favorite.jpg"
    cache, messages = make_cache(tmp_path, {favorite}, maximum=10000)
    insert(cache, favorite)
    insert(cache, "https://example.invalid/one.jpg")
    insert(cache, "https://example.invalid/two.jpg")
    insert(cache, "https://example.invalid/three.jpg")
    assert cache.metaData(QUrl(favorite)).isValid()
    assert not all(cache.metaData(QUrl(f"https://example.invalid/{name}.jpg")).isValid() for name in ("one", "two", "three"))
    assert cache.cacheSize() <= cache.maximumCacheSize()


def test_unavailable_protection_keeps_all_existing_qt_entries(tmp_path):
    cache = ProtectedImageDiskCache(key=cache_key, protection=lambda: nullcontext(None))
    cache.setCacheDirectory(str(tmp_path / "images"))
    cache.setMaximumCacheSize(100000)
    insert(cache, "https://example.invalid/image.jpg")
    cache.setMaximumCacheSize(1000)
    cache.clear()
    assert cache.metaData(QUrl("https://example.invalid/image.jpg")).isValid()


def test_remove_failure_is_not_reported_as_a_protected_favorite(tmp_path, monkeypatch):
    ordinary = "https://example.invalid/ordinary.jpg"
    cache, messages = make_cache(tmp_path, set())
    insert(cache, ordinary)
    monkeypatch.setattr(cache, "remove", lambda url: False)
    cache.clear()
    assert not cache.maintenance_ok and cache.failed_count == 1 and cache.protected_count == 0
    assert "未能删除" in messages[-1] and "收藏图片缓存已超过" not in messages[-1]


def test_prepared_same_url_replacement_does_not_replace_committed_size_during_expire(tmp_path):
    url = "https://example.invalid/same.jpg"
    cache, messages = make_cache(tmp_path, {url})
    insert(cache, url, b"old" * 100)
    before = cache.entries()
    metadata = cache.metaData(QUrl(url))
    metadata.setRawHeaders([(QByteArray(b"Content-Type"), QByteArray(b"image/jpeg")),
                            (QByteArray(b"Content-Length"), QByteArray(b"9000"))])
    device = cache.prepare(metadata)
    assert device is not None and device.write(b"new" * 3000) == 9000
    assert cache.entries() == before
    cache.expire()
    assert bytes(cache.data(QUrl(url)).readAll()) == b"old" * 100
    cache.insert(device)
    assert bytes(cache.data(QUrl(url)).readAll()) == b"new" * 3000


def test_insertions_below_capacity_do_not_rescan_files_or_reload_protection(tmp_path, monkeypatch):
    calls = []
    cache = ProtectedImageDiskCache(key=cache_key, protection=lambda: (calls.append("protection") or nullcontext(frozenset())))
    cache.setCacheDirectory(str(tmp_path / "images"))
    cache.setMaximumCacheSize(1000000)
    cache.cacheSize()
    original = cache.entries
    def entries():
        calls.append("scan")
        return original()
    monkeypatch.setattr(cache, "entries", entries)
    for index in range(12):
        insert(cache, f"https://example.invalid/{index}.jpg", b"image" * 20)
    assert calls == []
