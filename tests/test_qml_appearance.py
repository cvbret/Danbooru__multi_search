from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from PySide6.QtCore import Q_ARG, QCoreApplication, QEvent, QMetaObject, QObject, QUrl, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage
from PySide6.QtTest import QTest
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtQml import QQmlExpression

from danbooru_multi_search.core.bootstrap import initialize_runtime
from danbooru_multi_search.core.settings import AppSettings, SettingsStore
from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.ui.launcher import create_qml_engine
from danbooru_multi_search.ui.window_state import ScreenGeometry, WindowGeometry


APPLICATION = QGuiApplication.instance() or QGuiApplication([])


class NoNetworkManager:
    def get(self, _request):
        raise AssertionError("Appearance must not request remote metadata")


def _wait_until(predicate):
    for _ in range(100):
        APPLICATION.processEvents()
        if predicate():
            return True
        QTest.qWait(10)
    return predicate()


def _window(tmp_path, *, wide=False):
    runtime = initialize_runtime(tmp_path)
    engine, bridge = create_qml_engine(
        APPLICATION,
        danbooru_base_url="https://example.invalid",
        network_manager=NoNetworkManager(),
        runtime=runtime,
        screen_geometries=(
            (ScreenGeometry(WindowGeometry(0, 0, 1280, 820), primary=True),)
            if wide else None
        ),
    )
    root = engine.rootObjects()[0]
    session = engine.rootContext().contextProperty("sessionController").parent()
    return engine, root, session, bridge, runtime


def _close(engine, root, session):
    session.shutdown()
    root.close()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    APPLICATION.processEvents()


def test_theme_switches_real_qml_and_appearance_controls_fit_minimum_window(tmp_path):
    engine, root, session, bridge, runtime = _window(tmp_path)
    try:
        appearance = session.appearance_controller
        original_revision = bridge.postsModel.revision
        assert appearance.setTheme("dark")
        APPLICATION.processEvents()
        assert root.property("color") == QColor("#121419")
        assert appearance.setTheme("light")
        APPLICATION.processEvents()
        assert root.property("color") == QColor("#eef1f6")
        assert bridge.postsModel.revision == original_revision
        root.setProperty("width", 820)
        root.setProperty("height", 600)
        APPLICATION.processEvents()
        button = root.findChild(QObject, "appearanceButton")
        assert button is not None and button.property("visible")
        assert QMetaObject.invokeMethod(button, "clicked", Qt.ConnectionType.DirectConnection)
        panel = root.findChild(QObject, "appearancePanel")
        assert _wait_until(lambda: panel.property("opened"))
        assert panel.property("width") <= root.property("width") - 32
        assert panel.property("height") <= root.property("height") - 32
        assert root.findChild(QObject, "chooseBackgroundButton") is not None
        assert root.findChild(QObject, "backgroundBrightnessSlider") is not None
        assert QQuickStyle.name() == "Basic"
        choose = root.findChild(QObject, "chooseBackgroundButton")
        choice = root.findChild(QObject, "themeChoice")
        assert choose.property("height") >= 36
        assert choice.property("height") >= 36
        assert choose.property("font").pixelSize() == 14
        assert appearance.setTheme("dark")
        APPLICATION.processEvents()
        assert choose.property("background").property("color").lightnessF() < 0.5
        assert choice.property("background").property("color").lightnessF() < 0.5
        choose.forceActiveFocus(Qt.FocusReason.TabFocusReason)
        APPLICATION.processEvents()
        assert choose.property("visualFocus") is True
        outline = QQmlExpression(engine.rootContext(), choose.property("background"), "border.width")
        assert outline.evaluate()[0] == 2
        assert bridge.applyAdvancedText("cat")
        APPLICATION.processEvents()
        search = root.findChild(QObject, "searchButton")
        theme = root.property("theme")
        assert search.property("contentItem").property("color") == theme.property("accentForeground")
        large = root.findChild(QObject, "detailLargeButton")
        assert large.property("checked") is True
        assert large.property("background").property("color") == theme.property("selected")
        assert appearance.setTheme("light")
        APPLICATION.processEvents()
        assert choose.property("background").property("color").lightnessF() > 0.5
        assert SettingsStore(runtime.paths.settings_file).load_or_default(AppSettings()).theme == "light"
    finally:
        _close(engine, root, session)


