from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from danbooru_multi_search.storage.check_output import (
    CheckOutputError,
    CheckOutputExistsError,
    write_check_output,
)


def _temporary_outputs(directory: Path, target_name: str) -> list[Path]:
    return list(directory.glob(f".{target_name}.*.tmp"))


def test_writes_atomic_utf8_json_in_unicode_space_path(tmp_path):
    output_directory = tmp_path / "中文 结果"
    output_directory.mkdir()
    target = output_directory / "检查 结果.json"
    payload = {"message": "加载成功", "mode": "gui", "status": "ok"}

    result = write_check_output(target, payload)

    assert result == target
    assert json.loads(target.read_text(encoding="utf-8")) == payload
    assert "加载成功" in target.read_text(encoding="utf-8")
    assert _temporary_outputs(output_directory, target.name) == []


def test_rejects_existing_target_without_changing_its_contents(tmp_path):
    target = tmp_path / "result.json"
    target.write_text("existing", encoding="utf-8")

    with pytest.raises(CheckOutputExistsError, match="already exists"):
        write_check_output(target, {"status": "ok"})

    assert target.read_text(encoding="utf-8") == "existing"
    assert _temporary_outputs(tmp_path, target.name) == []


def test_publication_race_does_not_replace_winning_file(tmp_path, monkeypatch):
    target = tmp_path / "result.json"
    original_link = os.link

    def race_link(source, destination):
        Path(destination).write_text("race winner", encoding="utf-8")
        original_link(source, destination)

    monkeypatch.setattr(os, "link", race_link)

    with pytest.raises(CheckOutputExistsError, match="already exists"):
        write_check_output(target, {"status": "ok"})

    assert target.read_text(encoding="utf-8") == "race winner"
    assert _temporary_outputs(tmp_path, target.name) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"value": object()},
        {"value": float("nan")},
    ],
)
def test_serialization_failure_removes_temporary_file(tmp_path, payload):
    target = tmp_path / "result.json"

    with pytest.raises(CheckOutputError, match="Unable to write"):
        write_check_output(target, payload)

    assert not target.exists()
    assert _temporary_outputs(tmp_path, target.name) == []


def test_fsync_failure_removes_temporary_file(tmp_path, monkeypatch):
    target = tmp_path / "result.json"

    def fail_fsync(_descriptor):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(os, "fsync", fail_fsync)

    with pytest.raises(CheckOutputError, match="simulated fsync failure"):
        write_check_output(target, {"status": "ok"})

    assert not target.exists()
    assert _temporary_outputs(tmp_path, target.name) == []


def test_missing_parent_directory_is_rejected_without_creating_it(tmp_path):
    parent = tmp_path / "missing"
    target = parent / "result.json"

    with pytest.raises(CheckOutputError, match="parent directory"):
        write_check_output(target, {"status": "ok"})

    assert not parent.exists()
