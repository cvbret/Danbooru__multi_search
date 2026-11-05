"""Run the reusable Windows and Linux source and wheel validation sequence."""

from __future__ import annotations

import argparse
import configparser
import email.policy
from email.parser import BytesParser
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PACKAGE_ROOT = PROJECT_ROOT / "src"
INSTALLED_CHECK_SCRIPT = PROJECT_ROOT / "scripts" / "check_installed_package.py"
EXPECTED_QML_MEMBERS = (
    "danbooru_multi_search/ui/qml/Main.qml",
    "danbooru_multi_search/ui/qml/M2Main.qml",
    "danbooru_multi_search/ui/qml/DetailPage.qml",
    "danbooru_multi_search/ui/qml/ImageViewer.qml",
    "danbooru_multi_search/ui/qml/QueryEditorPanel.qml",
    "danbooru_multi_search/ui/qml/QueryFilterPanel.qml",
    "danbooru_multi_search/ui/qml/QueryStatusBar.qml",
    "danbooru_multi_search/ui/qml/HistoryPanel.qml",
)
EXPECTED_CONSOLE_SCRIPT = "danbooru-multi-search"
EXPECTED_CONSOLE_TARGET = "danbooru_multi_search.__main__:main"
EXPECTED_WHEEL_NAME = "danbooru_multi_search-0.1.0-py3-none-any.whl"
EXPECTED_SDIST_NAME = "danbooru_multi_search-0.1.0.tar.gz"
COPY_IGNORES = (
    ".build-validation-*",
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "*.egg-info",
    "*.pyc",
    "app-data",
    "build",
    "dist",
)


