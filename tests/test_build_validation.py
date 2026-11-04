from __future__ import annotations

import io
import re
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

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


def _write_test_wheel(path: Path, qml_members: set[str]) -> None:
    with zipfile.ZipFile(path, mode="w") as archive:
        for member in qml_members:
            archive.writestr(member, "import QtQuick\nItem {}\n")
        archive.writestr(
            "danbooru_multi_search-0.1.0.dist-info/METADATA",
            "\n".join(
                (
                    "Metadata-Version: 2.4",
                    "Name: danbooru-multi-search",
                    "Version: 0.1.0",
                    "Requires-Python: >=3.11,<3.14",
                    "Requires-Dist: PySide6>=6.12,<6.13",
                    "",
                )
            ),
        )
        archive.writestr(
            "danbooru_multi_search-0.1.0.dist-info/entry_points.txt",
            "[console_scripts]\n"
            "danbooru-multi-search = danbooru_multi_search.__main__:main\n",
        )


@pytest.mark.parametrize(
    "missing_member",
    verify_build.EXPECTED_QML_MEMBERS,
)
def test_wheel_validation_requires_every_qml_file(
    tmp_path: Path,
    missing_member: str,
) -> None:
    wheel = tmp_path / "test.whl"
    members = set(verify_build.EXPECTED_QML_MEMBERS) - {missing_member}
    _write_test_wheel(wheel, members)

    with pytest.raises(verify_build.ValidationError, match="required QML"):
        verify_build.verify_wheel(wheel)


def _write_test_sdist(
    path: Path,
    source_root: Path,
    missing_member: str,
) -> None:
    required = {"pyproject.toml"}
    required.update(
        item.relative_to(source_root).as_posix()
        for item in (source_root / "src" / "danbooru_multi_search").rglob("*.py")
        if item.is_file()
    )
    required.update(f"src/{member}" for member in verify_build.EXPECTED_QML_MEMBERS)
    required.remove(missing_member)
    with tarfile.open(path, mode="w:gz") as archive:
        for member in sorted(required):
            data = io.BytesIO(b"placeholder\n")
            info = tarfile.TarInfo(f"danbooru_multi_search-0.1.0/{member}")
            info.size = len(data.getbuffer())
            archive.addfile(info, data)


@pytest.mark.parametrize(
    "missing_member",
    (
        "src/danbooru_multi_search/ui/qml/DetailPage.qml",
        "src/danbooru_multi_search/ui/qml/ImageViewer.qml",
    ),
)
def test_source_distribution_validation_requires_new_qml_files(
    tmp_path: Path,
    missing_member: str,
) -> None:
    source_distribution = tmp_path / "test.tar.gz"
    _write_test_sdist(
        source_distribution,
        verify_build.PROJECT_ROOT,
        missing_member,
    )

    with pytest.raises(verify_build.ValidationError, match="missing required files"):
        verify_build.verify_source_distribution(
            source_distribution,
            verify_build.PROJECT_ROOT,
        )
