from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from PySide6.QtCore import QObject, QPoint, QPointF, Signal, Qt
from PySide6.QtGui import QGuiApplication, QInputMethodEvent
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from danbooru_multi_search.danbooru import (
    TagIndexSnapshot,
    TagSuggestion,
)
from danbooru_multi_search.ui.launcher import create_qml_engine


APPLICATION = QGuiApplication.instance() or QGuiApplication([])


class FakeReply(QObject):
    metaDataChanged = Signal()
    readyRead = Signal()
    finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.abort_count = 0

    def abort(self) -> None:
        self.abort_count += 1

    def readAll(self) -> bytes:
        return b""

    def error(self):
        return QNetworkReply.NetworkError.NoError

    def errorString(self) -> str:
        return ""

    def attribute(self, attribute):
        if attribute == QNetworkRequest.Attribute.HttpStatusCodeAttribute:
            return 200
        return None

    def rawHeader(self, name: str) -> bytes:
        return b""

    def deleteLater(self) -> None:
        pass


class FakeNetworkManager:
    def __init__(self) -> None:
        self.requests = []
        self.replies: list[FakeReply] = []

    def get(self, request):
        reply = FakeReply()
        self.requests.append(request)
        self.replies.append(reply)
        return reply


def _wait_until(predicate, timeout_ms: int = 1_500) -> bool:
    elapsed = 0
    while elapsed < timeout_ms:
        APPLICATION.processEvents()
        if predicate():
            return True
        QTest.qWait(10)
        elapsed += 10
    return predicate()


def _create_window():
    manager = FakeNetworkManager()
    engine, gallery_controller = create_qml_engine(
        APPLICATION,
        danbooru_base_url="https://example.invalid",
        network_manager=manager,
    )
    root = engine.rootObjects()[0]
    tag_controller = engine.rootContext().contextProperty(
        "tagSuggestionController"
    )
    tag_controller._index = TagIndexSnapshot.build(
        (
            TagSuggestion("cat", ("猫",), 20, 0),
            TagSuggestion("cat_ears", ("猫耳",), 10, 0),
        )
    )
    tags_input = root.findChild(QObject, "tagsInput")
    suggestion_popup = root.findChild(QObject, "suggestionPopup")
    suggestion_list = root.findChild(QObject, "suggestionList")
    return (
        engine,
        root,
        gallery_controller,
        tag_controller,
        tags_input,
        suggestion_popup,
        suggestion_list,
        manager,
    )


def _show_suggestions(tags_input, suggestion_popup, text: str, cursor: int):
    tags_input.setProperty("text", text)
    tags_input.setProperty("cursorPosition", cursor)
    tags_input.forceActiveFocus()
    assert _wait_until(lambda: suggestion_popup.property("opened"))


def _close_window(engine) -> None:
    APPLICATION.aboutToQuit.emit()
    engine.deleteLater()
    APPLICATION.processEvents()


def test_suggestion_popup_keeps_input_focus_and_keyboard_navigation_cycles():
    (
        engine,
        root,
        _gallery_controller,
        tag_controller,
        tags_input,
        suggestion_popup,
        suggestion_list,
        manager,
    ) = _create_window()
    try:
        _show_suggestions(tags_input, suggestion_popup, "猫", 1)
        original_cursor = tags_input.property("cursorPosition")

        assert root.activeFocusItem() == tags_input
        assert suggestion_list.property("currentIndex") == 0
        QTest.keyClick(root, Qt.Key.Key_Down)
        assert suggestion_list.property("currentIndex") == 1
        assert tags_input.property("cursorPosition") == original_cursor
        assert root.activeFocusItem() == tags_input

        QTest.keyClick(root, Qt.Key.Key_Up)
        assert suggestion_list.property("currentIndex") == 0
        assert tags_input.property("cursorPosition") == original_cursor

        QTest.keyClick(root, Qt.Key.Key_Escape)
        assert _wait_until(lambda: not suggestion_popup.property("opened"))
        assert tag_controller.suggestionsModel.count == 0
        assert tags_input.property("text") == "猫"
        assert manager.requests == []
    finally:
        _close_window(engine)


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter])
def test_return_keys_select_once_search_once_and_do_not_restart_completion(key):
    (
        engine,
        root,
        gallery_controller,
        tag_controller,
        tags_input,
        suggestion_popup,
        _suggestion_list,
        manager,
    ) = _create_window()
    try:
        _show_suggestions(tags_input, suggestion_popup, "猫 dog", 1)

        QTest.keyClick(root, key)

        assert _wait_until(lambda: len(manager.requests) == 1)
        assert tags_input.property("text") == "cat dog"
        assert suggestion_popup.property("opened") is False
        assert root.activeFocusItem() == tags_input
        assert gallery_controller.busy is True
        QTest.qWait(350)
        APPLICATION.processEvents()
        assert len(manager.requests) == 1
        assert tag_controller.debouncePending is False
    finally:
        _close_window(engine)


