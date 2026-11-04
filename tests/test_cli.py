from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from danbooru_multi_search.__main__ import _build_parser


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
    assert output["schema_version"] == 2
    assert output["settings_created"] is True


def test_wallpaper_probe_is_mutually_exclusive_with_other_modes():
    parser = _build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--headless-check", "--wallpaper-probe"])

    with pytest.raises(SystemExit):
        parser.parse_args(["--danbooru-smoke", "--wallpaper-probe"])

    with pytest.raises(SystemExit):
        parser.parse_args(
            ["--danbooru-smoke", "--danbooru-download-smoke"]
        )

    with pytest.raises(SystemExit):
        parser.parse_args(
            ["--headless-check", "--danbooru-download-smoke"]
        )


def test_wallpaper_probe_outputs_stable_read_only_json(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project_root / "src")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "danbooru_multi_search",
            "--wallpaper-probe",
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

    assert result.returncode in {0, 2}, result.stderr
    output = json.loads(result.stdout)
    assert output["platform"] in {"windows", "linux"}
    if result.returncode == 0:
        assert output["status"] == "ok"
        assert set(output["wallpaper"]) == {
            "path",
            "style",
            "wallpaper_style_value",
            "tile_wallpaper_value",
        }
    else:
        assert output["status"] == "error"
        assert output["code"]
        assert output["message"]


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


def _run_download_smoke_subprocess(
    data_root: Path,
) -> subprocess.CompletedProcess[str]:
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project_root / "src")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "danbooru_multi_search",
            "--danbooru-download-smoke",
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
        "message": "Danbooru 返回 HTTP 503，重试 2 次后仍然失败。",
        "status": "error",
        "tags": "cat",
    }
    assert len(server.requests) == 3


def test_danbooru_download_smoke_queries_and_downloads_to_platform_path(
    local_http_server,
    tmp_path,
):
    image_bytes = b"download-smoke-image"
    image_server = local_http_server(image_bytes)
    post = {
        "id": 77,
        "preview_file_url": None,
        "large_file_url": None,
        "file_url": f"{image_server.base_url}/original.jpg",
        "score": 5,
        "fav_count": 3,
        "rating": "g",
        "image_width": 800,
        "image_height": 600,
        "file_ext": "jpg",
    }
    post_server = local_http_server(json.dumps([post]).encode("utf-8"))
    _write_settings(tmp_path, post_server.base_url)

    result = _run_download_smoke_subprocess(tmp_path)

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    expected_path = (tmp_path / "downloads" / "77.jpg").resolve()
    assert output == {
        "download": {
            "bytes": len(image_bytes),
            "path": str(expected_path),
        },
        "message": "Downloaded Danbooru post to 77.jpg.",
        "operation": "download",
        "post_id": 77,
        "source_url": f"{image_server.base_url}/original.jpg",
        "status": "ok",
        "tags": "cat",
    }
    assert expected_path.read_bytes() == image_bytes
    assert list(expected_path.parent.glob(".*.part")) == []
    assert len(post_server.requests) == 1
    assert len(image_server.requests) == 1


def test_danbooru_download_smoke_failure_is_nonzero_and_leaves_no_file(
    local_http_server,
    tmp_path,
):
    image_server = local_http_server(b"missing", status=404)
    post = {
        "id": 77,
        "preview_file_url": None,
        "large_file_url": None,
        "file_url": f"{image_server.base_url}/missing.jpg",
        "score": 5,
        "fav_count": 3,
        "rating": "g",
        "image_width": 800,
        "image_height": 600,
        "file_ext": "jpg",
    }
    post_server = local_http_server(json.dumps([post]).encode("utf-8"))
    _write_settings(tmp_path, post_server.base_url)

    result = _run_download_smoke_subprocess(tmp_path)

    assert result.returncode == 2, result.stderr
    output = json.loads(result.stdout)
    assert output["status"] == "error"
    assert output["operation"] == "download"
    assert output["error_kind"] == "http"
    assert output["http_status"] == 404
    assert not (tmp_path / "downloads" / "77.jpg").exists()
    assert list((tmp_path / "downloads").glob(".*.part")) == []
    assert len(post_server.requests) == 1
    assert len(image_server.requests) == 1

