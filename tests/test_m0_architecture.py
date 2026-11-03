from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "danbooru_multi_search"
BUSINESS_MODULE_PATHS = (
    PACKAGE_ROOT / "__main__.py",
    *(PACKAGE_ROOT / name for name in ("core", "danbooru", "storage", "tasks", "ui")),
)
PLATFORM_SPECIFIC_PATTERNS = {
    "hard-coded drive path": re.compile(r"(?i)(?<![A-Za-z])[A-Z]:[\\/]"),
    "hard-coded Linux home path": re.compile(r"/home/"),
    "hard-coded user directory": re.compile(
        r"\b(?:AppData|USERPROFILE|HOMEDRIVE|HOMEPATH)\b"
    ),
    "operating-system branch": re.compile(
        r"\b(?:os\.name|sys\.platform|platform\.system\s*\()"
    ),
    "platform implementation import": re.compile(
        r"danbooru_multi_search\.platform\."
        r"(?:adapters|paths|stubs|windows_wallpaper)\b"
    ),
    "Windows implementation access": re.compile(
        r"\b(?:winreg|WinDLL|PowerShell|EagleWebApiBridge|"
        r"WindowsPlatformAdapter|LinuxPlatformAdapter|"
        r"UnsupportedPlatformAdapter)\b"
    ),
}


def _business_python_files() -> tuple[Path, ...]:
    files: list[Path] = []
    for path in BUSINESS_MODULE_PATHS:
        if path.is_file():
            files.append(path)
        else:
            files.extend(sorted(path.rglob("*.py")))
    return tuple(files)


def test_business_modules_have_no_platform_specific_paths_or_implementations() -> None:
    violations: list[str] = []

    for path in _business_python_files():
        source = path.read_text(encoding="utf-8")
        relative_path = path.relative_to(PROJECT_ROOT)
        for description, pattern in PLATFORM_SPECIFIC_PATTERNS.items():
            if pattern.search(source):
                violations.append(f"{relative_path}: {description}")
        if "\\\\" in source:
            violations.append(f"{relative_path}: hard-coded backslash")

    assert violations == []
