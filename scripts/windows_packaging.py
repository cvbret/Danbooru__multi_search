"""Shared Windows packaging and installation-validation utilities."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_VERSION = "0.1.0"
DATABASE_SCHEMA_VERSION = 9
WINDOWS_RESOURCE_VERSION = "0.1.0.0"
WINDOWS_RESOURCE_VERSION_TUPLE = (0, 1, 0, 0)
PYINSTALLER_VERSION = "6.22.3"
NSIS_VERSION = "3.13"
NSIS_DOWNLOAD_URL = (
    "https://downloads.sourceforge.net/project/nsis/NSIS%203/3.13/"
    "nsis-3.13.zip"
)
NSIS_SHA256 = "ba63dffc4410ee89193e1cb5a41989991bd77c61068da17e3156d136b7b0b3d8"

APPLICATION_BASENAME = "DanbooruMultiSearch"
APPLICATION_EXE = f"{APPLICATION_BASENAME}.exe"
APPLICATION_DISPLAY_NAME = "Danbooru Multi Search"
PUBLISHER = "Danbooru Multi Search Contributors"
INSTALLER_NAME = f"{APPLICATION_BASENAME}-{PACKAGE_VERSION}-win64-setup.exe"
UNINSTALLER_NAME = "Uninstall.exe"
INSTALL_MARKER_NAME = ".danbooru-multi-search-install"
INSTALL_MARKER_CONTENT = f"{APPLICATION_BASENAME}|{PACKAGE_VERSION}|windows-amd64"
BUILD_OWNER_MARKER = ".danbooru-multi-search-build-owner"
BUILD_OWNER_CONTENT = f"{APPLICATION_BASENAME}|{PACKAGE_VERSION}|onedir"

APPLICATION_REGISTRY_KEY = r"Software\DanbooruMultiSearch"
UNINSTALL_REGISTRY_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall\DanbooruMultiSearch"
)
START_MENU_DIRECTORY = "Danbooru Multi Search"
START_MENU_SHORTCUT = "Danbooru Multi Search.lnk"

HEADLESS_RESULT_FIELDS = frozenset(
    {
        "status",
        "mode",
        "version",
        "platform_name",
        "schema_version",
        "settings_created",
        "data_directory",
    }
)
GUI_RESULT_FIELDS = frozenset(
    {
        "status",
        "mode",
        "version",
        "schema_version",
        "qml_loaded",
        "shutdown_completed",
        "platform_name",
        "tls_backends",
        "supported_image_formats",
        "data_directory",
    }
)

QML_FILES = (
    "ActionButton.qml",
    "AppTheme.qml",
    "AppearancePanel.qml",
    "ApplicationBackground.qml",
    "PostCard.qml",
    "GalleryGrid.qml",
    "M2Main.qml",
    "FavoritesPage.qml",
    "FavoriteTransferDialog.qml",
    "ArtistCombinationPanel.qml",
    "SavedQueryPanel.qml",
    "DownloadsPage.qml",
    "DownloadRequestDialog.qml",
    "FavoriteObjectCard.qml",
    "FavoriteConfirmDialog.qml",
    "DetailPage.qml",
    "ImageViewer.qml",
    "QueryEditorPanel.qml",
    "QueryFilterPanel.qml",
    "QueryStatusBar.qml",
    "HistoryPanel.qml",
)
REQUIRED_PLUGIN_FILES = (
    ("platforms", "qwindows.dll"),
    ("platforms", "qoffscreen.dll"),
    ("tls", "qschannelbackend.dll"),
    ("imageformats", "qgif.dll"),
    ("imageformats", "qjpeg.dll"),
    ("imageformats", "qwebp.dll"),
)
ALLOWED_QML_DIRECT_MODULE_DIRECTORIES = frozenset(
    {"qtqml", "qtquick"}
)
ALLOWED_QML_RECURSIVE_MODULE_DIRECTORIES = (
    "qt/labs/folderlistmodel",
    "qtqml/models",
    "qtqml/workerscript",
    "qtquick/controls",
    "qtquick/dialogs",
    "qtquick/effects",
    "qtquick/layouts",
    "qtquick/nativestyle",
    "qtquick/shapes",
    "qtquick/templates",
    "qtquick/window",
)
FORBIDDEN_QT_FILE_PREFIXES = (
    "qt3d",
    "qt63d",
    "qtcharts",
    "qt6charts",
    "qtdatavisualization",
    "qt6datavisualization",
    "qtgraphs",
    "qt6graphs",
    "qtlocation",
    "qt6location",
    "qtmultimedia",
    "qt6multimedia",
    "qtpdf",
    "qt6pdf",
    "qtpositioning",
    "qt6positioning",
    "qtquick3d",
    "qt6quick3d",
    "qtspatialaudio",
    "qt6spatialaudio",
    "qtwebchannel",
    "qt6webchannel",
    "qtwebengine",
    "qt6webengine",
    "qtwebsockets",
    "qt6websockets",
    "qtwebview",
    "qt6webview",
)


class PackagingError(RuntimeError):
    """Raised when a Windows artifact violates the packaging contract."""


class ProcessTerminationError(PackagingError):
    """Raised when an owned timed-out process cannot be confirmed stopped."""


def native_windows_machine() -> int:
    """Return the native Windows image machine, including under emulation."""

    if os.name != "nt":
        raise PackagingError("Native Windows architecture requires Windows.")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process_machine = ctypes.c_ushort()
    native_machine = ctypes.c_ushort()
    try:
        is_wow64_process2 = kernel32.IsWow64Process2
    except AttributeError as exc:
        raise PackagingError(
            "Windows IsWow64Process2 is required to verify native AMD64."
        ) from exc
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    is_wow64_process2.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_ushort),
        ctypes.POINTER(ctypes.c_ushort),
    ]
    is_wow64_process2.restype = wintypes.BOOL
    if not is_wow64_process2(
        get_current_process(),
        ctypes.byref(process_machine),
        ctypes.byref(native_machine),
    ):
        error = ctypes.get_last_error()
        raise PackagingError(
            f"Unable to read native Windows architecture (error {error})."
        )
    return int(native_machine.value)


def normalize_windows_path(path: Path | str) -> str:
    """Return a stable comparison form for an absolute Windows path."""

    return os.path.normcase(os.path.abspath(os.fspath(path))).rstrip("\\/")


def ensure_windows_build_environment() -> None:
    """Reject unsupported builders before creating any output."""

    if sys.platform != "win32" or os.name != "nt":
        raise PackagingError("Windows packaging requires native Windows.")
    if native_windows_machine() != 0x8664:
        raise PackagingError("Windows packaging requires native AMD64 Windows.")
    if platform.machine().upper() not in {"AMD64", "X86_64"}:
        raise PackagingError("Windows packaging requires native AMD64 Python.")
    if struct.calcsize("P") != 8:
        raise PackagingError("Windows packaging requires a 64-bit Python process.")
    if sys.version_info[:2] != (3, 11):
        raise PackagingError("Windows packaging requires Python 3.11.")

    try:
        pyside_version = importlib.metadata.version("PySide6")
    except importlib.metadata.PackageNotFoundError as exc:
        raise PackagingError("PySide6 6.12 is required for packaging.") from exc
    if tuple(int(part) for part in pyside_version.split(".")[:2]) != (6, 12):
        raise PackagingError(
            f"PySide6 6.12 is required; found {pyside_version}."
        )

    try:
        pyinstaller_version = importlib.metadata.version("PyInstaller")
    except importlib.metadata.PackageNotFoundError as exc:
        raise PackagingError(
            f"PyInstaller {PYINSTALLER_VERSION} is required."
        ) from exc
    if pyinstaller_version != PYINSTALLER_VERSION:
        raise PackagingError(
            f"PyInstaller {PYINSTALLER_VERSION} is required; "
            f"found {pyinstaller_version}."
        )


def _terminate_owned_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            termination = subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProcessTerminationError(
                f"Timed-out process tree {process.pid} could not be stopped."
            ) from exc
        if termination.returncode != 0:
            raise ProcessTerminationError(
                f"Timed-out process tree {process.pid} could not be stopped."
            )
    else:
        process.kill()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired as exc:
        raise ProcessTerminationError(
            f"Timed-out process tree {process.pid} could not be stopped."
        ) from exc


def run_limited(
    command: Sequence[str | os.PathLike[str]] | str,
    *,
    cwd: Path,
    timeout_seconds: float,
    environment: Mapping[str, str] | None = None,
    executable: Path | None = None,
    encoding: str = "utf-8",
) -> subprocess.CompletedProcess[str]:
    """Run one owned process with a finite timeout and captured text output."""

    creationflags = (
        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(environment) if environment is not None else None,
        executable=os.fspath(executable) if executable is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding=encoding,
        errors="replace",
        creationflags=creationflags,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _terminate_owned_process_tree(process)
        raise PackagingError(
            f"Process timed out after {timeout_seconds:g} seconds: {command}"
        ) from exc
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if result.returncode != 0:
        detail = (stderr or stdout).strip()
        raise PackagingError(
            f"Process exited with {result.returncode}: {command}"
            + (f"\n{detail}" if detail else "")
        )
    return result


def find_makensis(
    explicit: Path | None,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Find makensis without installing or changing system software."""

    env = dict(os.environ if environment is None else environment)
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    configured = env.get("MAKENSIS") or env.get("NSIS_MAKENSIS")
    if configured:
        candidates.append(Path(configured))
    from_path = shutil.which("makensis.exe", path=env.get("PATH"))
    if from_path:
        candidates.append(Path(from_path))
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        root = env.get(variable)
        if root:
            candidates.append(Path(root) / "NSIS" / "makensis.exe")

    for candidate in candidates:
        absolute = candidate.expanduser().absolute()
        if absolute.is_file():
            return absolute
    raise PackagingError(
        "NSIS makensis.exe was not found. Pass --makensis or set MAKENSIS."
    )


