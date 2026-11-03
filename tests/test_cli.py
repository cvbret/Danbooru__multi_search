from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_headless_check_initializes_and_exits(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project_root / "src")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "danbooru_multi_search",
            "--headless-check",
            "--data-root",
            str(tmp_path),
        ],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["status"] == "ok"
    assert output["schema_version"] == 1
    assert output["settings_created"] is True


def _write_settings(data_root: Path, base_url: str) -> None:
    config_directory = data_root / "config"
    config_directory.mkdir(parents=True)
    settings = {
        "schema_version": 1,
        "language": "zh-CN",
        "theme": "system",
        "page_size": 20,
        "danbooru_api_url": base_url,
        "download_directory": str(data_root / "downloads"),
    }
    (config_directory / "settings.json").write_text(
        json.dumps(settings),
        encoding="utf-8",
    )


def _run_smoke_subprocess(data_root: Path) -> subprocess.CompletedProcess[str]:
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project_root / "src")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "danbooru_multi_search",
            "--danbooru-smoke",
            "--danbooru-tags",
            "cat",
            "--danbooru-timeout-ms",
            "1000",
            "--data-root",
            str(data_root),
        ],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_danbooru_smoke_check_outputs_stable_json(local_http_server, tmp_path):
    post = {
        "id": 77,
        "preview_file_url": "https://cdn.example/preview.jpg",
        "large_file_url": "https://cdn.example/large.jpg",
        "file_url": "https://cdn.example/file.jpg",
        "score": 5,
        "fav_count": 3,
        "rating": "g",
        "image_width": 800,
        "image_height": 600,
        "file_ext": "jpg",
    }
    server = local_http_server(json.dumps([post]).encode("utf-8"))
    _write_settings(tmp_path, server.base_url)

    result = _run_smoke_subprocess(tmp_path)

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["status"] == "ok"
    assert output["tags"] == "cat"
    assert output["post"]["id"] == 77
    assert len(server.requests) == 1


def test_danbooru_smoke_check_failure_is_nonzero_json(local_http_server, tmp_path):
    server = local_http_server(b'{"error":"busy"}', status=503)
    _write_settings(tmp_path, server.base_url)

    result = _run_smoke_subprocess(tmp_path)

    assert result.returncode != 0
    output = json.loads(result.stdout)
    assert output == {
        "error_kind": "http",
        "http_status": 503,
        "message": "Danbooru 返回 HTTP 503。",
        "status": "error",
        "tags": "cat",
    }

