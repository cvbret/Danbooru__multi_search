# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path, PurePosixPath


project_root = Path(SPECPATH).resolve().parents[1]
source_root = project_root / "src"
package_root = source_root / "danbooru_multi_search"
qml_root = package_root / "ui" / "qml"
version_file = Path(SPECPATH).resolve() / "version_info.txt"

project_qml = [
    (str(qml_root / "Main.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "M2Main.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "DetailPage.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "ImageViewer.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "QueryEditorPanel.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "QueryFilterPanel.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "QueryStatusBar.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "HistoryPanel.qml"), "danbooru_multi_search/ui/qml"),
]

# These modules are imported conditionally by the platform adapter or loaded by
# the QML engine. Keeping the list explicit makes the Windows package contract
# reviewable and lets PyInstaller's PySide6 hooks collect their Qt dependencies.
hidden_imports = [
    "ctypes.wintypes",
    "winreg",
    "danbooru_multi_search.integrations.eagle.client",
    "danbooru_multi_search.integrations.eagle.transport",
    "danbooru_multi_search.platform.windows_wallpaper",
    "PySide6.QtQuick",
    "PySide6.QtQuickControls2",
    "PySide6.QtQml",
    "PySide6.QtNetwork",
    "PySide6.QtSvg",
]

excluded_modules = [
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtLocation",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtSerialBus",
    "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets",
]

# PyInstaller's PySide6.QtQml hook collects the complete installed QML tree.
# The application imports only these modules (including the modules found by
# qmlimportscanner through Controls and Dialogs). Root QtQuick and QtQml files
# are kept separately so that unrelated children such as QtQuick.Pdf and
# QtQml.StateMachine do not slip through a broad prefix match.
qml_direct_module_directories = {
    "pyside6/qml/qtqml",
    "pyside6/qml/qtquick",
}
qml_recursive_module_directories = (
    "pyside6/qml/qt/labs/folderlistmodel",
    "pyside6/qml/qtqml/models",
    "pyside6/qml/qtqml/workerscript",
    "pyside6/qml/qtquick/controls",
    "pyside6/qml/qtquick/dialogs",
    "pyside6/qml/qtquick/effects",
    "pyside6/qml/qtquick/layouts",
    "pyside6/qml/qtquick/nativestyle",
    "pyside6/qml/qtquick/shapes",
    "pyside6/qml/qtquick/templates",
    "pyside6/qml/qtquick/window",
)
forbidden_qt_file_prefixes = (
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


def keep_selected_qml_entry(entry):
    destination = str(entry[0]).replace("\\", "/").casefold()
    marker = "pyside6/qml/"
    marker_index = destination.find(marker)
    if marker_index < 0:
        return True
    parent = PurePosixPath(destination[marker_index:]).parent.as_posix()
    if parent in qml_direct_module_directories:
        return True
    return any(
        parent == module or parent.startswith(module + "/")
        for module in qml_recursive_module_directories
    )


def keep_selected_runtime_binary(entry):
    filename = PurePosixPath(str(entry[0]).replace("\\", "/")).name.casefold()
    if filename == "icuuc.dll" or (
        filename.startswith("icudt") and filename.endswith(".dll")
    ):
        return False
    return not filename.startswith(forbidden_qt_file_prefixes)

a = Analysis(
    [str(package_root / "__main__.py")],
    pathex=[str(source_root)],
    binaries=[],
    datas=project_qml,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded_modules,
    noarchive=False,
    optimize=0,
)
a.datas = [entry for entry in a.datas if keep_selected_qml_entry(entry)]
a.binaries = [
    entry
    for entry in a.binaries
    if keep_selected_qml_entry(entry) and keep_selected_runtime_binary(entry)
]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DanbooruMultiSearch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    codesign_identity=None,
    entitlements_file=None,
    version=str(version_file),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DanbooruMultiSearch",
)