def verify_makensis_version(makensis: Path, *, timeout_seconds: float = 30) -> None:
    result = run_limited(
        [makensis, "/VERSION"],
        cwd=PROJECT_ROOT,
        timeout_seconds=timeout_seconds,
    )
    reported = (result.stdout or result.stderr).strip().lstrip("vV")
    if reported != NSIS_VERSION:
        raise PackagingError(
            f"NSIS {NSIS_VERSION} is required; found {reported or 'unknown'}."
        )


def validated_child(path: Path, parent: Path) -> Path:
    """Resolve a path and require it to be a strict child of its parent."""

    resolved_parent = parent.resolve()
    resolved_path = path.resolve()
    if resolved_path == resolved_parent or resolved_parent not in resolved_path.parents:
        raise PackagingError(f"Unsafe path outside {resolved_parent}: {resolved_path}")
    return resolved_path


def remove_owned_tree(path: Path, parent: Path, marker_name: str, marker: str) -> None:
    """Remove only a verified build-owned directory."""

    target = validated_child(path, parent)
    marker_path = target / marker_name
    if not target.is_dir() or not marker_path.is_file():
        raise PackagingError(f"Refusing to remove unowned directory: {target}")
    try:
        actual = marker_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PackagingError(f"Unable to read ownership marker: {marker_path}") from exc
    if actual != marker:
        raise PackagingError(f"Ownership marker does not match: {marker_path}")
    shutil.rmtree(target)


