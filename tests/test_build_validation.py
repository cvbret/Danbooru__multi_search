from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

from scripts import verify_build


LINUX_PACKAGE_PATTERN = re.compile(
    r"^\s+(lib[0-9A-Za-z.+-]+)(?:\s+\\)?\s*$",
    re.MULTILINE,
)


def _linux_packages(path: Path) -> set[str]:
    return set(LINUX_PACKAGE_PATTERN.findall(path.read_text(encoding="utf-8")))


def test_current_python_keeps_virtual_environment_launcher_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    launcher = tmp_path / "environment" / "bin" / "python"
    expected = launcher.absolute()
    monkeypatch.setattr(verify_build.sys, "executable", str(launcher))

    with patch.object(
        Path,
        "resolve",
        side_effect=AssertionError("virtual environment launcher was resolved"),
    ):
        actual = verify_build._current_python()

    assert actual == expected


def test_linux_qt_package_lists_match_and_include_runtime_libraries() -> None:
    container_script = verify_build.PROJECT_ROOT / "scripts" / "run_linux_validation.sh"
    workflow = verify_build.PROJECT_ROOT / ".github" / "workflows" / "ci.yml"

    container_packages = _linux_packages(container_script)
    workflow_packages = _linux_packages(workflow)

    assert {"libglib2.0-0t64", "libgssapi-krb5-2"} <= container_packages
    assert workflow_packages == container_packages
