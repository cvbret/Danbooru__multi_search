from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts import verify_windows_install as install_verifier
from scripts import windows_packaging


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NSIS_SCRIPT = PROJECT_ROOT / "packaging" / "windows" / "installer.nsi"
MAKENSIS = (
    PROJECT_ROOT
    / ".tools"
    / "nsis-3.13"
    / "nsis-3.13"
    / "makensis.exe"
)


def _read_probe_result(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-16").splitlines()
        if "=" in line
    )


def test_installer_initialization_uses_nonexistent_path_safe_normalization() -> None:
    source = NSIS_SCRIPT.read_text(encoding="utf-8")

    assert "GetFullPathNameW" in source
    assert source.count("GetFullPathNameW") == 2
    assert 'StrCpy $FailureStage "normalize_install_directory"' in source
    assert "FileWriteUTF16LE /BOM" in source
    assert 'Delete "$TEMP\\${INSTALL_ERROR_LOG}"' in source
    assert "!ifdef INIT_PROBE_RESULT" in source
    assert 'File /r /x "${BUILD_OWNER_MARKER}" "${APP_SOURCE}\\*.*"' in source
    assert (
        'ClearErrors\n  FileOpen $0 "$INSTDIR\\${MARKER_FILE}" w\n'
        "  IfErrors marker_open_error"
    ) in source
    assert (
        'ClearErrors\n  FileWrite $0 "${MARKER_VALUE}"\n'
        "  IfErrors marker_write_error"
    ) in source


@pytest.mark.skipif(
    os.name != "nt" or not MAKENSIS.is_file(),
    reason="Windows NSIS 3.13 initialization probe is unavailable",
)
def test_compiled_installer_initialization_accepts_new_chinese_path_and_logs_failure(
    tmp_path: Path,
) -> None:
    if (
        install_verifier._read_registry_key(
            windows_packaging.APPLICATION_REGISTRY_KEY
        )
        is not None
        or install_verifier._read_registry_key(
            windows_packaging.UNINSTALL_REGISTRY_KEY
        )
        is not None
    ):
        pytest.skip("stable installation registry keys already exist")

    app_source = tmp_path / "app"
    output_directory = tmp_path / "output"
    result_path = tmp_path / "init-result.txt"
    app_source.mkdir()
    output_directory.mkdir()
    (app_source / "placeholder.txt").write_text("placeholder", encoding="utf-8")
    compile_result = subprocess.run(
        [
            MAKENSIS,
            f"/DAPP_SOURCE={app_source}",
            f"/DOUTPUT_DIR={output_directory}",
            f"/DINIT_PROBE_RESULT={result_path}",
            NSIS_SCRIPT,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr
    installer = output_directory / windows_packaging.INSTALLER_NAME
    target = tmp_path / "中文 安装 目录"

    success_command = install_verifier.build_silent_install_command(
        installer,
        target,
    )
    windows_packaging.run_limited(
        success_command,
        executable=installer,
        cwd=output_directory,
        timeout_seconds=30,
    )
    success = _read_probe_result(result_path)
    assert success["stage"] == "success"
    assert success["native_architecture"] == "9"
    assert windows_packaging.normalize_windows_path(
        success["install_directory"]
    ) == windows_packaging.normalize_windows_path(target)
    assert success["is_reinstall"] == "0"
    assert not target.exists()

    result_path.unlink()
    forced_failure_compile = subprocess.run(
        [
            MAKENSIS,
            f"/DAPP_SOURCE={app_source}",
            f"/DOUTPUT_DIR={output_directory}",
            f"/DINIT_PROBE_RESULT={result_path}",
            "/DINIT_PROBE_FORCE_FAILURE=1",
            NSIS_SCRIPT,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert forced_failure_compile.returncode == 0, (
        forced_failure_compile.stdout + forced_failure_compile.stderr
    )
    with pytest.raises(
        windows_packaging.PackagingError,
        match="Process exited with 2",
    ):
        windows_packaging.run_limited(
            success_command,
            executable=installer,
            cwd=output_directory,
            timeout_seconds=30,
        )
    failure = _read_probe_result(result_path)
    assert failure["stage"] == "forced_init_probe_failure"
    assert failure["message"] == "Forced initialization probe failure."
    assert not target.exists()


@pytest.mark.skipif(
    os.name != "nt" or not MAKENSIS.is_file(),
    reason="Windows NSIS 3.13 marker probe is unavailable",
)
def test_missing_registry_first_install_writes_complete_fixed_marker(
    tmp_path: Path,
) -> None:
    for key_name in (
        windows_packaging.APPLICATION_REGISTRY_KEY,
        windows_packaging.UNINSTALL_REGISTRY_KEY,
    ):
        if install_verifier._read_registry_key(key_name) is not None:
            pytest.skip("stable installation registry keys already exist")

    app_source = tmp_path / "app"
    output_directory = tmp_path / "output"
    target = tmp_path / "中文 空标记目录"
    result_path = tmp_path / "marker-result.txt"
    app_source.mkdir()
    output_directory.mkdir()
    target.mkdir()
    (app_source / "placeholder.txt").write_text("placeholder", encoding="utf-8")
    compile_result = subprocess.run(
        [
            MAKENSIS,
            f"/DAPP_SOURCE={app_source}",
            f"/DOUTPUT_DIR={output_directory}",
            f"/DINIT_PROBE_RESULT={result_path}",
            "/DINIT_PROBE_WRITE_MARKER=1",
            NSIS_SCRIPT,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr
    installer = output_directory / windows_packaging.INSTALLER_NAME

    command = install_verifier.build_silent_install_command(installer, target)
    windows_packaging.run_limited(
        command,
        executable=installer,
        cwd=output_directory,
        timeout_seconds=30,
    )

    assert _read_probe_result(result_path)["stage"] == "success"
    marker = target / windows_packaging.INSTALL_MARKER_NAME
    assert marker.read_bytes() == (
        windows_packaging.INSTALL_MARKER_CONTENT.encode("ascii")
    )
    assert {path.name for path in target.iterdir()} == {
        windows_packaging.INSTALL_MARKER_NAME
    }
    for key_name in (
        windows_packaging.APPLICATION_REGISTRY_KEY,
        windows_packaging.UNINSTALL_REGISTRY_KEY,
    ):
        assert install_verifier._read_registry_key(key_name) is None