def read_pe_headers(executable: Path) -> tuple[int, int, int]:
    """Return PE machine, optional-header magic, and subsystem."""

    try:
        with executable.open("rb") as stream:
            dos_header = stream.read(64)
            if len(dos_header) != 64 or dos_header[:2] != b"MZ":
                raise PackagingError(f"Not a Windows executable: {executable}")
            pe_offset = struct.unpack_from("<I", dos_header, 0x3C)[0]
            stream.seek(pe_offset)
            header = stream.read(24)
            if len(header) != 24 or header[:4] != b"PE\0\0":
                raise PackagingError(f"Invalid PE header: {executable}")
            machine = struct.unpack_from("<H", header, 4)[0]
            optional_size = struct.unpack_from("<H", header, 20)[0]
            optional = stream.read(optional_size)
    except OSError as exc:
        raise PackagingError(f"Unable to inspect executable {executable}: {exc}") from exc
    if len(optional) < 70:
        raise PackagingError(f"Truncated PE optional header: {executable}")
    magic = struct.unpack_from("<H", optional, 0)[0]
    subsystem = struct.unpack_from("<H", optional, 68)[0]
    return machine, magic, subsystem


def _read_fixed_versions(
    executable: Path,
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """Read file and product versions from native VS_FIXEDFILEINFO."""

    if os.name != "nt":
        raise PackagingError("Windows version resources require Windows.")
    version = ctypes.windll.version
    handle = ctypes.c_uint32()
    size = version.GetFileVersionInfoSizeW(str(executable), ctypes.byref(handle))
    if not size:
        raise PackagingError(f"Version resource is missing: {executable}")
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(executable), 0, size, buffer):
        raise PackagingError(f"Unable to read version resource: {executable}")
    pointer = ctypes.c_void_p()
    length = ctypes.c_uint32()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        raise PackagingError(f"VS_FIXEDFILEINFO is missing: {executable}")

    class FixedInfo(ctypes.Structure):
        _fields_ = [
            ("signature", ctypes.c_uint32),
            ("struct_version", ctypes.c_uint32),
            ("file_ms", ctypes.c_uint32),
            ("file_ls", ctypes.c_uint32),
            ("product_ms", ctypes.c_uint32),
            ("product_ls", ctypes.c_uint32),
        ]

    info = ctypes.cast(pointer, ctypes.POINTER(FixedInfo)).contents
    if info.signature != 0xFEEF04BD:
        raise PackagingError(f"Invalid VS_FIXEDFILEINFO signature: {executable}")
    return (
        (
            info.file_ms >> 16,
            info.file_ms & 0xFFFF,
            info.file_ls >> 16,
            info.file_ls & 0xFFFF,
        ),
        (
            info.product_ms >> 16,
            info.product_ms & 0xFFFF,
            info.product_ls >> 16,
            info.product_ls & 0xFFFF,
        ),
    )


