"""Persisted appearance controls sharing the application settings owner."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Property, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication, QStyleHints

from danbooru_multi_search.core.settings import SettingsController, SettingsError


class AppearanceController(QObject):
    changed = Signal()
    backgroundChanged = Signal()
    statusChanged = Signal()

    def __init__(
        self,
        settings_controller: SettingsController,
        *,
        style_hints: QStyleHints | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings_controller
        self._style_hints = style_hints or QGuiApplication.styleHints()
        self._system_dark = self._style_hints.colorScheme() != Qt.ColorScheme.Light
        self._style_hints.colorSchemeChanged.connect(self._system_theme_changed)
        self._background_generation = 1
        self._background_error = ""
        self._status = ""
        self._closed = False

    @Property(str, notify=changed)
    def themeMode(self) -> str:
        return self._settings.settings.theme

    @Property(bool, notify=changed)
    def effectiveDark(self) -> bool:
        mode = self.themeMode
        return self._system_dark if mode == "system" else mode == "dark"

    @Property(str, notify=backgroundChanged)
    def backgroundPath(self) -> str:
        return self._settings.settings.background_image_path

    @Property(str, notify=backgroundChanged)
    def backgroundUrl(self) -> str:
        path = self.backgroundPath
        return QUrl.fromLocalFile(path).toString() if path else ""

    @Property(int, notify=backgroundChanged)
    def backgroundGeneration(self) -> int:
        return self._background_generation

    @Property(float, notify=changed)
    def backgroundMask(self) -> float:
        return self._settings.settings.background_mask

    @Property(float, notify=changed)
    def backgroundBlur(self) -> float:
        return self._settings.settings.background_blur

    @Property(float, notify=changed)
    def backgroundBrightness(self) -> float:
        return self._settings.settings.background_brightness

    @Property(float, notify=changed)
    def buttonBackgroundTransparency(self) -> float:
        return self._settings.settings.button_background_transparency

    @Slot(float, result=bool)
    def setButtonBackgroundTransparency(self, value: float) -> bool:
        """Persist only button fill transparency through the shared settings owner."""
        return self._save(button_background_transparency=value)

    @Property(str, notify=statusChanged)
    def statusText(self) -> str:
        return self._status or self._background_error

    @Slot(str, result=bool)
    def setTheme(self, mode: str) -> bool:
        return self._save(theme=mode)

    @Slot(QUrl, result=bool)
    def selectBackground(self, url: QUrl) -> bool:
        if self._closed:
            return False
        path = url.toLocalFile() if url.isLocalFile() else ""
        if not path or not Path(path).is_absolute():
            self._status = "请选择本地图片文件。"
            self.statusChanged.emit()
            return False
        previous_generation = self._background_generation
        saved = self._save(background_image_path=str(Path(path)))
        if saved and self._background_generation == previous_generation:
            self._background_generation += 1
            self._background_error = ""
            self.backgroundChanged.emit()
            self.statusChanged.emit()
        return saved

    @Slot(result=bool)
    def clearBackground(self) -> bool:
        return self._save(background_image_path="")

    @Slot(float, float, float, result=bool)
    def setBackgroundAdjustments(
        self, mask: float, blur: float, brightness: float
    ) -> bool:
        return self._save(
            background_mask=mask,
            background_blur=blur,
            background_brightness=brightness,
        )

    @Slot(int, str, bool)
    def reportBackgroundResult(self, generation: int, url: str, success: bool) -> None:
        if (
            self._closed
            or generation != self._background_generation
            or url != self.backgroundUrl
        ):
            return
        self._background_error = (
            "" if success else "背景图片无法加载，当前显示默认背景；原图片路径已保留。"
        )
        self.statusChanged.emit()

    def _save(self, **changes: object) -> bool:
        if self._closed:
            return False
        previous_path = self.backgroundPath
        try:
            self._settings.update(**changes)
        except SettingsError as exc:
            self._status = f"外观设置未能保存：{exc}"
            self.statusChanged.emit()
            return False
        if self.backgroundPath != previous_path:
            self._background_generation += 1
            self._background_error = ""
            self.backgroundChanged.emit()
        self._status = ""
        self.changed.emit()
        self.statusChanged.emit()
        return True

    def _system_theme_changed(self, scheme: Qt.ColorScheme) -> None:
        if self._closed:
            return
        self._system_dark = scheme != Qt.ColorScheme.Light
        self.changed.emit()

    @Slot()
    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._background_generation += 1
        self._style_hints.colorSchemeChanged.disconnect(self._system_theme_changed)
