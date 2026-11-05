"""Perform the explicit Windows install, reinstall, and uninstall validation."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.windows_packaging import (
    APPLICATION_BASENAME,
    APPLICATION_DISPLAY_NAME,
    APPLICATION_EXE,
    APPLICATION_REGISTRY_KEY,
    INSTALLER_NAME,
    INSTALL_MARKER_CONTENT,
    INSTALL_MARKER_NAME,
    PACKAGE_VERSION,
    PROJECT_ROOT,
    PUBLISHER,
    START_MENU_DIRECTORY,
    START_MENU_SHORTCUT,
    UNINSTALLER_NAME,
    UNINSTALL_REGISTRY_KEY,
    PackagingError,
    ProcessTerminationError,
    native_windows_machine,
    normalize_windows_path,
    run_application_checks,
    run_limited,
    sha256_file,
    validated_child,
    verify_executable,
    verify_installer,
    wait_for_absence,
)


DEFAULT_INSTALLER = PROJECT_ROOT / "dist" / "windows" / INSTALLER_NAME
VALIDATION_OWNER_NAME = ".danbooru-multi-search-validation-owner"
VALIDATION_OWNER_CONTENT = (
    f"{APPLICATION_BASENAME}|{PACKAGE_VERSION}|install-validation"
)
PRESERVE_MARKER_CONTENT = "DanbooruMultiSearch external data must remain"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installer", type=Path, default=DEFAULT_INSTALLER)
    parser.add_argument("--confirm-system-changes", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    return parser


def _require_native_amd64_windows() -> None:
    if sys.platform != "win32" or os.name != "nt":
        raise PackagingError("Windows installation validation requires Windows.")
    if native_windows_machine() != 0x8664:
        raise PackagingError(
            "Windows installation validation requires native AMD64 Windows."
        )


def _windows_directories() -> tuple[Path, Path]:
    local_app_data = os.environ.get("LOCALAPPDATA")
    app_data = os.environ.get("APPDATA")
    if not local_app_data or not app_data:
        raise PackagingError("LOCALAPPDATA and APPDATA are required.")
    default_install = (
        Path(local_app_data) / "Programs" / APPLICATION_BASENAME
    )
    start_menu = (
        Path(app_data)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / START_MENU_DIRECTORY
    )
    return default_install, start_menu


def _read_registry_key(key_name: str) -> dict[str, object] | None:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            key_name,
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            values: dict[str, object] = {}
            index = 0
            while True:
                try:
                    name, value, _value_type = winreg.EnumValue(key, index)
                except OSError:
                    break
                values[name] = value
                index += 1
            return values
    except FileNotFoundError:
        return None


def _named_process_running(image_name: str, timeout_seconds: float) -> bool:
    result = run_limited(
        [
            "tasklist.exe",
            "/FI",
            f"IMAGENAME eq {image_name}",
            "/FO",
            "CSV",
            "/NH",
        ],
        cwd=Path(tempfile.gettempdir()),
        timeout_seconds=min(timeout_seconds, 30.0),
    )
    return image_name.casefold() in result.stdout.casefold()


def _preflight(
    timeout_seconds: float,
    installer_name: str,
) -> tuple[Path, Path]:
    default_install, start_menu = _windows_directories()
    conflicts: list[str] = []
    if default_install.exists():
        conflicts.append(str(default_install))
    if start_menu.exists():
        conflicts.append(str(start_menu))
    if _read_registry_key(APPLICATION_REGISTRY_KEY) is not None:
        conflicts.append(f"HKCU\\{APPLICATION_REGISTRY_KEY}")
    if _read_registry_key(UNINSTALL_REGISTRY_KEY) is not None:
        conflicts.append(f"HKCU\\{UNINSTALL_REGISTRY_KEY}")
    for image_name in (APPLICATION_EXE, installer_name, UNINSTALLER_NAME):
        if _named_process_running(image_name, timeout_seconds):
            conflicts.append(image_name)
    if conflicts:
        raise PackagingError(
            "Installation validation found existing user objects: "
            + ", ".join(conflicts)
        )
    return default_install, start_menu


def build_silent_install_command(installer: Path, install_directory: Path) -> str:
    """Build the NSIS raw command line with an unquoted, final /D value."""

    if not installer.is_absolute() or not install_directory.is_absolute():
        raise PackagingError("Installer and installation paths must be absolute.")
    prefix = subprocess.list2cmdline([str(installer), "/S"])
    return f"{prefix} /D={install_directory}"


def _run_installer(
    installer: Path,
    install_directory: Path,
    *,
    timeout_seconds: float,
) -> None:
    command = build_silent_install_command(installer, install_directory)
    run_limited(
        command,
        executable=installer,
        cwd=installer.parent,
        timeout_seconds=timeout_seconds,
    )


def _read_shortcut_target(
    shortcut: Path,
    helper_directory: Path,
    *,
    timeout_seconds: float,
) -> Path:
    helper = helper_directory / "read-shortcut-target.vbs"
    helper.write_text(
        "Set shell = CreateObject(\"WScript.Shell\")\r\n"
        "Set link = shell.CreateShortcut(WScript.Arguments(0))\r\n"
        "WScript.StdOut.Write link.TargetPath\r\n",
        encoding="ascii",
    )
    try:
        result = run_limited(
            ["cscript.exe", "//NoLogo", "//U", helper, shortcut],
            cwd=helper_directory,
            timeout_seconds=min(timeout_seconds, 30.0),
            encoding="utf-16-le",
        )
    finally:
        helper.unlink(missing_ok=True)
    target = result.stdout.lstrip("\ufeff").rstrip("\r\n")
    if not target:
        raise PackagingError(f"Shortcut has no target: {shortcut}")
    return Path(target)


def _require_value(
    values: Mapping[str, object],
    name: str,
    expected: object,
) -> None:
    actual = values.get(name)
    if actual != expected:
        raise PackagingError(
            f"Registry value {name} is {actual!r}; expected {expected!r}."
        )


def _verify_installation(
    install_directory: Path,
    start_menu_directory: Path,
    helper_directory: Path,
    *,
    timeout_seconds: float,
) -> tuple[Path, Path]:
    app_directory = install_directory / "app"
    executable = app_directory / APPLICATION_EXE
    uninstaller = install_directory / UNINSTALLER_NAME
    marker = install_directory / INSTALL_MARKER_NAME
    shortcut = start_menu_directory / START_MENU_SHORTCUT
    for path in (app_directory, executable, uninstaller, marker, shortcut):
        if not path.exists():
            raise PackagingError(f"Installed object is missing: {path}")
    verify_executable(executable)
    if marker.read_text(encoding="utf-8") != INSTALL_MARKER_CONTENT:
        raise PackagingError("The installation marker content is invalid.")

    application_values = _read_registry_key(APPLICATION_REGISTRY_KEY)
    uninstall_values = _read_registry_key(UNINSTALL_REGISTRY_KEY)
    if application_values is None or uninstall_values is None:
        raise PackagingError("The current-user installation registry keys are missing.")
    normalized_install = normalize_windows_path(install_directory)
    for values in (application_values, uninstall_values):
        actual_location = values.get("InstallLocation")
        if not isinstance(actual_location, str) or (
            normalize_windows_path(actual_location) != normalized_install
        ):
            raise PackagingError("Registry InstallLocation does not match the test path.")
        _require_value(values, "DisplayVersion", PACKAGE_VERSION)
    _require_value(uninstall_values, "DisplayName", APPLICATION_DISPLAY_NAME)
    _require_value(uninstall_values, "Publisher", PUBLISHER)
    _require_value(uninstall_values, "DisplayIcon", str(executable))
    _require_value(
        uninstall_values,
        "UninstallString",
        f'"{uninstaller}"',
    )
    _require_value(
        uninstall_values,
        "QuietUninstallString",
        f'"{uninstaller}" /S',
    )
    _require_value(uninstall_values, "NoModify", 1)
    _require_value(uninstall_values, "NoRepair", 1)
    estimated_size = uninstall_values.get("EstimatedSize")
    if not isinstance(estimated_size, int) or estimated_size <= 0:
        raise PackagingError("EstimatedSize is missing or invalid.")

    shortcut_target = _read_shortcut_target(
        shortcut,
        helper_directory,
        timeout_seconds=timeout_seconds,
    )
    if normalize_windows_path(shortcut_target) != normalize_windows_path(executable):
        raise PackagingError("The Start menu shortcut target is incorrect.")
    return executable, uninstaller


def build_controlled_uninstall_command(uninstaller: Path) -> str:
    """Build a direct NSIS uninstall command whose final argument is ``_?=``."""

    if not uninstaller.is_absolute():
        raise PackagingError("The uninstaller path must be absolute.")
    prefix = subprocess.list2cmdline([str(uninstaller), "/S"])
    return f"{prefix} _?={uninstaller.parent}"


def _run_uninstaller(uninstaller: Path, *, timeout_seconds: float) -> None:
    command = build_controlled_uninstall_command(uninstaller)
    run_limited(
        command,
        executable=uninstaller,
        cwd=uninstaller.parent,
        timeout_seconds=timeout_seconds,
    )
    install_directory = uninstaller.parent
    if not install_directory.exists():
        return
    try:
        remaining = [
            path
            for path in install_directory.iterdir()
            if path != uninstaller
        ]
    except OSError as exc:
        raise PackagingError(
            f"Unable to inspect the uninstalled directory: {exc}"
        ) from exc
    if remaining:
        raise PackagingError(
            "The controlled uninstaller left unexpected installation objects: "
            + ", ".join(path.name for path in remaining)
        )
    try:
        uninstaller.unlink(missing_ok=True)
        install_directory.rmdir()
    except OSError as exc:
        raise PackagingError(
            f"Unable to remove the controlled uninstaller directory: {exc}"
        ) from exc


def _verify_uninstalled(
    install_directory: Path,
    start_menu_directory: Path,
    preserve_marker: Path,
    installer_name: str,
    *,
    timeout_seconds: float,
) -> None:
    wait_for_absence(
        install_directory.exists,
        timeout_seconds=timeout_seconds,
        description="the installation directory",
    )
    if start_menu_directory.exists():
        raise PackagingError("The Start menu directory remains after uninstall.")
    if _read_registry_key(APPLICATION_REGISTRY_KEY) is not None:
        raise PackagingError("The application registry key remains after uninstall.")
    if _read_registry_key(UNINSTALL_REGISTRY_KEY) is not None:
        raise PackagingError("The uninstall registry key remains after uninstall.")
    if not preserve_marker.is_file() or (
        preserve_marker.read_text(encoding="utf-8") != PRESERVE_MARKER_CONTENT
    ):
        raise PackagingError("External application data was not preserved.")
    remaining = [
        image_name
        for image_name in (APPLICATION_EXE, installer_name, UNINSTALLER_NAME)
        if _named_process_running(image_name, timeout_seconds)
    ]
    if remaining:
        raise PackagingError(
            "Packaging processes remain after uninstall: " + ", ".join(remaining)
        )


def _cleanup_validation_root(root: Path, temporary_parent: Path) -> None:
    target = validated_child(root, temporary_parent)
    owner = target / VALIDATION_OWNER_NAME
    if owner.read_text(encoding="utf-8") != VALIDATION_OWNER_CONTENT:
        raise PackagingError("Refusing to remove an unowned validation directory.")
    shutil.rmtree(target)


def _try_recover_owned_installation(
    install_directory: Path,
    start_menu_directory: Path,
    *,
    timeout_seconds: float,
) -> bool:
    """Uninstall only when every ownership identity still matches this run."""

    if not install_directory.exists():
        return (
            _read_registry_key(APPLICATION_REGISTRY_KEY) is None
            and _read_registry_key(UNINSTALL_REGISTRY_KEY) is None
            and not start_menu_directory.exists()
        )
    marker = install_directory / INSTALL_MARKER_NAME
    uninstaller = install_directory / UNINSTALLER_NAME
    try:
        if marker.read_text(encoding="utf-8") != INSTALL_MARKER_CONTENT:
            return False
    except OSError:
        return False
    application_values = _read_registry_key(APPLICATION_REGISTRY_KEY)
    uninstall_values = _read_registry_key(UNINSTALL_REGISTRY_KEY)
    if application_values is None or uninstall_values is None:
        return False
    expected_location = normalize_windows_path(install_directory)
    for values in (application_values, uninstall_values):
        location = values.get("InstallLocation")
        if (
            not isinstance(location, str)
            or normalize_windows_path(location) != expected_location
            or values.get("DisplayVersion") != PACKAGE_VERSION
        ):
            return False
    if not uninstaller.is_file():
        return False
    try:
        _run_uninstaller(uninstaller, timeout_seconds=timeout_seconds)
    except ProcessTerminationError:
        raise
    except (OSError, PackagingError):
        return False
    return (
        not install_directory.exists()
        and not start_menu_directory.exists()
        and _read_registry_key(APPLICATION_REGISTRY_KEY) is None
        and _read_registry_key(UNINSTALL_REGISTRY_KEY) is None
    )


def verify_windows_install(
    installer: Path,
    *,
    timeout_seconds: float,
) -> dict[str, object]:
    _require_native_amd64_windows()
    installer = installer.resolve()
    if not installer.is_file() or installer.name != INSTALLER_NAME:
        raise PackagingError(
            f"Expected installer {INSTALLER_NAME}; found {installer}."
        )
    verify_installer(installer)
    _default_install, start_menu_directory = _preflight(
        timeout_seconds,
        installer.name,
    )

    temporary_parent = Path(tempfile.gettempdir()).resolve()
    validation_root = Path(
        tempfile.mkdtemp(
            prefix=f"{APPLICATION_BASENAME}-中文 安装验证-{uuid.uuid4().hex}-",
            dir=temporary_parent,
        )
    ).resolve()
    (validation_root / VALIDATION_OWNER_NAME).write_text(
        VALIDATION_OWNER_CONTENT,
        encoding="utf-8",
    )
    install_directory = validation_root / "中文 安装 目录"
    check_root = validation_root / "中文 数据与结果"
    working_directory = validation_root / "仓库外 工作目录"
    preserve_marker: Path | None = None
    completed = False
    preserve_scene = False
    try:
        _run_installer(
            installer,
            install_directory,
            timeout_seconds=timeout_seconds,
        )
        executable, uninstaller = _verify_installation(
            install_directory,
            start_menu_directory,
            validation_root,
            timeout_seconds=timeout_seconds,
        )
        first_results = run_application_checks(
            executable,
            working_directory=working_directory,
            check_root=check_root / "首次安装 检查",
            label="first-install",
            timeout_seconds=timeout_seconds,
        )
        headless_data_directory = Path(first_results[0]["data_directory"])
        preserve_marker = headless_data_directory.parent / "外部数据 保留标记.txt"
        preserve_marker.write_text(PRESERVE_MARKER_CONTENT, encoding="utf-8")

        _run_installer(
            installer,
            install_directory,
            timeout_seconds=timeout_seconds,
        )
        executable, uninstaller = _verify_installation(
            install_directory,
            start_menu_directory,
            validation_root,
            timeout_seconds=timeout_seconds,
        )
        run_application_checks(
            executable,
            working_directory=working_directory,
            check_root=check_root / "同版本重装 检查",
            label="same-version-reinstall",
            timeout_seconds=timeout_seconds,
        )
        if preserve_marker.read_text(encoding="utf-8") != PRESERVE_MARKER_CONTENT:
            raise PackagingError("Reinstall changed external application data.")

        _run_uninstaller(uninstaller, timeout_seconds=timeout_seconds)
        _verify_uninstalled(
            install_directory,
            start_menu_directory,
            preserve_marker,
            installer.name,
            timeout_seconds=timeout_seconds,
        )
        completed = True
        result = {
            "status": "ok",
            "installer": str(installer),
            "installer_sha256": sha256_file(installer),
            "install_directory": str(install_directory),
            "external_data_preserved": True,
        }
    except ProcessTerminationError:
        preserve_scene = True
        raise
    finally:
        if completed:
            _cleanup_validation_root(validation_root, temporary_parent)
        elif not preserve_scene and _try_recover_owned_installation(
            install_directory,
            start_menu_directory,
            timeout_seconds=timeout_seconds,
        ):
            _cleanup_validation_root(validation_root, temporary_parent)
    return result


def main() -> int:
    args = _build_parser().parse_args()
    if not args.confirm_system_changes:
        print(
            "Installation validation requires --confirm-system-changes.",
            file=sys.stderr,
        )
        return 2
    if args.timeout_seconds <= 0:
        print("--timeout-seconds must be greater than zero.", file=sys.stderr)
        return 2
    try:
        result = verify_windows_install(
            args.installer,
            timeout_seconds=args.timeout_seconds,
        )
    except (OSError, PackagingError) as exc:
        print(f"Windows installation validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
