from __future__ import annotations

import json

from danbooru_multi_search.core.bootstrap import initialize_runtime


def test_initialize_runtime_creates_settings_and_database(tmp_path):
    runtime = initialize_runtime(data_root=tmp_path)

    assert runtime.settings_created is True
    assert runtime.database_version == 7
    assert runtime.platform.services.paths.resolve() == runtime.paths
    assert runtime.paths.settings_file.is_file()
    assert runtime.paths.database_file.is_file()

    saved = json.loads(runtime.paths.settings_file.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 1
    assert saved["download_directory"] == str(tmp_path / "downloads")

    second_runtime = initialize_runtime(data_root=tmp_path)
    assert second_runtime.settings_created is False
    assert second_runtime.database_version == 7