def test_local_background_loads_adjusts_and_bad_image_keeps_path_with_default(tmp_path):
    image_path = (tmp_path / "背景 100%.png").resolve()
    image = QImage(32, 24, QImage.Format.Format_ARGB32)
    image.fill(QColor("#6699cc"))
    assert image.save(str(image_path))
    engine, root, session, _bridge, runtime = _window(tmp_path)
    try:
        appearance = session.appearance_controller
        background = root.findChild(QObject, "applicationBackground")
        assert appearance.selectBackground(QUrl.fromLocalFile(str(image_path)))
        assert _wait_until(lambda: background.property("imageReady"))
        assert appearance.statusText == ""
        assert appearance.setBackgroundAdjustments(0.4, 0.6, 1.5)
        APPLICATION.processEvents()
        effect = root.findChild(QObject, "backgroundEffect")
        assert effect.property("blur") == 0.6
        assert effect.property("brightness") == 0.5
        good_generation = appearance.backgroundGeneration
        good_url = appearance.backgroundUrl
        broken_path = (tmp_path / "损坏背景.png").resolve()
        broken_path.write_bytes(b"not an image")
        assert appearance.selectBackground(QUrl.fromLocalFile(str(broken_path)))
        assert _wait_until(lambda: "默认背景" in appearance.statusText)
        assert background.property("imageReady") is False
        appearance.reportBackgroundResult(good_generation, good_url, True)
        assert "默认背景" in appearance.statusText
        assert Path(appearance.backgroundPath) == broken_path
        saved = SettingsStore(runtime.paths.settings_file).load_or_default(AppSettings())
        assert Path(saved.background_image_path) == broken_path
        assert saved.background_brightness == 1.5
    finally:
        _close(engine, root, session)


def test_appearance_qml_resources_and_effect_module_are_in_bundle_contract():
    from scripts.verify_build import EXPECTED_QML_MEMBERS
    from scripts.windows_packaging import (
        ALLOWED_QML_RECURSIVE_MODULE_DIRECTORIES,
        QML_FILES,
    )

    project = Path(__file__).resolve().parents[1]
    source = (project / "packaging/windows/DanbooruMultiSearch.spec").read_text(encoding="utf-8")
    for name in (
        "ActionButton.qml", "AppTheme.qml", "AppearancePanel.qml", "ApplicationBackground.qml",
        "PostCard.qml", "GalleryGrid.qml",
        "FavoritesPage.qml", "FavoriteObjectCard.qml", "FavoriteConfirmDialog.qml",
        "FavoriteTransferDialog.qml",
        "ArtistCombinationPanel.qml",
        "SavedQueryPanel.qml",
        "CacheCleanupDialog.qml",
        "DownloadsPage.qml", "DownloadRequestDialog.qml",
    ):
        assert name in QML_FILES
        assert f"danbooru_multi_search/ui/qml/{name}" in EXPECTED_QML_MEMBERS
        assert f'qml_root / "{name}"' in source
        assert (project / "src/danbooru_multi_search/ui/qml" / name).is_file()
    assert "qtquick/effects" in ALLOWED_QML_RECURSIVE_MODULE_DIRECTORIES
    assert '"pyside6/qml/qtquick/effects"' in source


