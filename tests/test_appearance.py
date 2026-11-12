from __future__ import annotations

import pytest
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Qt, Signal

from danbooru_multi_search.core.settings import (
    AppSettings,
    SettingsController,
    SettingsError,
    SettingsStore,
)
from danbooru_multi_search.ui.appearance import AppearanceController


class StyleHints(QObject):
    colorSchemeChanged = Signal(object)

    def __init__(self):
        super().__init__()
        self.scheme = Qt.ColorScheme.Dark

    def colorScheme(self):
        return self.scheme

    def change(self, scheme):
        self.scheme = scheme
        self.colorSchemeChanged.emit(scheme)


def _appearance(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    settings = SettingsController(store, AppSettings())
    hints = StyleHints()
    appearance = AppearanceController(settings, style_hints=hints)
    return appearance, settings, store, hints


def test_theme_modes_follow_system_and_survive_restart(tmp_path):
    appearance, settings, store, hints = _appearance(tmp_path)
    assert appearance.effectiveDark is True
    hints.change(Qt.ColorScheme.Light)
    assert appearance.effectiveDark is False
    assert appearance.setTheme("dark")
    assert appearance.effectiveDark is True
    hints.change(Qt.ColorScheme.Light)
    assert appearance.effectiveDark is True
    assert appearance.setTheme("light")
    assert appearance.effectiveDark is False
    assert appearance.setTheme("system")
    assert store.load_or_default(AppSettings()).theme == "system"
    assert settings.settings.theme == "system"
    appearance.shutdown()


def test_appearance_csv_and_cache_updates_keep_each_others_fields(tmp_path):
    appearance, settings, store, _hints = _appearance(tmp_path)
    csv_path = (tmp_path / "标签 100%.csv").resolve()
    background = (tmp_path / "背景 100%.png").resolve()
    settings.set_tag_csv_path(csv_path)
    assert appearance.selectBackground(QUrl.fromLocalFile(str(background)))
    settings.set_image_cache_limit_bytes(768 * 1024 * 1024)
    assert appearance.setTheme("light")
    assert appearance.setBackgroundAdjustments(0.4, 0.3, 1.5)
    saved = store.load_or_default(AppSettings())
    assert saved == settings.settings
    assert saved.tag_csv_path == str(csv_path)
    assert saved.image_cache_limit_bytes == 768 * 1024 * 1024
    assert saved.background_image_path == str(background)
    assert (saved.background_mask, saved.background_blur, saved.background_brightness) == (
        0.4, 0.3, 1.5
    )
    assert appearance.backgroundUrl == QUrl.fromLocalFile(str(background)).toString()
    appearance.shutdown()


def test_old_background_result_cannot_replace_new_selection_or_same_path_retry(tmp_path):
    appearance, _settings, store, _hints = _appearance(tmp_path)
    first = QUrl.fromLocalFile(str((tmp_path / "first.png").resolve()))
    second = QUrl.fromLocalFile(str((tmp_path / "second.png").resolve()))
    assert appearance.selectBackground(first)
    first_generation = appearance.backgroundGeneration
    assert appearance.selectBackground(second)
    appearance.reportBackgroundResult(first_generation, first.toString(), False)
    assert appearance.statusText == ""
    current_generation = appearance.backgroundGeneration
    appearance.reportBackgroundResult(current_generation, second.toString(), False)
    assert "默认背景" in appearance.statusText
    assert Path(store.load_or_default(AppSettings()).background_image_path) == Path(
        second.toLocalFile()
    )
    assert appearance.selectBackground(second)
    assert appearance.backgroundGeneration > current_generation
    appearance.reportBackgroundResult(current_generation, second.toString(), False)
    assert appearance.statusText == ""
    assert appearance.clearBackground()
    assert appearance.backgroundPath == ""
    appearance.shutdown()


def test_appearance_save_failure_keeps_confirmed_settings_and_reports_error(
    tmp_path, monkeypatch
):
    appearance, settings, store, _hints = _appearance(tmp_path)
    assert appearance.setTheme("dark")
    confirmed = settings.settings

    def fail_save(_settings):
        raise SettingsError("simulated failure")

    monkeypatch.setattr(store, "save", fail_save)
    assert appearance.setTheme("light") is False
    assert appearance.themeMode == "dark"
    assert settings.settings == confirmed
    assert "未能保存" in appearance.statusText
    appearance.shutdown()


@pytest.mark.parametrize(
    "changes",
    (
        {"background_image_path": "relative.png"},
        {"background_mask": True},
        {"background_mask": -0.1},
        {"background_mask": 1.1},
        {"background_blur": float("nan")},
        {"background_blur": float("inf")},
        {"background_brightness": -0.1},
        {"background_brightness": 2.1},
        {"background_brightness": 10 ** 400},
    ),
)
def test_background_settings_reject_invalid_values(changes):
    with pytest.raises(SettingsError):
        AppSettings.from_mapping(changes)


def test_closed_appearance_rejects_actions_and_late_background_results(tmp_path):
    appearance, settings, _store, hints = _appearance(tmp_path)
    confirmed = settings.settings
    appearance.shutdown()
    appearance.shutdown()
    assert appearance.setTheme("light") is False
    assert appearance.selectBackground(QUrl("https://example.invalid/bg.png")) is False
    hints.change(Qt.ColorScheme.Light)
    appearance.reportBackgroundResult(appearance.backgroundGeneration, "", False)
    assert settings.settings == confirmed
    assert appearance.statusText == ""
