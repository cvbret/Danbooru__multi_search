from __future__ import annotations

import json
import os
import re
import struct
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts import build_windows_installer as build_script
from scripts import verify_windows_install as install_verifier
from scripts import windows_packaging


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = PROJECT_ROOT / "packaging" / "windows" / "DanbooruMultiSearch.spec"
VERSION_PATH = PROJECT_ROOT / "packaging" / "windows" / "version_info.txt"
NSIS_PATH = PROJECT_ROOT / "packaging" / "windows" / "installer.nsi"


def _function_body(source: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^Function {re.escape(name)}\s*$\n(.*?)^FunctionEnd\s*$",
        source,
    )
    assert match is not None, f"missing NSIS function {name}"
    return match.group(1)


def _section_body(source: str, name: str) -> str:
    match = re.search(
        rf'(?ms)^Section "{re.escape(name)}"(?:\s+\S+)?\s*$\n(.*?)^SectionEnd\s*$',
        source,
    )
    assert match is not None, f"missing NSIS section {name}"
    return match.group(1)


def _write_synthetic_pe(
    path: Path,
    *,
    machine: int = 0x8664,
    magic: int = 0x20B,
    subsystem: int = 2,
) -> None:
    dos_header = bytearray(64)
    dos_header[:2] = b"MZ"
    struct.pack_into("<I", dos_header, 0x3C, 0x80)
    pe_header = bytearray(24)
    pe_header[:4] = b"PE\0\0"
    struct.pack_into("<H", pe_header, 4, machine)
    struct.pack_into("<H", pe_header, 20, 70)
    optional_header = bytearray(70)
    struct.pack_into("<H", optional_header, 0, magic)
    struct.pack_into("<H", optional_header, 68, subsystem)
    path.write_bytes(
        bytes(dos_header)
        + bytes(0x80 - len(dos_header))
        + bytes(pe_header)
        + bytes(optional_header)
    )


def _make_onedir_bundle(root: Path) -> Path:
    bundle = root / windows_packaging.APPLICATION_BASENAME
    executable = bundle / windows_packaging.APPLICATION_EXE
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic executable")

    qml_root = (
        bundle
        / "_internal"
        / "danbooru_multi_search"
        / "ui"
        / "qml"
    )
    qml_root.mkdir(parents=True)
    for name in windows_packaging.QML_FILES:
        (qml_root / name).write_text("import QtQuick\nItem {}\n", encoding="utf-8")
    (bundle / "_internal" / "PySide6" / "Qt" / "qml" / "QtQuick").mkdir(
        parents=True
    )
    plugin_root = bundle / "_internal" / "PySide6" / "Qt" / "plugins"
    for directory, name in windows_packaging.REQUIRED_PLUGIN_FILES:
        plugin = plugin_root / directory / name
        plugin.parent.mkdir(parents=True, exist_ok=True)
        plugin.write_bytes(b"plugin")
    return bundle


def test_fixed_packaging_versions_names_and_tool_source() -> None:
    assert windows_packaging.PACKAGE_VERSION == "0.1.0"
    assert windows_packaging.WINDOWS_RESOURCE_VERSION == "0.1.0.0"
    assert windows_packaging.WINDOWS_RESOURCE_VERSION_TUPLE == (0, 1, 0, 0)
    assert windows_packaging.PYINSTALLER_VERSION == "6.22.3"
    assert windows_packaging.NSIS_VERSION == "3.13"
    assert windows_packaging.NSIS_DOWNLOAD_URL == (
        "https://downloads.sourceforge.net/project/nsis/NSIS%203/3.13/"
        "nsis-3.13.zip"
    )
    assert windows_packaging.NSIS_SHA256 == (
        "ba63dffc4410ee89193e1cb5a41989991bd77c61068da17e3156d136b7b0b3d8"
    )
    assert windows_packaging.APPLICATION_EXE == "DanbooruMultiSearch.exe"
    assert windows_packaging.INSTALLER_NAME == (
        "DanbooruMultiSearch-0.1.0-win64-setup.exe"
    )
    assert windows_packaging.INSTALL_MARKER_NAME == (
        ".danbooru-multi-search-install"
    )
    assert windows_packaging.INSTALL_MARKER_CONTENT == (
        "DanbooruMultiSearch|0.1.0|windows-amd64"
    )


