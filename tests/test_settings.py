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
    settings = AppSettings(language="zh-CN", download_directory="下载",
                           button_background_transparency=0.45)

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
    assert settings.background_image_path == ""
    assert settings.background_mask == 0.65
    assert settings.background_blur == 0.2
    assert settings.background_brightness == 1.0
    assert settings.button_background_transparency == 0
    assert AppSettings().page_size == 40


@pytest.mark.parametrize("value", [True, -0.01, 1.01, float("nan"), float("inf"), 10 ** 400])
def test_button_background_transparency_rejects_invalid_values(value):
    with pytest.raises(SettingsError, match="button_background_transparency"):
        AppSettings.from_mapping({"button_background_transparency": value})


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


def test_default_download_directory_merges_and_survives_restart(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    initial = AppSettings(theme="light", tag_csv_path=str(tmp_path / "tags.csv"), image_cache_limit_bytes=768 * 1024 * 1024)
    store.save(initial)
    controller = SettingsController(store, initial)
    directory = tmp_path / "中文 下载"
    saved = controller.set_download_directory(directory)
    assert store.load_or_default(AppSettings()) == saved
    assert saved.download_directory == str(directory)
    assert saved.theme == initial.theme and saved.tag_csv_path == initial.tag_csv_path
    assert saved.image_cache_limit_bytes == initial.image_cache_limit_bytes


def test_default_download_directory_failure_retains_previous_value(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path / "settings.json")
    previous = AppSettings(download_directory=str(tmp_path / "old"))
    store.save(previous)
    controller = SettingsController(store, previous)
    def fail(_settings):
        raise SettingsError("save failed")
    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(SettingsError):
        controller.set_download_directory(tmp_path / "new")
    assert controller.settings is previous and store.load_or_default(AppSettings()) == previous
