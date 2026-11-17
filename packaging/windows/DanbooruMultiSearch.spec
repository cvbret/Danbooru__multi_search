# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path, PurePosixPath


project_root = Path(SPECPATH).resolve().parents[1]
source_root = project_root / "src"
package_root = source_root / "danbooru_multi_search"
qml_root = package_root / "ui" / "qml"
version_file = Path(SPECPATH).resolve() / "version_info.txt"

project_qml = [
    (str(qml_root / "ActionButton.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "AppTheme.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "AppearancePanel.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "ApplicationBackground.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "PostCard.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "GalleryGrid.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "M2Main.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "FavoritesPage.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "FavoriteTransferDialog.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "ArtistCombinationPanel.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "SavedQueryPanel.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "CacheCleanupDialog.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "DownloadsPage.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "DownloadRequestDialog.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "FavoriteObjectCard.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "FavoriteConfirmDialog.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "DetailPage.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "PoolPage.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "PoolReaderPage.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "PoolDownloadPanel.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "EaglePanel.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "PoolReaderItem.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "PoolMembershipPanel.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "ImageViewer.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "QueryEditorPanel.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "QueryFilterPanel.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "QueryStatusBar.qml"), "danbooru_multi_search/ui/qml"),
    (str(qml_root / "HistoryPanel.qml"), "danbooru_multi_search/ui/qml"),
]

# These modules are imported conditionally by platform adapters, lazy package
# exports, or the QML engine. Keep every lazy export target explicit so freezing
# preserves the same public imports and collects the required Qt dependencies.
hidden_imports = [
    "ctypes.wintypes",
    "winreg",
    "danbooru_multi_search.integrations.eagle.client",
    "danbooru_multi_search.integrations.eagle.contracts",
    "danbooru_multi_search.integrations.eagle.stubs",
    "danbooru_multi_search.integrations.eagle.transport",
    "danbooru_multi_search.platform.windows_wallpaper",
    "danbooru_multi_search.core.favorites",
    "danbooru_multi_search.core.artist_combinations",
    "danbooru_multi_search.core.saved_queries",
    "danbooru_multi_search.core.pools",
    "danbooru_multi_search.core.pool_pages",
    "danbooru_multi_search.core.pool_reader",
    "danbooru_multi_search.core.pool_progress",
    "danbooru_multi_search.core.pool_downloads",
    "danbooru_multi_search.storage.pool_downloads",
    "danbooru_multi_search.app.pool_downloads",
    "danbooru_multi_search.ui.pool_downloads",
    "danbooru_multi_search.integrations.eagle.qt_transport",
    "danbooru_multi_search.app.eagle_service",
    "danbooru_multi_search.app.eagle_runtime",
    "danbooru_multi_search.ui.eagle_messages",
    "danbooru_multi_search.ui.eagle",
    "danbooru_multi_search.storage.pool_progress",
    "danbooru_multi_search.ui.pool_progress",
    "danbooru_multi_search.ui.pool_reader_models",
    "danbooru_multi_search.ui.pool_reader",
    "danbooru_multi_search.danbooru.pool_post_parsing",
    "danbooru_multi_search.danbooru.pool_pages",
    "danbooru_multi_search.ui.pool_models",
    "danbooru_multi_search.ui.pools",
    "danbooru_multi_search.ui.pool_navigation",
    "danbooru_multi_search.danbooru.pool_parsing",
    "danbooru_multi_search.danbooru.pools",
    "danbooru_multi_search.storage.saved_queries",
    "danbooru_multi_search.ui.saved_queries",
    "danbooru_multi_search.danbooru.query_checkpoint",
    "danbooru_multi_search.storage.checkpoint_serialization",
    "danbooru_multi_search.storage.query_checkpoints",
    "danbooru_multi_search.ui.query_checkpoints",
    "danbooru_multi_search.storage.cache_protection",
    "danbooru_multi_search.ui.cache_protection",
    "danbooru_multi_search.ui.protected_image_cache",
    "danbooru_multi_search.ui.cache_cleanup",
    "danbooru_multi_search.core.downloads",
    "danbooru_multi_search.storage.downloads",
    "danbooru_multi_search.storage.download_files",
    "danbooru_multi_search.app.download_manager",
    "danbooru_multi_search.app.download_submission",
    "danbooru_multi_search.storage.artist_combinations",
    "danbooru_multi_search.ui.artist_combinations",
    "danbooru_multi_search.ui.downloads",
    "danbooru_multi_search.ui.download_models",
    "danbooru_multi_search.danbooru.post_validation",
    "danbooru_multi_search.ui.query_forms",
    "danbooru_multi_search.danbooru.autocomplete",
    "danbooru_multi_search.danbooru.client",
    "danbooru_multi_search.danbooru.details",
    "danbooru_multi_search.danbooru.download",
    "danbooru_multi_search.danbooru.gallery",
    "danbooru_multi_search.danbooru.models",
    "danbooru_multi_search.danbooru.parsing",
    "danbooru_multi_search.danbooru.query_controller",
    "danbooru_multi_search.danbooru.query_definition",
    "danbooru_multi_search.danbooru.query_evaluator",
    "danbooru_multi_search.danbooru.query_execution",
    "danbooru_multi_search.danbooru.query_language",
    "danbooru_multi_search.danbooru.query_planning",
    "danbooru_multi_search.danbooru.query_types",
    "danbooru_multi_search.danbooru.result_processing",
    "danbooru_multi_search.danbooru.smoke",
    "danbooru_multi_search.danbooru.tag_index",
    "danbooru_multi_search.danbooru.tag_syntax",
    "danbooru_multi_search.danbooru.tag_translation",
    "danbooru_multi_search.danbooru.tags",
    "danbooru_multi_search.storage.database",
    "danbooru_multi_search.storage.favorite_codec",
    "danbooru_multi_search.storage.favorite_transfer",
    "danbooru_multi_search.app.favorite_transfer",
    "danbooru_multi_search.app.favorites",
    "danbooru_multi_search.app.write_access",
    "danbooru_multi_search.storage.favorites",
    "danbooru_multi_search.storage.history",
    "danbooru_multi_search.storage.post_snapshot",
    "danbooru_multi_search.storage.query_serialization",
    "danbooru_multi_search.tasks.model",
    "danbooru_multi_search.tasks.qt_scheduling",
    "danbooru_multi_search.tasks.queue",
    "danbooru_multi_search.tasks.retry",
    "danbooru_multi_search.tasks.scheduling",
    "danbooru_multi_search.ui.detail_navigation",
    "danbooru_multi_search.ui.favorites",
    "danbooru_multi_search.ui.favorite_collection",
    "danbooru_multi_search.ui.favorite_transfer",
    "danbooru_multi_search.ui.favorite_models",
    "danbooru_multi_search.ui.gallery_selection",
    "danbooru_multi_search.ui.detail_favorites",
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