def test_pyproject_has_isolated_fixed_windows_build_dependency() -> None:
    payload = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert payload["project"]["version"] == "0.1.0"
    assert payload["project"]["optional-dependencies"]["windows-build"] == [
        "PyInstaller==6.22.3"
    ]
    assert not any(
        requirement.casefold().startswith("pyinstaller")
        for requirement in payload["project"]["dependencies"]
    )


def test_pyinstaller_spec_is_onedir_windowed_amd64_with_project_resources() -> None:
    source = SPEC_PATH.read_text(encoding="utf-8")

    assert "COLLECT(" in source
    assert "exclude_binaries=True" in source
    assert "console=False" in source
    assert 'target_arch="x86_64"' in source
    assert 'name="DanbooruMultiSearch"' in source
    assert 'package_root / "__main__.py"' in source
    assert "version=str(version_file)" in source
    for qml_name in windows_packaging.QML_FILES:
        assert f'qml_root / "{qml_name}"' in source
    for hidden_import in (
        "ctypes.wintypes",
        "winreg",
        "danbooru_multi_search.integrations.eagle.client",
        "danbooru_multi_search.platform.windows_wallpaper",
        "PySide6.QtQuick",
        "PySide6.QtQuickControls2",
        "PySide6.QtQml",
        "PySide6.QtNetwork",
    ):
        assert f'"{hidden_import}"' in source


def test_pyinstaller_spec_excludes_unused_large_qt_modules() -> None:
    source = SPEC_PATH.read_text(encoding="utf-8")

    for module in (
        "PySide6.Qt3DCore",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtLocation",
        "PySide6.QtMultimedia",
        "PySide6.QtPdf",
        "PySide6.QtPositioning",
        "PySide6.QtRemoteObjects",
        "PySide6.QtSensors",
        "PySide6.QtSerialPort",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebSockets",
    ):
        assert f'"{module}"' in source


def test_version_resource_has_fixed_product_and_file_identity() -> None:
    source = VERSION_PATH.read_text(encoding="utf-8")

    assert "filevers=(0, 1, 0, 0)" in source
    assert "prodvers=(0, 1, 0, 0)" in source
    expected_strings = {
        "CompanyName": "Danbooru Multi Search Contributors",
        "FileDescription": "Danbooru Multi Search",
        "FileVersion": "0.1.0.0",
        "InternalName": "DanbooruMultiSearch.exe",
        "OriginalFilename": "DanbooruMultiSearch.exe",
        "ProductName": "Danbooru Multi Search",
        "ProductVersion": "0.1.0.0",
    }
    for name, value in expected_strings.items():
        assert f"StringStruct(u'{name}', u'{value}')" in source


def test_nsis_uses_current_user_unicode_64_bit_amd64_contract() -> None:
    source = NSIS_PATH.read_text(encoding="utf-8")

    assert re.search(r"(?m)^Unicode true\s*$", source)
    assert re.search(r"(?m)^RequestExecutionLevel user\s*$", source)
    assert source.count("SetShellVarContext current") >= 4
    assert source.count("SetRegView 64") >= 4
    assert "GetNativeSystemInfo" in source
    assert "PROCESSOR_ARCHITECTURE_AMD64" in source
    assert "requires native AMD64 Windows" in source
    assert "HKLM" not in source
    assert "$DESKTOP" not in source


def test_nsis_fixed_paths_marker_registry_shortcut_and_artifact() -> None:
    source = NSIS_PATH.read_text(encoding="utf-8")

    for statement in (
        '!define PRODUCT_VERSION "0.1.0"',
        '!define PRODUCT_NAME "Danbooru Multi Search"',
        '!define PRODUCT_PUBLISHER "Danbooru Multi Search Contributors"',
        '!define PRODUCT_KEY "Software\\DanbooruMultiSearch"',
        '!define UNINSTALL_KEY "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\DanbooruMultiSearch"',
        '!define START_MENU_DIR "Danbooru Multi Search"',
        '!define START_MENU_LINK "Danbooru Multi Search.lnk"',
        '!define MARKER_FILE ".danbooru-multi-search-install"',
        '!define MARKER_VALUE "DanbooruMultiSearch|0.1.0|windows-amd64"',
        '!define INSTALLER_FILE "DanbooruMultiSearch-0.1.0-win64-setup.exe"',
        'InstallDir "$LOCALAPPDATA\\Programs\\DanbooruMultiSearch"',
        'OutFile "${OUTPUT_DIR}\\${INSTALLER_FILE}"',
        'CreateShortcut "$SMPROGRAMS\\${START_MENU_DIR}\\${START_MENU_LINK}" "$INSTDIR\\app\\${PRODUCT_EXE}"',
    ):
        assert statement in source
    assert 'VIProductVersion "0.1.0.0"' in source
    assert 'VIAddVersionKey /LANG=1033 "ProductVersion" "0.1.0.0"' in source
    assert 'VIAddVersionKey /LANG=1033 "FileVersion" "0.1.0.0"' in source


