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

