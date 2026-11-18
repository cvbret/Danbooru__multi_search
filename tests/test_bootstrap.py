from __future__ import annotations

import json

import pytest

from danbooru_multi_search.app.bootstrap import initialize_runtime
from danbooru_multi_search.core.settings import SettingsError
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.settings import SettingsStore


def test_initialize_runtime_creates_settings_and_database(tmp_path):
    runtime = initialize_runtime(data_root=tmp_path)

    assert runtime.settings_created is True
    assert runtime.database_version == 12
    assert runtime.platform.services.paths.resolve() == runtime.paths
    assert runtime.paths.settings_file.is_file()
    assert runtime.paths.database_file.is_file()

    saved = json.loads(runtime.paths.settings_file.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 1
    assert saved["download_directory"] == str(tmp_path / "downloads")

    second_runtime = initialize_runtime(data_root=tmp_path)
    assert second_runtime.settings_created is False
    assert second_runtime.database_version == 12


def test_existing_legal_configuration_is_loaded_without_rewriting(tmp_path, monkeypatch):
    settings_path = tmp_path / "config" / "settings.json"
    settings_path.parent.mkdir()
    original = b'{ "schema_version": 1, "theme": "dark", "page_size": 20, "future_option": 7 }\n'
    settings_path.write_bytes(original)

    def unexpected_save(_store, _settings):
        raise AssertionError("Existing settings must not be rewritten at startup")

    monkeypatch.setattr(SettingsStore, "save", unexpected_save)
    runtime = initialize_runtime(tmp_path)

    assert runtime.settings_created is False
    assert runtime.settings.theme == "dark" and runtime.settings.page_size == 20
    assert settings_path.read_bytes() == original
    assert runtime.paths.database_file.is_file()


def test_invalid_configuration_is_not_overwritten_or_followed_by_database_init(
    tmp_path, monkeypatch
):
    settings_path = tmp_path / "config" / "settings.json"
    settings_path.parent.mkdir()
    original = b'{invalid settings'
    settings_path.write_bytes(original)

    def unexpected_initialize(_database):
        raise AssertionError("Invalid settings must stop database initialization")

    monkeypatch.setattr(Database, "initialize", unexpected_initialize)
    with pytest.raises(SettingsError, match="Unable to read settings"):
        initialize_runtime(tmp_path)

    assert settings_path.read_bytes() == original
    assert not (tmp_path / "data" / "application.sqlite3").exists()