def test_nsis_first_install_requires_empty_target_and_absent_both_registry_keys() -> None:
    source = NSIS_PATH.read_text(encoding="utf-8")
    on_init = _function_body(source, ".onInit")
    validate_absent = _function_body(source, "ValidateRegistryAbsent")

    assert on_init.index("Call CheckNativeAmd64") < on_init.index(
        "Call NormalizeInstallDirectory"
    )
    assert on_init.index("Call NormalizeInstallDirectory") < on_init.index(
        "Call ReadInstallRegistry"
    )
    assert on_init.count("Call ValidateRegistryAbsent") == 2
    assert "Call DirectoryHasEntries" in on_init
    assert on_init.index("Call ValidateMarker") < on_init.index(
        "Call ValidateRegistry\n"
    )
    assert validate_absent.count("Call RegistryKeyExists64") == 2
    assert 'Push "${PRODUCT_KEY}"' in validate_absent
    assert 'Push "${UNINSTALL_KEY}"' in validate_absent


def test_nsis_lifecycle_uses_marker_and_both_current_user_registry_keys() -> None:
    source = NSIS_PATH.read_text(encoding="utf-8")
    write_marker = _function_body(source, "WriteInstallMarker")
    install = _section_body(source, "Install")
    uninstall_init = _function_body(source, "un.onInit")
    uninstall = _section_body(source, "Uninstall")

    assert write_marker.index("ClearErrors") < write_marker.index(
        'FileOpen $0 "$INSTDIR\\${MARKER_FILE}" w'
    )
    assert write_marker.index('FileOpen $0 "$INSTDIR\\${MARKER_FILE}" w') < (
        write_marker.index('FileWrite $0 "${MARKER_VALUE}"')
    )
    assert "ClearErrors" in write_marker[
        write_marker.index('FileOpen $0 "$INSTDIR\\${MARKER_FILE}" w') :
        write_marker.index('FileWrite $0 "${MARKER_VALUE}"')
    ]
    assert "Call WriteInstallMarker" in install
    assert 'WriteRegStr HKCU "${PRODUCT_KEY}" "InstallLocation" "$INSTDIR"' in install
    assert 'WriteRegStr HKCU "${PRODUCT_KEY}" "DisplayVersion" "${PRODUCT_VERSION}"' in install
    for value in (
        "DisplayName",
        "DisplayVersion",
        "Publisher",
        "DisplayIcon",
        "InstallLocation",
        "UninstallString",
        "QuietUninstallString",
        "NoModify",
        "NoRepair",
        "EstimatedSize",
    ):
        assert f'"{value}"' in install
    assert uninstall_init.index("Call un.ValidateMarker") < uninstall_init.index(
        "Call un.ValidateRegistry"
    )
    assert 'DeleteRegKey HKCU "${PRODUCT_KEY}"' in uninstall
    assert 'DeleteRegKey HKCU "${UNINSTALL_KEY}"' in uninstall
    assert 'Delete "$SMPROGRAMS\\${START_MENU_DIR}\\${START_MENU_LINK}"' in uninstall


def test_nsis_recursive_deletion_is_limited_to_marked_app_directory() -> None:
    source = NSIS_PATH.read_text(encoding="utf-8")
    recursive_deletions = re.findall(r'(?m)^\s*RMDir\s+/r\s+"([^"]+)"\s*$', source)

    assert recursive_deletions == ["$INSTDIR\\app", "$INSTDIR\\app"]
    assert not re.search(r'(?m)^\s*RMDir\s+/r\s+"\$INSTDIR"\s*$', source)
    assert not re.search(r'(?mi)^\s*RMDir\s+/r\s+"\$(?:LOCALAPPDATA|APPDATA)', source)
    assert 'RMDir "$INSTDIR"' in source
    assert 'RMDir "$SMPROGRAMS\\${START_MENU_DIR}"' in source


def test_nsis_reinstall_and_uninstall_validate_before_recursive_deletion() -> None:
    source = NSIS_PATH.read_text(encoding="utf-8")
    on_init = _function_body(source, ".onInit")
    uninstall_init = _function_body(source, "un.onInit")

    assert "Call ValidateMarker" in on_init
    assert "Call ValidateRegistry" in on_init
    assert "Call un.ValidateMarker" in uninstall_init
    assert "Call un.ValidateRegistry" in uninstall_init
    assert 'RMDir /r "$INSTDIR\\app"' not in on_init
    assert 'RMDir /r "$INSTDIR\\app"' not in uninstall_init


