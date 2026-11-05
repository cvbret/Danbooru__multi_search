from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NSIS_SCRIPT = PROJECT_ROOT / "packaging" / "windows" / "installer.nsi"
MAKENSIS = (
    PROJECT_ROOT
    / ".tools"
    / "nsis-3.13"
    / "nsis-3.13"
    / "makensis.exe"
)


def _function_body(source: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^Function {re.escape(name)}\s*$\n(.*?)^FunctionEnd\s*$",
        source,
    )
    assert match is not None, f"missing NSIS function {name}"
    return match.group(1)


def test_first_install_checks_actual_64_bit_registry_keys() -> None:
    source = NSIS_SCRIPT.read_text(encoding="utf-8")
    on_init = _function_body(source, ".onInit")
    validate_absent = _function_body(source, "ValidateRegistryAbsent")
    probe = _function_body(source, "RegistryKeyExists64")

    assert on_init.index("SetRegView 64") < on_init.index(
        "Call ValidateRegistryAbsent"
    )
    assert validate_absent.count("Call RegistryKeyExists64") == 2
    assert 'Push "${PRODUCT_KEY}"' in validate_absent
    assert 'Push "${UNINSTALL_KEY}"' in validate_absent
    assert "$AppInstallLocation" not in validate_absent
    assert "$AppDisplayVersion" not in validate_absent
    assert "$UninstallLocation" not in validate_absent
    assert "$UninstallDisplayVersion" not in validate_absent

    assert "RegOpenKeyExW" in probe
    assert "0x80000001" in probe  # HKEY_CURRENT_USER
    assert "0x0101" in probe  # KEY_QUERY_VALUE | KEY_WOW64_64KEY
    assert "RegCloseKey" in probe
    assert "StrCmp $2 0 registry_key_exists" in probe
    assert "StrCmp $2 2 registry_key_absent registry_key_error" in probe
    assert 'StrCpy $0 "error"' in probe


@pytest.mark.skipif(not MAKENSIS.is_file(), reason="project NSIS 3.13 tool is absent")
def test_nsis_script_compiles_with_registry_key_probe(tmp_path: Path) -> None:
    app_source = tmp_path / "app"
    output_directory = tmp_path / "output"
    app_source.mkdir()
    output_directory.mkdir()
    (app_source / "placeholder.txt").write_text("placeholder", encoding="utf-8")

    result = subprocess.run(
        [
            MAKENSIS,
            f"/DAPP_SOURCE={app_source}",
            f"/DOUTPUT_DIR={output_directory}",
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

    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        output_directory / "DanbooruMultiSearch-0.1.0-win64-setup.exe"
    ).is_file()