class ValidationError(RuntimeError):
    """Raised when an artifact does not satisfy the build contract."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=Path("dist"),
        help="Directory that receives the verified wheel and source distribution.",
    )
    return parser


def _clean_environment(
    additions: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    if additions:
        environment.update(additions)
    return environment


def _run(
    command: Sequence[os.PathLike[str] | str],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
) -> None:
    display = subprocess.list2cmdline([os.fspath(part) for part in command])
    print(f"+ {display}", flush=True)
    subprocess.run(
        [os.fspath(part) for part in command],
        cwd=cwd,
        env=dict(environment) if environment is not None else None,
        check=True,
    )


def _venv_python(environment_root: Path) -> Path:
    if os.name == "nt":
        return environment_root / "Scripts" / "python.exe"
    return environment_root / "bin" / "python"


def _current_python() -> Path:
    """Keep the active virtual environment launcher without resolving symlinks."""

    return Path(sys.executable).absolute()


def _venv_console_script(environment_root: Path) -> Path:
    if os.name == "nt":
        return environment_root / "Scripts" / f"{EXPECTED_CONSOLE_SCRIPT}.exe"
    return environment_root / "bin" / EXPECTED_CONSOLE_SCRIPT


def _single_artifact(directory: Path, pattern: str, description: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        names = ", ".join(path.name for path in matches) or "none"
        raise ValidationError(
            f"Expected one {description} in {directory}, found {len(matches)}: {names}"
        )
    return matches[0]


def verify_wheel(wheel_path: Path) -> None:
    """Verify QML and the runtime metadata required by an installed wheel."""

    try:
        with zipfile.ZipFile(wheel_path) as archive:
            names = archive.namelist()
            qml_counts = {
                member: names.count(member) for member in EXPECTED_QML_MEMBERS
            }
            metadata_names = [
                name for name in names if name.endswith(".dist-info/METADATA")
            ]
            entry_point_names = [
                name for name in names if name.endswith(".dist-info/entry_points.txt")
            ]
            if len(metadata_names) != 1 or len(entry_point_names) != 1:
                raise ValidationError(
                    "Wheel must contain exactly one METADATA file and one "
                    "entry_points.txt file."
                )
            metadata_bytes = archive.read(metadata_names[0])
            entry_points_text = archive.read(entry_point_names[0]).decode("utf-8")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValidationError(f"Unable to inspect wheel {wheel_path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ValidationError(f"Wheel entry points are not UTF-8: {exc}") from exc

    invalid_qml_members = {
        member: count for member, count in qml_counts.items() if count != 1
    }
    if invalid_qml_members:
        details = ", ".join(
            f"{member}: {count}"
            for member, count in invalid_qml_members.items()
        )
        raise ValidationError(
            "Wheel must contain every required QML file exactly once; found "
            + details
            + "."
        )

    metadata = BytesParser(policy=email.policy.default).parsebytes(metadata_bytes)
    requires_python = metadata.get("Requires-Python", "").replace(" ", "")
    if set(requires_python.split(",")) != {">=3.11", "<3.14"}:
        raise ValidationError(
            "Wheel Requires-Python must be <3.14,>=3.11; found "
            f"{requires_python or 'missing'}."
        )
    runtime_requirements = [
        value.replace(" ", "") for value in metadata.get_all("Requires-Dist", [])
    ]
    pyside_requirements = [
        value.removeprefix("PySide6")
        for value in runtime_requirements
        if value.startswith("PySide6")
    ]
    if not any(
        set(requirement.split(",")) == {">=6.12", "<6.13"}
        for requirement in pyside_requirements
    ):
        raise ValidationError(
            "Wheel must require PySide6<6.13,>=6.12; found "
            f"{runtime_requirements or 'no runtime requirements'}."
        )

    entry_points = configparser.ConfigParser(interpolation=None)
    try:
        entry_points.read_string(entry_points_text)
    except configparser.Error as exc:
        raise ValidationError(f"Wheel entry points are invalid: {exc}") from exc
    if entry_points.get("console_scripts", EXPECTED_CONSOLE_SCRIPT, fallback="") != (
        EXPECTED_CONSOLE_TARGET
    ):
        raise ValidationError(
            f"Wheel must map {EXPECTED_CONSOLE_SCRIPT} to "
            f"{EXPECTED_CONSOLE_TARGET}."
        )


def verify_source_distribution(
    source_distribution: Path,
    source_root: Path,
) -> None:
    """Require the sdist to contain pyproject, production Python, and QML files."""

    expected_members = {"pyproject.toml"}
    package_root = source_root / "src" / "danbooru_multi_search"
    expected_members.update(
        path.relative_to(source_root).as_posix()
        for path in package_root.rglob("*.py")
        if path.is_file()
    )
    expected_members.update(
        f"src/{member}" for member in EXPECTED_QML_MEMBERS
    )

    try:
        with tarfile.open(source_distribution, mode="r:gz") as archive:
            archive_members = {
                "/".join(Path(member.name).parts[1:])
                for member in archive.getmembers()
                if len(Path(member.name).parts) > 1
            }
    except (OSError, tarfile.TarError) as exc:
        raise ValidationError(
            f"Unable to inspect source distribution {source_distribution}: {exc}"
        ) from exc

    missing = sorted(expected_members - archive_members)
    if missing:
        raise ValidationError(
            "Source distribution is missing required files: " + ", ".join(missing)
        )


def _copy_project(destination: Path) -> None:
    shutil.copytree(
        PROJECT_ROOT,
        destination,
        ignore=shutil.ignore_patterns(*COPY_IGNORES),
    )


def _copy_verified_artifacts(
    wheel_path: Path,
    source_distribution: Path,
    artifact_dir: Path,
) -> tuple[Path, Path]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    copied_wheel = Path(shutil.copy2(wheel_path, artifact_dir / wheel_path.name))
    copied_source = Path(
        shutil.copy2(
            source_distribution,
            artifact_dir / source_distribution.name,
        )
    )
    return copied_wheel, copied_source


def run_validation(artifact_dir: Path) -> tuple[Path, Path]:
    python = _current_python()
    base_environment = _clean_environment()
    qml_environment = _clean_environment(
        {
            "LIBGL_ALWAYS_SOFTWARE": "1",
            "QML_DISABLE_DISK_CACHE": "1",
            "QT_QPA_PLATFORM": "offscreen",
            "QT_QUICK_BACKEND": "software",
        }
    )

    _run(
        [python, "-m", "compileall", "-q", "src", "tests", "scripts"],
        cwd=PROJECT_ROOT,
        environment=base_environment,
    )
    _run(
        [python, "-m", "pytest"],
        cwd=PROJECT_ROOT,
        environment=base_environment,
    )

    with tempfile.TemporaryDirectory(
        prefix=".build-validation-",
        dir=PROJECT_ROOT,
    ) as temporary_name:
        temporary_root = Path(temporary_name)
        source_data_root = temporary_root / "source-data"
        _run(
            [
                python,
                "-m",
                "danbooru_multi_search",
                "--headless-check",
                "--data-root",
                source_data_root,
            ],
            cwd=PROJECT_ROOT,
            environment=base_environment,
        )
        _run(
            [python, "-m", "pytest", "-q", "tests/test_qml_shell.py"],
            cwd=PROJECT_ROOT,
            environment=qml_environment,
        )

        build_source = temporary_root / "source"
        build_output = temporary_root / "artifacts"
        _copy_project(build_source)
        _run(
            [
                python,
                "-m",
                "build",
                "--sdist",
                "--wheel",
                "--outdir",
                build_output,
            ],
            cwd=build_source,
            environment=base_environment,
        )
        wheel_path = _single_artifact(build_output, "*.whl", "wheel")
        source_distribution = _single_artifact(
            build_output,
            "*.tar.gz",
            "source distribution",
        )
        if wheel_path.name != EXPECTED_WHEEL_NAME:
            raise ValidationError(
                f"Unexpected wheel filename: {wheel_path.name}; expected "
                f"{EXPECTED_WHEEL_NAME}."
            )
        if source_distribution.name != EXPECTED_SDIST_NAME:
            raise ValidationError(
                "Unexpected source distribution filename: "
                f"{source_distribution.name}; expected {EXPECTED_SDIST_NAME}."
            )
        verify_wheel(wheel_path)
        verify_source_distribution(source_distribution, build_source)

        rebuilt_output = temporary_root / "rebuilt-from-sdist"
        _run(
            [
                python,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--prefer-binary",
                "--wheel-dir",
                rebuilt_output,
                source_distribution,
            ],
            cwd=temporary_root,
            environment=base_environment,
        )
        rebuilt_wheel = _single_artifact(
            rebuilt_output,
            "*.whl",
            "wheel rebuilt from the source distribution",
        )
        if rebuilt_wheel.name != EXPECTED_WHEEL_NAME:
            raise ValidationError(
                f"Unexpected rebuilt wheel filename: {rebuilt_wheel.name}; "
                f"expected {EXPECTED_WHEEL_NAME}."
            )
        verify_wheel(rebuilt_wheel)

        isolated_environment = temporary_root / "installed-environment"
        _run(
            [python, "-m", "venv", isolated_environment],
            cwd=temporary_root,
            environment=base_environment,
        )
        isolated_python = _venv_python(isolated_environment)
        installed_run_root = temporary_root / "installed-run"
        installed_run_root.mkdir()
        _run(
            [
                isolated_python,
                "-m",
                "pip",
                "install",
                "--prefer-binary",
                wheel_path,
            ],
            cwd=installed_run_root,
            environment=base_environment,
        )
        installed_console_script = _venv_console_script(isolated_environment)
        if not installed_console_script.is_file():
            raise ValidationError(
                f"Installed console script is missing: {installed_console_script}"
            )
        _run(
            [
                installed_console_script,
                "--headless-check",
                "--data-root",
                installed_run_root / "console-data",
            ],
            cwd=installed_run_root,
            environment=base_environment,
        )
        _run(
            [
                isolated_python,
                "-m",
                "danbooru_multi_search",
                "--headless-check",
                "--data-root",
                installed_run_root / "data",
            ],
            cwd=installed_run_root,
            environment=base_environment,
        )
        _run(
            [
                isolated_python,
                INSTALLED_CHECK_SCRIPT,
                "--source-root",
                SOURCE_PACKAGE_ROOT,
            ],
            cwd=installed_run_root,
            environment=qml_environment,
        )

        return _copy_verified_artifacts(
            wheel_path,
            source_distribution,
            artifact_dir,
        )


def main() -> int:
    args = _build_parser().parse_args()
    artifact_dir = args.artifact_dir
    if not artifact_dir.is_absolute():
        artifact_dir = PROJECT_ROOT / artifact_dir
    artifact_dir = artifact_dir.resolve()

    try:
        wheel_path, source_distribution = run_validation(artifact_dir)
    except subprocess.CalledProcessError as exc:
        print(
            f"Build validation subprocess failed with exit code {exc.returncode}.",
            file=sys.stderr,
        )
        return exc.returncode or 1
    except (OSError, ValidationError) as exc:
        print(f"Build validation failed: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "artifacts": [wheel_path.name, source_distribution.name],
                "status": "ok",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