def test_read_pe_headers_parses_synthetic_amd64_gui_image(tmp_path: Path) -> None:
    executable = tmp_path / "synthetic.exe"
    _write_synthetic_pe(executable)

    assert windows_packaging.read_pe_headers(executable) == (0x8664, 0x20B, 2)


@pytest.mark.parametrize(
    ("machine", "subsystem", "message"),
    (
        (0x14C, 2, "not AMD64"),
        (0x8664, 3, "GUI subsystem"),
    ),
)
def test_verify_executable_rejects_wrong_pe_identity_before_version_api(
    tmp_path: Path,
    machine: int,
    subsystem: int,
    message: str,
) -> None:
    executable = tmp_path / "synthetic.exe"
    _write_synthetic_pe(executable, machine=machine, subsystem=subsystem)

    with pytest.raises(windows_packaging.PackagingError, match=message):
        windows_packaging.verify_executable(executable)


def test_verify_installer_requires_fixed_name_and_both_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong_name = tmp_path / "renamed.exe"
    wrong_name.write_bytes(b"installer")
    with pytest.raises(windows_packaging.PackagingError, match="Unexpected installer"):
        windows_packaging.verify_installer(wrong_name)

    installer = tmp_path / windows_packaging.INSTALLER_NAME
    installer.write_bytes(b"installer")
    monkeypatch.setattr(
        windows_packaging,
        "read_fixed_file_version",
        lambda _path: windows_packaging.WINDOWS_RESOURCE_VERSION_TUPLE,
    )
    monkeypatch.setattr(
        windows_packaging,
        "read_fixed_product_version",
        lambda _path: windows_packaging.WINDOWS_RESOURCE_VERSION_TUPLE,
    )
    expected = {
        "FileVersion": "0.1.0.0",
        "ProductVersion": "0.1.0.0",
        "ProductName": "Danbooru Multi Search",
    }
    monkeypatch.setattr(
        windows_packaging,
        "read_version_string",
        lambda _path, name: expected[name],
    )

    windows_packaging.verify_installer(installer)


def test_verify_onedir_requires_qml_runtime_and_plugins_in_exact_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _make_onedir_bundle(tmp_path)
    monkeypatch.setattr(windows_packaging, "verify_executable", lambda _path: None)

    assert windows_packaging.verify_onedir(bundle) == (
        bundle / windows_packaging.APPLICATION_EXE
    )

    plugin = next(bundle.rglob("qwindows.dll"))
    misplaced = plugin.parent.parent / "wrong-platform-directory" / plugin.name
    misplaced.parent.mkdir()
    plugin.replace(misplaced)
    with pytest.raises(windows_packaging.PackagingError, match="platforms"):
        windows_packaging.verify_onedir(bundle)


@pytest.mark.parametrize(
    "qml_name",
    (
        "M2Main.qml",
        "QueryEditorPanel.qml",
        "QueryFilterPanel.qml",
        "QueryStatusBar.qml",
        "HistoryPanel.qml",
    ),
)
def test_verify_onedir_requires_each_m2_qml_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qml_name: str,
) -> None:
    bundle = _make_onedir_bundle(tmp_path)
    missing = next(
        path
        for path in bundle.rglob(qml_name)
        if "danbooru_multi_search" in {
            part.casefold() for part in path.parts
        }
    )
    missing.unlink()
    monkeypatch.setattr(windows_packaging, "verify_executable", lambda _path: None)

    with pytest.raises(windows_packaging.PackagingError, match=qml_name):
        windows_packaging.verify_onedir(bundle)


def test_find_makensis_accepts_explicit_file_and_reports_missing_tool(
    tmp_path: Path,
) -> None:
    explicit = tmp_path / "nsis" / "makensis.exe"
    explicit.parent.mkdir()
    explicit.write_bytes(b"tool")

    assert windows_packaging.find_makensis(
        explicit,
        environment={"PATH": ""},
    ) == explicit.absolute()
    with pytest.raises(windows_packaging.PackagingError, match="was not found"):
        windows_packaging.find_makensis(None, environment={"PATH": ""})