def read_fixed_file_version(executable: Path) -> tuple[int, int, int, int]:
    return _read_fixed_versions(executable)[0]


def read_fixed_product_version(executable: Path) -> tuple[int, int, int, int]:
    return _read_fixed_versions(executable)[1]


def read_version_string(executable: Path, name: str) -> str:
    """Read one fixed English Unicode version-resource string."""

    if os.name != "nt":
        raise PackagingError("Windows version resources require Windows.")
    version = ctypes.windll.version
    handle = ctypes.c_uint32()
    size = version.GetFileVersionInfoSizeW(str(executable), ctypes.byref(handle))
    if not size:
        raise PackagingError(f"Version resource is missing: {executable}")
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(executable), 0, size, buffer):
        raise PackagingError(f"Unable to read version resource: {executable}")
    pointer = ctypes.c_void_p()
    length = ctypes.c_uint32()
    translation_query = "\\VarFileInfo\\Translation"
    if not version.VerQueryValueW(
        buffer,
        translation_query,
        ctypes.byref(pointer),
        ctypes.byref(length),
    ) or length.value < 4:
        raise PackagingError(
            f"Version translation is missing: {executable}"
        )
    translation = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ushort * 2)).contents
    table = f"{translation[0]:04X}{translation[1]:04X}"
    query = f"\\StringFileInfo\\{table}\\{name}"
    if not version.VerQueryValueW(
        buffer,
        query,
        ctypes.byref(pointer),
        ctypes.byref(length),
    ):
        raise PackagingError(f"Version string {name} is missing: {executable}")
    return ctypes.wstring_at(pointer, max(0, length.value - 1))


