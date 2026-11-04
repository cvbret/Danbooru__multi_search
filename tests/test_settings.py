from __future__ import annotations

import json
import threading

import pytest

from danbooru_multi_search.core.settings import (
    DEFAULT_IMAGE_CACHE_LIMIT_BYTES,
    MAX_IMAGE_CACHE_LIMIT_BYTES,
    MIN_IMAGE_CACHE_LIMIT_BYTES,
    AppSettings,
    SettingsController,
    SettingsError,
    SettingsStore,
)


def test_missing_settings_returns_default_without_writing(tmp_path):
    path = tmp_path / "config" / "settings.json"
    store = SettingsStore(path)
    default = AppSettings(download_directory="downloads")

    assert store.load_or_default(default) == default
    assert path.exists() is False


def test_settings_round_trip_is_atomic(tmp_path):
    path = tmp_path / "config" / "settings.json"
    store = SettingsStore(path)
    settings = AppSettings(language="zh-CN", download_directory="下载")

    store.save(settings)

    assert store.load_or_default(AppSettings()) == settings
    assert json.loads(path.read_text(encoding="utf-8"))["download_directory"] == "下载"
    assert list(path.parent.glob(".settings.json.*.tmp")) == []


def test_invalid_settings_are_reported_without_overwrite(tmp_path):
    path = tmp_path / "settings.json"
    original = "{invalid json"
    path.write_text(original, encoding="utf-8")
    store = SettingsStore(path)

    with pytest.raises(SettingsError):
        store.load_or_default(AppSettings())

    assert path.read_text(encoding="utf-8") == original


def test_version_one_settings_fill_new_defaults_and_keep_legal_old_page_size(
    tmp_path,
):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "language": "zh-CN",
                "theme": "system",
                "page_size": 20,
                "danbooru_api_url": "https://example.invalid",
                "download_directory": "downloads",
            }
        ),
        encoding="utf-8",
    )

    settings = SettingsStore(path).load_or_default(AppSettings())

    assert settings.schema_version == 1
    assert settings.page_size == 20
    assert settings.tag_csv_path == ""
    assert settings.image_cache_limit_bytes == DEFAULT_IMAGE_CACHE_LIMIT_BYTES
    assert AppSettings().page_size == 40


@pytest.mark.parametrize("page_size", [1, 40, 200])
def test_settings_accept_gallery_page_size_range(page_size):
    assert AppSettings.from_mapping({"page_size": page_size}).page_size == page_size


@pytest.mark.parametrize("page_size", [0, 201, True])
def test_settings_reject_page_size_outside_gallery_range(page_size):
    with pytest.raises(SettingsError, match="page_size"):
        AppSettings.from_mapping({"page_size": page_size})


@pytest.mark.parametrize(
    "cache_limit",
    [MIN_IMAGE_CACHE_LIMIT_BYTES, DEFAULT_IMAGE_CACHE_LIMIT_BYTES, MAX_IMAGE_CACHE_LIMIT_BYTES],
)
def test_settings_accept_image_cache_limit_range(cache_limit):
    settings = AppSettings.from_mapping({"image_cache_limit_bytes": cache_limit})

    assert settings.image_cache_limit_bytes == cache_limit


@pytest.mark.parametrize(
    "cache_limit",
    [MIN_IMAGE_CACHE_LIMIT_BYTES - 1, MAX_IMAGE_CACHE_LIMIT_BYTES + 1, True],
)
def test_settings_reject_invalid_image_cache_limit(cache_limit):
    with pytest.raises(SettingsError, match="image_cache_limit_bytes"):
        AppSettings.from_mapping({"image_cache_limit_bytes": cache_limit})


def test_settings_controller_serializes_interleaved_field_updates(tmp_path):
    settings_path = tmp_path / "config" / "settings.json"
    csv_path = (tmp_path / "标签 100%.csv").resolve()
    store = SettingsStore(settings_path)
    initial = AppSettings(download_directory="downloads")
    store.save(initial)
    controller = SettingsController(store, initial)
    barrier = threading.Barrier(3)
    failures: list[BaseException] = []

    def update_csv() -> None:
        try:
            barrier.wait()
            controller.set_tag_csv_path(csv_path)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    def update_cache() -> None:
        try:
            barrier.wait()
            controller.set_image_cache_limit_bytes(768 * 1024 * 1024)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [
        threading.Thread(target=update_csv),
        threading.Thread(target=update_cache),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert failures == []
    assert all(not thread.is_alive() for thread in threads)
    persisted = store.load_or_default(AppSettings())
    assert persisted == controller.settings
    assert persisted.tag_csv_path == str(csv_path)
    assert persisted.image_cache_limit_bytes == 768 * 1024 * 1024


def test_settings_controller_keeps_previous_snapshot_when_save_fails(
    tmp_path,
    monkeypatch,
):
    store = SettingsStore(tmp_path / "settings.json")
    initial = AppSettings()
    store.save(initial)
    controller = SettingsController(store, initial)

    def fail_save(_settings) -> None:
        raise SettingsError("simulated save failure")

    monkeypatch.setattr(store, "save", fail_save)

    with pytest.raises(SettingsError, match="simulated"):
        controller.set_image_cache_limit_bytes(768 * 1024 * 1024)

    assert controller.settings == initial