def test_mouse_click_only_replaces_fragment_and_outside_click_closes_popup():
    (
        engine,
        root,
        _gallery_controller,
        tag_controller,
        tags_input,
        suggestion_popup,
        suggestion_list,
        manager,
    ) = _create_window()
    try:
        _show_suggestions(tags_input, suggestion_popup, "猫 dog", 1)
        suggestion_row = suggestion_list.property("currentItem")
        assert suggestion_row is not None
        center = suggestion_row.mapToScene(
            QPointF(suggestion_row.width() / 2, suggestion_row.height() / 2)
        )

        QTest.mouseClick(
            root,
            Qt.MouseButton.LeftButton,
            pos=QPoint(round(center.x()), round(center.y())),
        )

        assert _wait_until(lambda: not suggestion_popup.property("opened"))
        assert tags_input.property("text") == "cat dog"
        assert manager.requests == []
        QTest.qWait(350)
        assert manager.requests == []
        assert tag_controller.debouncePending is False

        _show_suggestions(tags_input, suggestion_popup, "猫 dog", 1)
        QTest.mouseClick(
            root,
            Qt.MouseButton.LeftButton,
            pos=QPoint(8, 8),
        )
        assert _wait_until(lambda: not suggestion_popup.property("opened"))
        assert tag_controller.suggestionsModel.count == 0
        assert tags_input.property("text") == "猫 dog"
        assert manager.requests == []
    finally:
        _close_window(engine)


def test_visible_csv_import_controls_exist_without_opening_native_dialog():
    (
        engine,
        root,
        _gallery_controller,
        _tag_controller,
        _tags_input,
        _suggestion_popup,
        _suggestion_list,
        _manager,
    ) = _create_window()
    try:
        import_button = root.findChild(QObject, "importTagsButton")
        import_status = root.findChild(QObject, "tagImportStatus")
        file_dialog = root.findChild(QObject, "tagCsvDialog")

        assert import_button is not None
        assert import_button.property("visible") is True
        assert import_button.property("enabled") is True
        assert import_status is not None
        assert "尚未导入" in import_status.property("text")
        assert file_dialog is not None
    finally:
        _close_window(engine)


def test_input_method_composition_return_does_not_select_or_search():
    (
        engine,
        root,
        _gallery_controller,
        _tag_controller,
        tags_input,
        suggestion_popup,
        suggestion_list,
        manager,
    ) = _create_window()
    try:
        _show_suggestions(tags_input, suggestion_popup, "cat", 3)
        assert suggestion_list.property("currentIndex") == 0
        composing = QInputMethodEvent("x", [])
        QGuiApplication.sendEvent(root, composing)
        assert tags_input.property("inputMethodComposing") is True

        QTest.keyClick(root, Qt.Key.Key_Return)

        assert manager.requests == []
        assert tags_input.property("text") == "cat"
        assert suggestion_popup.property("opened") is True
    finally:
        QGuiApplication.sendEvent(root, QInputMethodEvent("", []))
        _close_window(engine)