def verify_executable(executable: Path) -> None:
    machine, magic, subsystem = read_pe_headers(executable)
    if machine != 0x8664:
        raise PackagingError(f"Executable is not AMD64: machine=0x{machine:04x}")
    if magic != 0x20B:
        raise PackagingError(f"Executable is not PE32+: magic=0x{magic:04x}")
    if subsystem != 2:
        raise PackagingError(
            f"Executable does not use the Windows GUI subsystem: {subsystem}"
        )
    actual_version = read_fixed_file_version(executable)
    if actual_version != WINDOWS_RESOURCE_VERSION_TUPLE:
        raise PackagingError(
            f"Executable version is {actual_version}; expected "
            f"{WINDOWS_RESOURCE_VERSION_TUPLE}."
        )
    product_version = read_fixed_product_version(executable)
    if product_version != WINDOWS_RESOURCE_VERSION_TUPLE:
        raise PackagingError(
            f"Executable product version is {product_version}; expected "
            f"{WINDOWS_RESOURCE_VERSION_TUPLE}."
        )
    expected_strings = {
        "FileVersion": WINDOWS_RESOURCE_VERSION,
        "ProductVersion": WINDOWS_RESOURCE_VERSION,
        "ProductName": APPLICATION_DISPLAY_NAME,
        "InternalName": APPLICATION_EXE,
        "OriginalFilename": APPLICATION_EXE,
    }
    for name, expected in expected_strings.items():
        actual = read_version_string(executable, name)
        if actual != expected:
            raise PackagingError(
                f"Executable {name} is {actual!r}; expected {expected!r}."
            )


def verify_installer(installer: Path) -> None:
    """Verify the fixed installer name and both Windows resource versions."""

    if installer.name != INSTALLER_NAME:
        raise PackagingError(
            f"Unexpected installer name {installer.name}; expected {INSTALLER_NAME}."
        )
    file_version = read_fixed_file_version(installer)
    product_version = read_fixed_product_version(installer)
    if (
        file_version != WINDOWS_RESOURCE_VERSION_TUPLE
        or product_version != WINDOWS_RESOURCE_VERSION_TUPLE
    ):
        raise PackagingError(
            "Installer file and product versions must both be "
            f"{WINDOWS_RESOURCE_VERSION_TUPLE}; found "
            f"{file_version} and {product_version}."
        )
    expected_strings = {
        "FileVersion": WINDOWS_RESOURCE_VERSION,
        "ProductVersion": WINDOWS_RESOURCE_VERSION,
        "ProductName": APPLICATION_DISPLAY_NAME,
    }
    for name, expected in expected_strings.items():
        actual = read_version_string(installer, name)
        if actual != expected:
            raise PackagingError(
                f"Installer {name} is {actual!r}; expected {expected!r}."
            )


