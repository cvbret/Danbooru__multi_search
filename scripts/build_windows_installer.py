"""Build the Windows onedir application and NSIS installer."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.windows_packaging import (
    APPLICATION_BASENAME,
    BUILD_OWNER_CONTENT,
    BUILD_OWNER_MARKER,
    INSTALLER_NAME,
    PROJECT_ROOT,
    PackagingError,
    ProcessTerminationError,
    clean_pyinstaller_environment,
    ensure_windows_build_environment,
    find_makensis,
    remove_owned_tree,
    run_application_checks,
    run_limited,
    sha256_file,
    validated_child,
    verify_makensis_version,
    verify_installer,
    verify_onedir,
)


SPEC_PATH = PROJECT_ROOT / "packaging" / "windows" / "DanbooruMultiSearch.spec"
NSIS_SCRIPT = PROJECT_ROOT / "packaging" / "windows" / "installer.nsi"
DEFAULT_OUTPUT_DIRECTORY = PROJECT_ROOT / "dist" / "windows"
DEFAULT_BUILD_PARENT = PROJECT_ROOT / ".windows-build"
INSTALLER_OWNER_MARKER = ".danbooru-multi-search-installer-owner"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--makensis", type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--build-parent", type=Path, default=DEFAULT_BUILD_PARENT)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    return parser


def _require_project_file(path: Path) -> None:
    if not path.is_file():
        raise PackagingError(f"Required packaging file is missing: {path}")


def _prepare_final_bundle(source_bundle: Path, output_directory: Path) -> Path:
    final_bundle = output_directory / APPLICATION_BASENAME
    if final_bundle.exists():
        remove_owned_tree(
            final_bundle,
            output_directory,
            BUILD_OWNER_MARKER,
            BUILD_OWNER_CONTENT,
        )
    shutil.copytree(source_bundle, final_bundle)
    (final_bundle / BUILD_OWNER_MARKER).write_text(
        BUILD_OWNER_CONTENT,
        encoding="utf-8",
    )
    return final_bundle


def _publish_installer(
    built_installer: Path,
    output_directory: Path,
) -> Path:
    final_installer = output_directory / INSTALLER_NAME
    owner_marker = output_directory / INSTALLER_OWNER_MARKER
    if final_installer.exists():
        if not owner_marker.is_file() or owner_marker.read_text(encoding="utf-8") != (
            BUILD_OWNER_CONTENT
        ):
            raise PackagingError(
                f"Refusing to replace an unowned installer: {final_installer}"
            )
    temporary_installer = output_directory / f".{INSTALLER_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copy2(built_installer, temporary_installer)
        os.replace(temporary_installer, final_installer)
        owner_marker.write_text(BUILD_OWNER_CONTENT, encoding="utf-8")
    finally:
        temporary_installer.unlink(missing_ok=True)
    return final_installer


def build_windows_installer(
    *,
    makensis: Path | None,
    output_directory: Path,
    build_parent: Path,
    timeout_seconds: float,
) -> tuple[Path, Path, str]:
    ensure_windows_build_environment()
    _require_project_file(SPEC_PATH)
    _require_project_file(NSIS_SCRIPT)
    makensis_path = find_makensis(makensis)
    verify_makensis_version(makensis_path)

    output_directory = output_directory.absolute()
    build_parent = build_parent.absolute()
    output_directory.mkdir(parents=True, exist_ok=True)
    build_parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(
        tempfile.mkdtemp(prefix="m1005-", dir=build_parent)
    ).resolve()
    validated_child(workspace, build_parent)
    (workspace / BUILD_OWNER_MARKER).write_text(
        BUILD_OWNER_CONTENT,
        encoding="utf-8",
    )
    preserve_workspace = False
    try:
        pyinstaller_dist = workspace / "pyinstaller-dist"
        pyinstaller_work = workspace / "pyinstaller-work"
        run_limited(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--clean",
                "--noconfirm",
                "--distpath",
                pyinstaller_dist,
                "--workpath",
                pyinstaller_work,
                SPEC_PATH,
            ],
            cwd=PROJECT_ROOT,
            timeout_seconds=timeout_seconds,
            environment=clean_pyinstaller_environment(),
        )
        built_bundle = pyinstaller_dist / APPLICATION_BASENAME
        verify_onedir(built_bundle)

        external_parent = Path(tempfile.gettempdir()).resolve()
        external_root = Path(
            tempfile.mkdtemp(prefix="danbooru-m1005-check-")
        ).resolve()
        (external_root / BUILD_OWNER_MARKER).write_text(
            BUILD_OWNER_CONTENT,
            encoding="utf-8",
        )
        preserve_external = False
        try:
            run_application_checks(
                built_bundle / f"{APPLICATION_BASENAME}.exe",
                working_directory=external_root,
                check_root=external_root / "中文 检查结果",
                label="portable",
            )
        except ProcessTerminationError:
            preserve_external = True
            preserve_workspace = True
            raise
        finally:
            if not preserve_external and external_root.exists():
                remove_owned_tree(
                    external_root,
                    external_parent,
                    BUILD_OWNER_MARKER,
                    BUILD_OWNER_CONTENT,
                )

        final_bundle = _prepare_final_bundle(built_bundle, output_directory)
        verify_onedir(final_bundle)

        nsis_output = workspace / "nsis-output"
        nsis_output.mkdir()
        run_limited(
            [
                makensis_path,
                f"/DAPP_SOURCE={built_bundle}",
                f"/DOUTPUT_DIR={nsis_output}",
                NSIS_SCRIPT,
            ],
            cwd=PROJECT_ROOT,
            timeout_seconds=timeout_seconds,
        )
        built_installer = nsis_output / INSTALLER_NAME
        if not built_installer.is_file():
            raise PackagingError(
                f"NSIS did not create the expected installer: {built_installer}"
            )
        verify_installer(built_installer)
        final_installer = _publish_installer(built_installer, output_directory)
        verify_installer(final_installer)
        installer_hash = sha256_file(final_installer)
        return final_bundle, final_installer, installer_hash
    except ProcessTerminationError:
        preserve_workspace = True
        raise
    finally:
        if workspace.exists() and not preserve_workspace:
            remove_owned_tree(
                workspace,
                build_parent,
                BUILD_OWNER_MARKER,
                BUILD_OWNER_CONTENT,
            )


def main() -> int:
    args = _build_parser().parse_args()
    if args.timeout_seconds <= 0:
        print("--timeout-seconds must be greater than zero.", file=sys.stderr)
        return 2
    try:
        bundle, installer, installer_hash = build_windows_installer(
            makensis=args.makensis,
            output_directory=args.output_dir,
            build_parent=args.build_parent,
            timeout_seconds=args.timeout_seconds,
        )
    except (OSError, PackagingError) as exc:
        print(f"Windows build failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "bundle": str(bundle),
                "installer": str(installer),
                "installer_sha256": installer_hash,
                "status": "ok",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
