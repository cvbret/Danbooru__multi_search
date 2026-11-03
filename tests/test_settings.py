from __future__ import annotations

import json

import pytest

from danbooru_multi_search.core.settings import (
    AppSettings,
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