def verify_onedir(bundle: Path) -> Path:
    """Verify the executable, project QML, Qt QML tree, and runtime plugins."""

    if not bundle.is_dir():
        raise PackagingError(f"Onedir bundle is missing: {bundle}")
    executable = bundle / APPLICATION_EXE
    if not executable.is_file():
        raise PackagingError(f"Application executable is missing: {executable}")
    verify_executable(executable)

    entries = list(bundle.rglob("*"))
    files = [path for path in entries if path.is_file()]
    lower_names: dict[str, list[Path]] = {}
    for path in files:
        lower_names.setdefault(path.name.casefold(), []).append(path)
    for qml_name in QML_FILES:
        matches = [
            path
            for path in lower_names.get(qml_name.casefold(), [])
            if "danbooru_multi_search" in {part.casefold() for part in path.parts}
        ]
        if len(matches) != 1:
            raise PackagingError(
                f"Bundle must contain one project QML file {qml_name}; "
                f"found {len(matches)}."
            )
    if not any(
        path.is_dir() and path.name == "QtQuick" and path.parent.name == "qml"
        for path in bundle.rglob("QtQuick")
    ):
        raise PackagingError("Qt Quick QML runtime directory is missing.")
    for directory, plugin in REQUIRED_PLUGIN_FILES:
        matches = lower_names.get(plugin.casefold(), [])
        if not any(path.parent.name.casefold() == directory for path in matches):
            raise PackagingError(
                f"Required Qt plugin is missing from {directory}: {plugin}"
            )
    for path in entries:
        relative_parts = tuple(
            part.casefold() for part in path.relative_to(bundle).parts
        )
        qml_root_index: int | None = None
        for index in range(len(relative_parts) - 1):
            if relative_parts[index : index + 2] == ("pyside6", "qml"):
                qml_root_index = index + 2
                break
        if qml_root_index is None or qml_root_index >= len(relative_parts):
            continue
        module_parts = relative_parts[qml_root_index:]
        if path.is_file():
            module_parts = module_parts[:-1]
        module_directory = "/".join(module_parts)
        if not module_directory:
            continue
        if module_directory in ALLOWED_QML_DIRECT_MODULE_DIRECTORIES:
            continue
        if any(
            module_directory == allowed
            or module_directory.startswith(allowed + "/")
            for allowed in ALLOWED_QML_RECURSIVE_MODULE_DIRECTORIES
        ):
            continue
        if path.is_dir() and any(
            allowed.startswith(module_directory + "/")
            for allowed in ALLOWED_QML_RECURSIVE_MODULE_DIRECTORIES
        ):
            continue
        raise PackagingError(
            "Unused Qt QML module is present in the bundle: "
            f"{path.relative_to(bundle)}"
        )
    for path in files:
        filename = path.name.casefold()
        if filename == "icuuc.dll" or (
            filename.startswith("icudt") and filename.endswith(".dll")
        ):
            raise PackagingError(
                "An external ICU runtime is present in the bundle: "
                f"{path.relative_to(bundle)}"
            )
        if filename.startswith(FORBIDDEN_QT_FILE_PREFIXES):
            raise PackagingError(
                "Unused Qt runtime file is present in the bundle: "
                f"{path.relative_to(bundle)}"
            )
    return executable