@pytest.mark.skipif(os.name != "nt", reason="Windows environment gate")
def test_environment_gate_reports_missing_pyinstaller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_packaging, "native_windows_machine", lambda: 0x8664)
    monkeypatch.setattr(windows_packaging.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(windows_packaging.struct, "calcsize", lambda _format: 8)

    def version(distribution: str) -> str:
        if distribution == "PySide6":
            return "6.12.0"
        raise windows_packaging.importlib.metadata.PackageNotFoundError(distribution)

    monkeypatch.setattr(windows_packaging.importlib.metadata, "version", version)

    with pytest.raises(windows_packaging.PackagingError, match="PyInstaller 6.22.3"):
        windows_packaging.ensure_windows_build_environment()


def test_owned_directory_removal_requires_strict_child_and_exact_marker(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    unowned = parent / "unowned"
    unowned.mkdir()
    with pytest.raises(windows_packaging.PackagingError, match="unowned"):
        windows_packaging.remove_owned_tree(
            unowned,
            parent,
            "owner.marker",
            "expected",
        )
    assert unowned.is_dir()

    mismatched = parent / "mismatched"
    mismatched.mkdir()
    (mismatched / "owner.marker").write_text("other", encoding="utf-8")
    with pytest.raises(windows_packaging.PackagingError, match="does not match"):
        windows_packaging.remove_owned_tree(
            mismatched,
            parent,
            "owner.marker",
            "expected",
        )
    assert mismatched.is_dir()

    owned = parent / "owned"
    owned.mkdir()
    (owned / "owner.marker").write_text("expected", encoding="utf-8")
    (owned / "payload.txt").write_text("payload", encoding="utf-8")
    windows_packaging.remove_owned_tree(
        owned,
        parent,
        "owner.marker",
        "expected",
    )
    assert not owned.exists()

    with pytest.raises(windows_packaging.PackagingError, match="Unsafe path"):
        windows_packaging.validated_child(parent, parent)


def test_clean_application_environment_removes_discovery_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overridden = {
        "PYTHONHOME": "python-home",
        "PYTHONPATH": "source-tree",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUSERBASE": "user-site",
        "PYTHONUTF8": "1",
        "QML_IMPORT_PATH": "qml",
        "QML2_IMPORT_PATH": "qml2",
        "QT_PLUGIN_PATH": "plugins",
        "QT_QPA_PLATFORM_PLUGIN_PATH": "platforms",
        "QT_QPA_PLATFORM": "windows",
        "QT_QUICK_BACKEND": "unexpected",
    }
    for name, value in overridden.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DANBOORU_TEST_KEEP", "kept")

    normal = windows_packaging.clean_application_environment(offscreen=False)
    offscreen = windows_packaging.clean_application_environment(offscreen=True)

    for name in overridden:
        assert name not in normal
    assert normal["DANBOORU_TEST_KEEP"] == "kept"
    assert offscreen["QT_QPA_PLATFORM"] == "offscreen"
    assert offscreen["QT_QUICK_BACKEND"] == "software"
    assert offscreen["LIBGL_ALWAYS_SOFTWARE"] == "1"


def test_pyinstaller_environment_removes_external_icu_path(
    tmp_path: Path,
) -> None:
    system_root = tmp_path / "Windows"
    system_directory = system_root / "System32"
    external_icu = tmp_path / "external" / "poppler" / "bin"
    safe_directory = tmp_path / "safe"
    for directory in (system_directory, external_icu, safe_directory):
        directory.mkdir(parents=True)
    for directory in (system_directory, external_icu):
        (directory / "icuuc.dll").write_bytes(b"icu")
        (directory / "icudt78.dll").write_bytes(b"data")
    environment = {
        "PATH": os.pathsep.join(
            (str(external_icu), str(system_directory), str(safe_directory))
        ),
        "SystemRoot": str(system_root),
    }

    cleaned = windows_packaging.clean_pyinstaller_environment(environment)

    assert cleaned["PATH"].split(os.pathsep) == [
        str(system_directory),
        str(safe_directory),
    ]


def test_application_checks_use_unique_results_fixed_fields_and_data_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "portable" / windows_packaging.APPLICATION_EXE
    executable.parent.mkdir()
    executable.write_bytes(b"application")
    invocations: list[tuple[list[object], dict[str, str]]] = []

    def fake_run_limited(command, **kwargs):
        arguments = list(command)
        environment = dict(kwargs["environment"])
        invocations.append((arguments, environment))
        data_root = Path(arguments[arguments.index("--data-root") + 1])
        result_path = Path(arguments[arguments.index("--check-output") + 1])
        if "--headless-check" in arguments:
            payload = {
                "status": "ok",
                "mode": "headless",
                "version": "0.1.0",
                "platform_name": "windows",
                "schema_version": 8,
                "settings_created": True,
                "data_directory": str(data_root / "data"),
            }
        else:
            is_offscreen = environment.get("QT_QPA_PLATFORM") == "offscreen"
            payload = {
                "status": "ok",
                "mode": "gui",
                "version": "0.1.0",
                "schema_version": 8,
                "qml_loaded": True,
                "shutdown_completed": True,
                "platform_name": "offscreen" if is_offscreen else "windows",
                "tls_backends": ["schannel"],
                "supported_image_formats": ["png", "jpg", "gif", "webp"],
                "data_directory": str(data_root / "data"),
            }
        result_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(windows_packaging, "run_limited", fake_run_limited)
    check_root = tmp_path / "中文 数据与结果"

    results = windows_packaging.run_application_checks(
        executable,
        working_directory=tmp_path / "仓库外 工作目录",
        check_root=check_root,
        label="测试 阶段",
    )

    assert len(invocations) == 3
    assert tuple(set(result) for result in results) == (
        set(windows_packaging.HEADLESS_RESULT_FIELDS),
        set(windows_packaging.GUI_RESULT_FIELDS),
        set(windows_packaging.GUI_RESULT_FIELDS),
    )
    assert [result["mode"] for result in results] == ["headless", "gui", "gui"]
    assert [result["platform_name"] for result in results] == [
        "windows",
        "windows",
        "offscreen",
    ]
    result_files = sorted(check_root.glob("*-结果.json"))
    assert len(result_files) == 3
    assert len({path.name for path in result_files}) == 3
    for arguments, _environment in invocations:
        data_root = Path(arguments[arguments.index("--data-root") + 1])
        assert "数据 目录" in data_root.name
        assert not data_root.exists()


def test_application_checks_refuse_preexisting_result_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check_root = tmp_path / "checks"
    check_root.mkdir()
    existing = check_root / "label-1-headless-结果.json"
    existing.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        windows_packaging,
        "run_limited",
        lambda *_args, **_kwargs: pytest.fail("application must not be launched"),
    )

    with pytest.raises(windows_packaging.PackagingError, match="already exists"):
        windows_packaging.run_application_checks(
            tmp_path / windows_packaging.APPLICATION_EXE,
            working_directory=tmp_path / "working",
            check_root=check_root,
            label="label",
        )


def test_publish_installer_uses_fixed_name_and_refuses_unowned_replacement(
    tmp_path: Path,
) -> None:
    built = tmp_path / "built.exe"
    built.write_bytes(b"first")
    output = tmp_path / "output"
    output.mkdir()

    published = build_script._publish_installer(built, output)
    assert published == output / windows_packaging.INSTALLER_NAME
    assert published.read_bytes() == b"first"
    assert (output / build_script.INSTALLER_OWNER_MARKER).read_text(
        encoding="utf-8"
    ) == windows_packaging.BUILD_OWNER_CONTENT

    unowned_output = tmp_path / "unowned-output"
    unowned_output.mkdir()
    (unowned_output / windows_packaging.INSTALLER_NAME).write_bytes(b"user file")
    with pytest.raises(windows_packaging.PackagingError, match="unowned installer"):
        build_script._publish_installer(built, unowned_output)
    assert (unowned_output / windows_packaging.INSTALLER_NAME).read_bytes() == (
        b"user file"
    )


def test_silent_install_command_has_s_and_final_unquoted_chinese_space_d_value(
    tmp_path: Path,
) -> None:
    installer = (tmp_path / "构建 工具" / windows_packaging.INSTALLER_NAME).absolute()
    install_directory = (tmp_path / "中文 安装 目录").absolute()

    command = install_verifier.build_silent_install_command(
        installer,
        install_directory,
    )

    expected_prefix = subprocess.list2cmdline([str(installer), "/S"])
    assert command == f"{expected_prefix} /D={install_directory}"
    assert command.endswith(f"/D={install_directory}")
    assert command.rfind(" /D=") > command.rfind(" /S")
    assert f'/D="{install_directory}"' not in command


def test_shortcut_target_uses_explicit_utf16_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shortcut = tmp_path / "中文 快捷方式.lnk"
    expected = Path("C:/中文 安装 目录/app/DanbooruMultiSearch.exe")
    captured: dict[str, object] = {}

    def fake_run_limited(command, **kwargs):
        captured["command"] = list(command)
        captured["encoding"] = kwargs.get("encoding")
        return subprocess.CompletedProcess(
            command,
            0,
            "\ufeff" + str(expected) + "\r\n",
            "",
        )

    monkeypatch.setattr(install_verifier, "run_limited", fake_run_limited)

    actual = install_verifier._read_shortcut_target(
        shortcut,
        tmp_path,
        timeout_seconds=10,
    )

    assert actual == expected
    assert captured["command"][:3] == ["cscript.exe", "//NoLogo", "//U"]
    assert captured["encoding"] == "utf-16-le"
    assert not (tmp_path / "read-shortcut-target.vbs").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows cscript Unicode output")
def test_cscript_unicode_output_is_decoded_without_active_codepage(
    tmp_path: Path,
) -> None:
    helper = tmp_path / "echo-argument.vbs"
    helper.write_text(
        "WScript.StdOut.Write WScript.Arguments(0)\r\n",
        encoding="ascii",
    )
    expected = r"C:\中文 安装 目录\app\DanbooruMultiSearch.exe"

    result = windows_packaging.run_limited(
        ["cscript.exe", "//NoLogo", "//U", helper, expected],
        cwd=tmp_path,
        timeout_seconds=30,
        encoding="utf-16-le",
    )

    assert result.stdout.lstrip("\ufeff") == expected


def test_controlled_uninstall_command_has_s_and_final_unquoted_question_value(
    tmp_path: Path,
) -> None:
    uninstaller = (tmp_path / "中文 安装 目录" / "Uninstall.exe").absolute()

    command = install_verifier.build_controlled_uninstall_command(uninstaller)

    expected_prefix = subprocess.list2cmdline([str(uninstaller), "/S"])
    assert command == f"{expected_prefix} _?={uninstaller.parent}"
    assert command.endswith(f"_?={uninstaller.parent}")
    assert command.rfind(" _?=") > command.rfind(" /S")
    assert f'_?="{uninstaller.parent}"' not in command


def test_controlled_uninstaller_is_held_then_removes_only_itself_and_empty_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_directory = tmp_path / "中文 安装 目录"
    install_directory.mkdir()
    uninstaller = install_directory / windows_packaging.UNINSTALLER_NAME
    uninstaller.write_bytes(b"uninstaller")
    captured: dict[str, object] = {}

    def fake_run_limited(command, **kwargs):
        captured["command"] = command
        captured["executable"] = kwargs.get("executable")
        assert uninstaller.exists()
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(install_verifier, "run_limited", fake_run_limited)

    install_verifier._run_uninstaller(uninstaller, timeout_seconds=10)

    assert captured["executable"] == uninstaller
    assert str(captured["command"]).endswith(f"_?={install_directory}")
    assert not install_directory.exists()


def test_controlled_uninstaller_preserves_scene_on_timeout_or_leftover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_directory = tmp_path / "install"
    install_directory.mkdir()
    uninstaller = install_directory / windows_packaging.UNINSTALLER_NAME
    uninstaller.write_bytes(b"uninstaller")
    monkeypatch.setattr(
        install_verifier,
        "run_limited",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            windows_packaging.ProcessTerminationError("uninstaller remains")
        ),
    )

    with pytest.raises(windows_packaging.ProcessTerminationError):
        install_verifier._run_uninstaller(uninstaller, timeout_seconds=1)
    assert uninstaller.is_file()

    leftover = install_directory / "unexpected.txt"
    leftover.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(
        install_verifier,
        "run_limited",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    with pytest.raises(
        windows_packaging.PackagingError,
        match="unexpected installation objects",
    ):
        install_verifier._run_uninstaller(uninstaller, timeout_seconds=1)
    assert uninstaller.is_file()
    assert leftover.is_file()


def test_install_validation_requires_explicit_confirmation_without_side_effect(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["verify_windows_install.py"])
    monkeypatch.setattr(
        install_verifier,
        "verify_windows_install",
        lambda *_args, **_kwargs: pytest.fail("validation must not start"),
    )

    assert install_verifier.main() == 2
    assert "--confirm-system-changes" in capsys.readouterr().err


@pytest.mark.parametrize(
    "script_name",
    ("build_windows_installer.py", "verify_windows_install.py"),
)
def test_windows_packaging_scripts_support_direct_invocation(
    script_name: str,
) -> None:
    result = subprocess.run(
        [sys.executable, PROJECT_ROOT / "scripts" / script_name, "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_build_timeout_with_unconfirmed_termination_preserves_owned_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_parent = tmp_path / "build"
    output_directory = tmp_path / "output"
    monkeypatch.setattr(build_script, "ensure_windows_build_environment", lambda: None)
    monkeypatch.setattr(
        build_script,
        "find_makensis",
        lambda _explicit: tmp_path / "makensis.exe",
    )
    monkeypatch.setattr(build_script, "verify_makensis_version", lambda _path: None)
    monkeypatch.setattr(
        build_script,
        "run_limited",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            windows_packaging.ProcessTerminationError("owned process remains")
        ),
    )

    with pytest.raises(
        windows_packaging.ProcessTerminationError,
        match="owned process remains",
    ):
        build_script.build_windows_installer(
            makensis=None,
            output_directory=output_directory,
            build_parent=build_parent,
            timeout_seconds=1,
        )

    workspaces = list(build_parent.glob("m1005-*"))
    assert len(workspaces) == 1
    assert (workspaces[0] / windows_packaging.BUILD_OWNER_MARKER).read_text(
        encoding="utf-8"
    ) == windows_packaging.BUILD_OWNER_CONTENT


@pytest.mark.skipif(os.name != "nt", reason="Windows taskkill behavior")
def test_failed_taskkill_is_reported_as_unconfirmed_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        pid = 1234

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(
        windows_packaging.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", ""),
    )

    with pytest.raises(
        windows_packaging.ProcessTerminationError,
        match="could not be stopped",
    ):
        windows_packaging._terminate_owned_process_tree(Process())


def test_install_timeout_with_unconfirmed_termination_preserves_validation_scene(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary_parent = tmp_path / "temporary"
    temporary_parent.mkdir()
    installer = tmp_path / windows_packaging.INSTALLER_NAME
    installer.write_bytes(b"installer")
    monkeypatch.setattr(install_verifier, "_require_native_amd64_windows", lambda: None)
    monkeypatch.setattr(install_verifier, "verify_installer", lambda _path: None)
    monkeypatch.setattr(
        install_verifier,
        "_preflight",
        lambda _timeout, _name: (tmp_path / "default", tmp_path / "start-menu"),
    )
    monkeypatch.setattr(
        install_verifier.tempfile,
        "gettempdir",
        lambda: str(temporary_parent),
    )
    monkeypatch.setattr(
        install_verifier,
        "_run_installer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            windows_packaging.ProcessTerminationError("installer process remains")
        ),
    )

    with pytest.raises(
        windows_packaging.ProcessTerminationError,
        match="installer process remains",
    ):
        install_verifier.verify_windows_install(installer, timeout_seconds=1)

    scenes = list(temporary_parent.glob("DanbooruMultiSearch-中文 安装验证-*"))
    assert len(scenes) == 1
    assert (scenes[0] / install_verifier.VALIDATION_OWNER_NAME).read_text(
        encoding="utf-8"
    ) == install_verifier.VALIDATION_OWNER_CONTENT


def test_recovery_rethrows_unconfirmed_uninstaller_termination_and_preserves_scene(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_directory = tmp_path / "中文 安装 目录"
    start_menu_directory = tmp_path / "开始 菜单"
    install_directory.mkdir()
    start_menu_directory.mkdir()
    marker = install_directory / windows_packaging.INSTALL_MARKER_NAME
    marker.write_text(
        windows_packaging.INSTALL_MARKER_CONTENT,
        encoding="utf-8",
    )
    uninstaller = install_directory / windows_packaging.UNINSTALLER_NAME
    uninstaller.write_bytes(b"uninstaller")
    registry_values = {
        "InstallLocation": str(install_directory),
        "DisplayVersion": windows_packaging.PACKAGE_VERSION,
    }
    monkeypatch.setattr(
        install_verifier,
        "_read_registry_key",
        lambda _key: dict(registry_values),
    )
    monkeypatch.setattr(
        install_verifier,
        "_run_uninstaller",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            windows_packaging.ProcessTerminationError(
                "recovery uninstaller process remains"
            )
        ),
    )

    with pytest.raises(
        windows_packaging.ProcessTerminationError,
        match="recovery uninstaller process remains",
    ):
        install_verifier._try_recover_owned_installation(
            install_directory,
            start_menu_directory,
            timeout_seconds=1,
        )

    assert marker.read_text(encoding="utf-8") == (
        windows_packaging.INSTALL_MARKER_CONTENT
    )
    assert uninstaller.read_bytes() == b"uninstaller"
    assert start_menu_directory.is_dir()