def test_shared_gallery_keeps_model_and_position_after_detail_and_theme_change(tmp_path):
    engine, root, session, bridge, _runtime = _window(tmp_path)
    try:
        model = bridge.postsModel
        model.set_posts(tuple(
            DanbooruPost(
                id=index + 1, preview_url=None, large_file_url=None, file_url=None,
                score=0, fav_count=0, rating="g", width=100, height=100,
                file_ext="jpg",
            )
            for index in range(30)
        ))
        grid = root.findChild(QObject, "galleryGrid")
        assert _wait_until(lambda: grid.property("count") == 30)
        grid.setProperty("contentY", 340.0)
        APPLICATION.processEvents()
        revision = model.revision
        assert QMetaObject.invokeMethod(
            root, "openPostDetails", Qt.ConnectionType.DirectConnection,
            Q_ARG("QVariant", 4),
        )
        snapshot = session.detail_navigation.gallery_snapshot
        assert snapshot is not None
        assert session.appearance_controller.setTheme("light")
        assert QMetaObject.invokeMethod(
            root, "returnToGallery", Qt.ConnectionType.DirectConnection
        )
        assert _wait_until(lambda: not session.detail_controller.isOpen)
        APPLICATION.processEvents()
        assert model.revision == revision
        assert model.count == 30
        assert grid.property("currentIndex") == snapshot.index
        assert abs(grid.property("contentY") - snapshot.content_y) < 1e-6
    finally:
        _close(engine, root, session)


def test_query_input_precedes_actions_and_advanced_controls_start_collapsed(tmp_path):
    engine, root, session, bridge, _runtime = _window(tmp_path, wide=True)
    try:
        root.setWidth(1280)
        root.setHeight(820)
        APPLICATION.processEvents()
        query_input = root.findChild(QObject, "tagsInput")
        search = root.findChild(QObject, "searchButton")
        toggle = root.findChild(QObject, "advancedConditionsToggle")
        status = root.findChild(QObject, "queryStatusBar")
        assert query_input.property("y") < search.property("y")
        assert search.property("highlighted") is True
        assert toggle.property("checked") is False
        assert status.property("visible") is False
        error = root.findChild(QObject, "queryExpressionError")
        assert error.property("visible") is False
        assert error.property("text") == ""
        assert search.property("enabled") is False
        assert search.property("background").property("color") != root.property("theme").property("accent")
        assert query_input.property("background").property("radius") >= 6
        assert query_input.property("background").property("color") != root.property("theme").property("surface")
        header = root.findChild(QObject, "browseHeader")
        tools = root.findChild(QObject, "browseTools")
        assert abs(tools.property("x") + tools.property("width") - header.property("width")) < 1.5
        assert root.findChild(QObject, "queryTreeList").isVisible() is False
        toggle.setProperty("checked", True)
        APPLICATION.processEvents()
        assert root.findChild(QObject, "queryTreeList").isVisible() is True
        assert bridge.applyAdvancedText("cat AND") is False
        APPLICATION.processEvents()
        assert error.property("visible") is True
        assert "范围 [" not in error.property("text")
    finally:
        _close(engine, root, session)


def test_narrow_detail_uses_full_width_and_has_accessible_information_drawer(tmp_path):
    engine, root, session, bridge, _runtime = _window(tmp_path)
    try:
        root.setWidth(820)
        root.setHeight(600)
        bridge.postsModel.set_posts((DanbooruPost(
            id=1, preview_url=None, large_file_url=None, file_url=None,
            score=0, fav_count=0, rating="g", width=100, height=100,
            file_ext="jpg",
        ),))
        assert QMetaObject.invokeMethod(
            root, "openPostDetails", Qt.ConnectionType.DirectConnection,
            Q_ARG("QVariant", 0),
        )
        APPLICATION.processEvents()
        information_button = root.findChild(QObject, "detailInformationButton")
        assert information_button.property("visible") is True
        page = root.findChild(QObject, "detailPage")
        assert page.property("width") >= 760
        assert QMetaObject.invokeMethod(
            information_button, "clicked", Qt.ConnectionType.DirectConnection
        )
        drawer = root.findChild(QObject, "detailInformationDrawer")
        assert _wait_until(lambda: drawer.property("opened"))
        panel = root.findChild(QObject, "detailInformationPanel")
        assert panel.property("width") <= 400
        assert QMetaObject.invokeMethod(drawer, "close", Qt.ConnectionType.DirectConnection)
    finally:
        _close(engine, root, session)