def clean_pyinstaller_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Remove non-system PATH entries that can shadow Windows ICU at analysis."""

    cleaned = dict(os.environ if environment is None else environment)
    path_value = cleaned.get("PATH")
    if not path_value:
        return cleaned
    system_root_value = cleaned.get("SystemRoot") or cleaned.get("WINDIR")
    system_root = (
        Path(system_root_value).resolve()
        if system_root_value
        else None
    )
    retained: list[str] = []
    for raw_entry in path_value.split(os.pathsep):
        if not raw_entry:
            retained.append(raw_entry)
            continue
        try:
            directory = Path(raw_entry).resolve()
            inside_system_root = system_root is not None and (
                directory == system_root or system_root in directory.parents
            )
            has_external_icu = (
                (directory / "icuuc.dll").is_file()
                and any(directory.glob("icudt*.dll"))
            )
        except OSError:
            retained.append(raw_entry)
            continue
        if has_external_icu and not inside_system_root:
            continue
        retained.append(raw_entry)
    cleaned["PATH"] = os.pathsep.join(retained)
    return cleaned


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise PackagingError(f"Unable to hash {path}: {exc}") from exc
    return digest.hexdigest()


def clean_application_environment(*, offscreen: bool) -> dict[str, str]:
    """Remove Python and Qt discovery overrides from an executable check."""

    environment = os.environ.copy()
    for name in tuple(environment):
        upper = name.upper()
        if upper.startswith("PYTHON"):
            environment.pop(name, None)
        elif upper.startswith("QML_") or upper == "QML2_IMPORT_PATH":
            environment.pop(name, None)
        elif upper.startswith("QT_PLUGIN") or upper in {
            "QT_QPA_PLATFORM",
            "QT_QPA_PLATFORM_PLUGIN_PATH",
            "QT_QUICK_BACKEND",
        }:
            environment.pop(name, None)
    if offscreen:
        environment["QT_QPA_PLATFORM"] = "offscreen"
        environment["QT_QUICK_BACKEND"] = "software"
        environment["LIBGL_ALWAYS_SOFTWARE"] = "1"
    return environment


def _load_check_result(result_path: Path, expected_fields: frozenset[str]) -> dict:
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackagingError(f"Invalid check result {result_path}: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise PackagingError(
            f"Unexpected check fields in {result_path}: "
            f"{sorted(payload) if isinstance(payload, dict) else type(payload).__name__}"
        )
    if payload.get("status") != "ok":
        raise PackagingError(f"Application check failed: {payload}")
    return payload


def run_application_checks(
    executable: Path,
    *,
    working_directory: Path,
    check_root: Path,
    label: str,
    timeout_seconds: float = 60,
) -> tuple[dict, dict, dict]:
    """Run headless, native GUI, and offscreen GUI checks with unique files."""

    working_directory.mkdir(parents=True, exist_ok=True)
    check_root.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    definitions = (
        ("headless", "--headless-check", False, HEADLESS_RESULT_FIELDS),
        ("windows", "--gui-smoke-check", False, GUI_RESULT_FIELDS),
        ("offscreen", "--gui-smoke-check", True, GUI_RESULT_FIELDS),
    )
    for index, (name, mode, offscreen, fields) in enumerate(definitions, start=1):
        data_root = check_root / f"{label}-{index}-{name}-数据 目录"
        result_path = check_root / f"{label}-{index}-{name}-结果.json"
        if data_root.exists() or os.path.lexists(result_path):
            raise PackagingError(f"Check path already exists: {data_root} or {result_path}")
        run_limited(
            [
                executable,
                mode,
                "--data-root",
                data_root,
                "--check-output",
                result_path,
            ],
            cwd=working_directory,
            timeout_seconds=timeout_seconds,
            environment=clean_application_environment(offscreen=offscreen),
        )
        payload = _load_check_result(result_path, fields)
        if (
            payload["version"] != PACKAGE_VERSION
            or payload["schema_version"] != DATABASE_SCHEMA_VERSION
        ):
            raise PackagingError(f"Check reported unexpected version data: {payload}")
        if normalize_windows_path(payload["data_directory"]) != normalize_windows_path(
            data_root / "data"
        ):
            raise PackagingError(f"Check used an unexpected data directory: {payload}")
        if name == "headless":
            if (
                payload["mode"] != "headless"
                or payload["platform_name"] != "windows"
                or payload["settings_created"] is not True
            ):
                raise PackagingError(f"Unexpected headless result: {payload}")
        else:
            expected_platform = "offscreen" if offscreen else "windows"
            if payload["mode"] != "gui" or payload["platform_name"] != expected_platform:
                raise PackagingError(f"Unexpected GUI platform result: {payload}")
            backends = {str(value).casefold() for value in payload["tls_backends"]}
            formats = {
                str(value).casefold() for value in payload["supported_image_formats"]
            }
            if "schannel" not in backends:
                raise PackagingError("GUI check did not report the Schannel backend.")
            if not {"png", "gif", "webp"} <= formats or not ({"jpg", "jpeg"} & formats):
                raise PackagingError("GUI check did not report every image format.")
            if not payload["qml_loaded"] or not payload["shutdown_completed"]:
                raise PackagingError(f"GUI lifecycle check failed: {payload}")
        results.append(payload)
    return results[0], results[1], results[2]


def wait_for_absence(
    predicate,
    *,
    timeout_seconds: float,
    description: str,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while predicate():
        if time.monotonic() >= deadline:
            raise PackagingError(f"Timed out waiting for {description} to disappear.")
        time.sleep(0.1)
