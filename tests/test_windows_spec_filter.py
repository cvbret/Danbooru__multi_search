from __future__ import annotations

from pathlib import Path

import pytest

from scripts import windows_packaging


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = PROJECT_ROOT / "packaging" / "windows" / "DanbooruMultiSearch.spec"


def _make_minimal_bundle(root: Path) -> Path:
    bundle = root / windows_packaging.APPLICATION_BASENAME
    executable = bundle / windows_packaging.APPLICATION_EXE
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic executable")

    project_qml = (
        bundle / "_internal" / "danbooru_multi_search" / "ui" / "qml"
    )
    project_qml.mkdir(parents=True)
    for name in windows_packaging.QML_FILES:
        (project_qml / name).write_text("import QtQuick\nItem {}\n", encoding="utf-8")

    qt_root = bundle / "_internal" / "PySide6" / "Qt"
    (qt_root / "qml" / "QtQuick").mkdir(parents=True)
    plugin_root = qt_root / "plugins"
    for directory, name in windows_packaging.REQUIRED_PLUGIN_FILES:
        plugin = plugin_root / directory / name
        plugin.parent.mkdir(parents=True, exist_ok=True)
        plugin.write_bytes(b"plugin")
    return bundle


def test_spec_filters_qtqml_hook_results_before_collection() -> None:
    source = SPEC_PATH.read_text(encoding="utf-8")

    analysis_end = source.index(")\na.datas =", source.index("a = Analysis("))
    pyz_start = source.index("pyz = PYZ(a.pure)")
    assert analysis_end < pyz_start
    assert "a.datas = [entry for entry in a.datas if keep_selected_qml_entry(entry)]" in source
    assert "keep_selected_qml_entry(entry) and keep_selected_runtime_binary(entry)" in source
    for required_module in (
        "pyside6/qml/qt/labs/folderlistmodel",
        "pyside6/qml/qtqml/models",
        "pyside6/qml/qtqml/workerscript",
        "pyside6/qml/qtquick/controls",
        "pyside6/qml/qtquick/dialogs",
        "pyside6/qml/qtquick/layouts",
        "pyside6/qml/qtquick/templates",
        "pyside6/qml/qtquick/window",
    ):
        assert f'"{required_module}"' in source
    for forbidden_prefix in (
        "qt63d",
        "qt6multimedia",
        "qt6quick3d",
        "qt6webengine",
    ):
        assert f'"{forbidden_prefix}"' in source
    assert 'filename == "icuuc.dll"' in source
    assert 'filename.startswith("icudt")' in source


def test_verify_onedir_accepts_only_selected_qml_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _make_minimal_bundle(tmp_path)
    qml_root = bundle / "_internal" / "PySide6" / "qml"
    for relative in (
        Path("Qt") / "labs" / "folderlistmodel" / "qmldir",
        Path("QtQml") / "Models" / "qmldir",
        Path("QtQuick") / "Controls" / "Windows" / "Button.qml",
        Path("QtQuick") / "Dialogs" / "quickimpl" / "FileDialog.qml",
        Path("QtQuick") / "Layouts" / "qmldir",
    ):
        target = qml_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("selected module", encoding="utf-8")
    monkeypatch.setattr(windows_packaging, "verify_executable", lambda _path: None)

    windows_packaging.verify_onedir(bundle)


@pytest.mark.parametrize(
    "module_directory",
    (
        "Qt3D",
        "QtMultimedia",
        "QtQuick3D",
        "QtWebEngine",
        "QtQuick/Pdf",
        "QtQml/StateMachine",
    ),
)
def test_verify_onedir_rejects_unselected_qml_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module_directory: str,
) -> None:
    bundle = _make_minimal_bundle(tmp_path)
    unwanted = (
        bundle
        / "_internal"
        / "PySide6"
        / "qml"
        / Path(module_directory)
        / "qmldir"
    )
    unwanted.parent.mkdir(parents=True, exist_ok=True)
    unwanted.write_text("unwanted module", encoding="utf-8")
    monkeypatch.setattr(windows_packaging, "verify_executable", lambda _path: None)

    with pytest.raises(
        windows_packaging.PackagingError,
        match="Unused Qt QML module",
    ):
        windows_packaging.verify_onedir(bundle)


@pytest.mark.parametrize(
    "filename",
    (
        "Qt63DCore.dll",
        "Qt6Multimedia.dll",
        "QtQuick3D.pyd",
        "Qt6WebEngineCore.dll",
    ),
)
def test_verify_onedir_rejects_unused_qt_runtime_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    bundle = _make_minimal_bundle(tmp_path)
    unwanted = bundle / "_internal" / "PySide6" / filename
    unwanted.parent.mkdir(parents=True, exist_ok=True)
    unwanted.write_bytes(b"unused runtime")
    monkeypatch.setattr(windows_packaging, "verify_executable", lambda _path: None)

    with pytest.raises(
        windows_packaging.PackagingError,
        match="Unused Qt runtime file",
    ):
        windows_packaging.verify_onedir(bundle)


@pytest.mark.parametrize("filename", ("icuuc.dll", "icudt78.dll"))
def test_verify_onedir_rejects_external_icu_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    bundle = _make_minimal_bundle(tmp_path)
    unwanted = bundle / "_internal" / filename
    unwanted.write_bytes(b"external ICU")
    monkeypatch.setattr(windows_packaging, "verify_executable", lambda _path: None)

    with pytest.raises(
        windows_packaging.PackagingError,
        match="external ICU runtime",
    ):
        windows_packaging.verify_onedir(bundle)
