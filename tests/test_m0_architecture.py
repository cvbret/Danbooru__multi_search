from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


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


def _has_forbidden_backslash_literal(source: str) -> bool:
    if "\\\\" not in source:
        return False
    tree = ast.parse(source)
    filename_checks: set[ast.AST] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1 or not isinstance(node.ops[0], (ast.In, ast.NotIn)):
            continue
        left, right = node.left, node.comparators[0]
        if isinstance(left, ast.Constant) and left.value == "\\" and isinstance(right, ast.Name):
            filename_checks.add(left)
        if isinstance(right, ast.Constant) and right.value == '<>:"/\\|?*' and isinstance(left, ast.Name):
            filename_checks.add(right)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, (str, bytes)):
            continue
        if node in filename_checks:
            continue
        # Keep the existing doubled-backslash rule. Raw regex escapes such as
        # r"\d+" have never represented a hard-coded path under this check.
        literal = ast.get_source_segment(source, node) or ""
        if "\\\\" in literal:
            return True
    return False


def _source_violations(source: str) -> tuple[str, ...]:
    violations = [description for description, pattern in PLATFORM_SPECIFIC_PATTERNS.items() if pattern.search(source)]
    if _has_forbidden_backslash_literal(source):
        violations.append("hard-coded backslash")
    return tuple(violations)


def test_business_modules_have_no_platform_specific_paths_or_implementations() -> None:
    violations: list[str] = []

    for path in _business_python_files():
        source = path.read_text(encoding="utf-8")
        relative_path = path.relative_to(PROJECT_ROOT)
        violations.extend(f"{relative_path}: {description}" for description in _source_violations(source))

    assert violations == []


@pytest.mark.parametrize("source", [
    r'''valid = "\\" not in filename''',
    r'''valid = all(char not in '<>:"/\\|?*' for char in filename)''',
    r'''pattern = r"\d+\s"''',
])
def test_filename_membership_checks_and_existing_raw_regexes_are_allowed(source):
    assert _source_violations(source) == ()


@pytest.mark.parametrize("source", [
    r'''path = "C:\\Users\\sample"''',
    r'''path = r"\\server\share"''',
    r'''path = "relative\\folder"''',
    r'''separator = "\\"; path = separator.join(parts)''',
    r'''path = b"relative\\folder"''',
    '''path = "/home/sample"''',
    '''location = "AppData"''',
    '''if os.name == "nt": pass''',
    '''import winreg''',
    '''from danbooru_multi_search.platform.windows_wallpaper import probe''',
])
def test_platform_paths_and_implementations_remain_rejected(source):
    assert _source_violations(source)
