from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from PySide6.QtCore import (
    Property,
    Q_ARG,
    QCoreApplication,
    QEvent,
    QMetaObject,
    QObject,
    QPointF,
    QTimeZone,
    QUrl,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QGuiApplication, QInputMethodEvent
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from danbooru_multi_search.core.settings import (
    AppSettings,
    SettingsController,
    SettingsStore,
)
from danbooru_multi_search.danbooru import (
    DanbooruDetailController,
    DanbooruM2QueryController,
    DanbooruPost,
    QueryExecutionAccepted,
    QueryExecutionBound,
    QueryExecutionCounts,
    QueryExecutionErrorCode,
    QueryExecutionFailure,
    QueryExecutionSnapshot,
    QueryExecutionState,
    QueryBranchPosition,
    TagIndexSnapshot,
    TagSuggestion,
    TagTranslationStore,
)
from danbooru_multi_search.danbooru.autocomplete import (
    DanbooruTagSuggestionController,
)
from danbooru_multi_search.storage import HistoryRepository
from danbooru_multi_search.storage.favorites import FavoriteRepository
from danbooru_multi_search.ui.favorites import FavoriteController
from danbooru_multi_search.ui.favorite_collection import FavoriteCollectionController
from danbooru_multi_search.ui.favorite_transfer import FavoriteTransferController
from danbooru_multi_search.ui.gallery_selection import GallerySelectionController
from danbooru_multi_search.ui.detail_favorites import DetailFavoritesController
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.ui.detail_navigation import DetailNavigationController
from danbooru_multi_search.ui.appearance import AppearanceController
from danbooru_multi_search.ui.launcher import configure_controls_style
from danbooru_multi_search.ui.image_cache import (
    ImageCacheController,
    ImageNetworkAccessManagerFactory,
)
from danbooru_multi_search.ui.query_editor import QueryTreeEditor
from danbooru_multi_search.ui.query_state import M2QueryBridgeController
from danbooru_multi_search.ui.query_forms import (
    default_query_form_values,
)
from danbooru_multi_search.ui.session_state import BrowserSessionController
from danbooru_multi_search.ui.window_state import ScreenGeometry, WindowGeometry


APPLICATION = QGuiApplication.instance() or QGuiApplication([])
QML_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "danbooru_multi_search"
    / "ui"
    / "qml"
)


class _UiExecutor(QObject):
    executionBound = Signal(object)
    firstPageAccepted = Signal(object)
    snapshotChanged = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.snapshot = QueryExecutionSnapshot(
            0,
            QueryExecutionState.IDLE,
            QueryExecutionCounts(),
            (),
            (),
        )
        self.current_execution_id: int | None = None
        self.start_calls: list[object] = []
        self.cancel_calls = 0
        self.retry_calls = 0
        self._active = False

    def start(self, plan, limits=None, *, binding_token=None) -> int:
        del limits
        self.current_execution_id = (self.current_execution_id or 0) + 1
        execution_id = self.current_execution_id
        self.start_calls.append(plan)
        self._active = True
        self.snapshot = QueryExecutionSnapshot(
            execution_id,
            QueryExecutionState.WAITING,
            QueryExecutionCounts(),
            (),
            (),
        )
        self.executionBound.emit(QueryExecutionBound(execution_id, binding_token))
        self.snapshotChanged.emit(self.snapshot)
        return execution_id

    def is_execution_active(self, execution_id: int) -> bool:
        return self._active and self.current_execution_id == execution_id

    def accept_first_page(self) -> None:
        assert self.current_execution_id is not None
        self.firstPageAccepted.emit(
            QueryExecutionAccepted(
                self.current_execution_id,
                QueryBranchPosition(0, 1),
                "qml-test-task",
            )
        )

    def cancel(self) -> bool:
        self.cancel_calls += 1
        if not self._active or self.current_execution_id is None:
            return False
        self._active = False
        self.snapshot = QueryExecutionSnapshot(
            self.current_execution_id,
            QueryExecutionState.PARTIAL_CANCELLED,
            self.snapshot.counts,
            self.snapshot.posts,
            self.snapshot.branch_progress,
        )
        self.snapshotChanged.emit(self.snapshot)
        return True

    def retry_failed_page(self) -> bool:
        if self.snapshot.failure is None or self.current_execution_id is None:
            return False
        self.retry_calls += 1
        self._active = True
        self.snapshot = QueryExecutionSnapshot(
            self.current_execution_id,
            QueryExecutionState.RUNNING,
            self.snapshot.counts,
            self.snapshot.posts,
            self.snapshot.branch_progress,
        )
        self.snapshotChanged.emit(self.snapshot)
        return True

    def fail_current_page(self) -> None:
        assert self.current_execution_id is not None
        self._active = False
        self.snapshot = QueryExecutionSnapshot(
            self.current_execution_id,
            QueryExecutionState.FAILED,
            self.snapshot.counts,
            self.snapshot.posts,
            self.snapshot.branch_progress,
            failure=QueryExecutionFailure(
                QueryBranchPosition(0, 1),
                "cat",
                QueryExecutionErrorCode.HTTP,
                500,
                "simulated failure",
            ),
        )
        self.snapshotChanged.emit(self.snapshot)

    def shutdown(self) -> None:
        self._active = False


def _wait_until(predicate, timeout_ms: int = 1_500) -> bool:
    elapsed = 0
    while elapsed < timeout_ms:
        APPLICATION.processEvents()
        if predicate():
            return True
        QTest.qWait(10)
        elapsed += 10
    return predicate()


def _find(root: QObject, name: str) -> QObject:
    found = root.findChild(QObject, name)
    assert found is not None, name
    return found


def _find_visual_item(root, name: str):
    pending = [root.contentItem()]
    while pending:
        item = pending.pop()
        if item.objectName() == name:
            return item
        pending.extend(item.childItems())
    return None


def _click(root, item: QQuickItem) -> None:
    del root
    assert QMetaObject.invokeMethod(
        item,
        "clicked",
        Qt.ConnectionType.DirectConnection,
    )
    APPLICATION.processEvents()


def _post(post_id: int = 1) -> DanbooruPost:
    image = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
        "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    return DanbooruPost(
        id=post_id,
        preview_url=image,
        large_file_url=image,
        file_url=image,
        score=12,
        fav_count=3,
        rating="g",
        width=640,
        height=480,
        file_ext="png",
        created_at="2026-01-02T03:04:05.000000Z",
        general_tags=("blue_sky",),
        artist_tags=("sample_artist",),
    )


def _create_window(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    executor = _UiExecutor()
    index = TagIndexSnapshot.build(
        (
            TagSuggestion("cat", ("猫",), 100, 0),
            TagSuggestion("cat_ears", ("猫耳",), 90, 0),
            TagSuggestion("sample_artist", ("示例画师",), 20, 1),
            TagSuggestion("sample_painter", ("另一画师",), 10, 1),
        )
    )
    translations = TagTranslationStore(index)
    core = DanbooruM2QueryController(
        "https://example.invalid",
        executor=executor,
        tag_store=translations,
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    bridge = M2QueryBridgeController(
        core,
        query_editor=QueryTreeEditor("cat"),
        time_zone=QTimeZone.utc(),
    )
    database = Database(tmp_path / "qml.sqlite3")
    database.initialize()
    session = BrowserSessionController(
        HistoryRepository(database),
        (ScreenGeometry(WindowGeometry(0, 0, 1280, 820), primary=True),),
        m2_controller=core,
        query_bridge=bridge,
    )
    tags = DanbooruTagSuggestionController(
        "https://example.invalid",
        translation_store=translations,
        connect_application_quit=False,
    )
    details = DanbooruDetailController(
        "https://example.invalid",
        translations,
        local_timezone=timezone.utc,
    )
    detail_navigation = DetailNavigationController(details, core.posts_model)
    favorites = FavoriteController(FavoriteRepository(database))
    selection = GallerySelectionController(core.posts_model, favorites)
    detail_favorites = DetailFavoritesController(details, favorites)
    collection = FavoriteCollectionController(favorites._repository, favorites, detail_navigation)
    transfer = FavoriteTransferController(database.path, favorites)
    settings_controller = SettingsController(
        SettingsStore(tmp_path / "settings.json"), AppSettings()
    )
    appearance = AppearanceController(settings_controller)
    images = ImageCacheController(tmp_path / "image-cache")
    factory = ImageNetworkAccessManagerFactory(images)
    configure_controls_style()
    engine = QQmlApplicationEngine()
    qml_warnings = []
    engine.warnings.connect(qml_warnings.extend)
    engine.setNetworkAccessManagerFactory(factory)
    context = engine.rootContext()
    context.setContextProperty("searchController", bridge)
    context.setContextProperty("tagSuggestionController", tags)
    context.setContextProperty("detailController", details)
    context.setContextProperty("detailNavigation", detail_navigation)
    context.setContextProperty("appearanceController", appearance)
    context.setContextProperty("sessionController", session)
    context.setContextProperty("imageCacheController", images)
    context.setContextProperty("favoriteTransfer", transfer)
    context.setContextProperty("favoriteCollection", collection)
    context.setContextProperty("favoriteController", favorites)
    context.setContextProperty("gallerySelection", selection)
    context.setContextProperty("detailFavorites", detail_favorites)
    engine.load(QUrl.fromLocalFile(str(QML_DIRECTORY / "M2Main.qml")))
    assert engine.rootObjects(), [error.toString() for error in engine.warnings()]
    root = engine.rootObjects()[0]
    return {
        "engine": engine,
        "root": root,
        "executor": executor,
        "core": core,
        "bridge": bridge,
        "session": session,
        "tags": tags,
        "details": details,
        "detail_navigation": detail_navigation,
        "appearance": appearance,
        "images": images,
        "factory": factory,
        "favorites": favorites,
        "collection": collection,
        "transfer": transfer,
        "selection": selection,
        "detail_favorites": detail_favorites,
        "qml_warnings": qml_warnings,
    }


def _close_window(values) -> None:
    values["transfer"].begin_shutdown()
    values["transfer"].shutdown()
    values["collection"].shutdown()
    values["favorites"].shutdown()
    values["selection"].shutdown()
    values["detail_favorites"].shutdown()
    values["bridge"].beginClosing()
    values["core"].shutdown()
    values["session"].begin_shutdown()
    values["detail_navigation"].shutdown()
    values["appearance"].shutdown()
    values["tags"].shutdown()
    values["images"].shutdown()
    values["root"].close()
    values["engine"].deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    APPLICATION.processEvents()


def test_m2_root_loads_split_panels_and_has_no_pagination(tmp_path):
    values = _create_window(tmp_path)
    try:
        root = values["root"]
        for name in (
            "tagsInput",
            "ratingFilter",
            "pageSizeInput",
            "minimumScoreInput",
            "minimumFavCountInput",
            "publishedFromInput",
            "publishedUntilInput",
            "minimumAgeDaysInput",
            "maximumAgeDaysInput",
            "staticFileFilter",
            "gifFileFilter",
            "webmFileFilter",
            "mp4FileFilter",
            "minimumWidthInput",
            "minimumHeightInput",
            "minimumAspectRatioInput",
            "maximumAspectRatioInput",
            "landscapeFilter",
            "portraitFilter",
            "squareFilter",
            "blockedTagsInput",
            "sortFieldInput",
            "sortDirectionInput",
            "candidateLimitInput",
            "branchPageLimitInput",
            "activeTimeLimitInput",
            "queryTreeList",
            "addVisualTagButton",
            "wrapGroupButton",
            "insertArtistGroupButton",
            "historyList",
            "queryStatusBar",
            "galleryGrid",
            "searchButton",
            "globalRankButton",
            "conditionalRankButton",
            "latestButton",
            "cancelButton",
            "refreshButton",
            "retryButton",
            "detailPage",
        ):
            _find(root, name)
        assert root.findChild(QObject, "previousPageButton") is None
        assert root.findChild(QObject, "nextPageButton") is None
        assert root.property("compactLayout") is False
        assert not [
            warning.toString()
            for warning in values["qml_warnings"]
            if "TypeError" in warning.toString()
            or "ReferenceError" in warning.toString()
            or "is not a type" in warning.toString()
        ]

        root.setWidth(800)
        APPLICATION.processEvents()
        assert root.property("compactLayout") is True
        assert _find(root, "openQueryDrawerButton").property("visible") is True
    finally:
        _close_window(values)


@pytest.mark.parametrize(
    ("window_width", "expected_compact"),
    ((983, False), (651, True)),
)
def test_status_count_cards_use_two_rows_when_content_is_narrow(
    tmp_path,
    window_width,
    expected_compact,
):
    values = _create_window(tmp_path)
    try:
        root = values["root"]
        root.setWidth(window_width)
        assert _wait_until(
            lambda: root.width() == window_width
            and root.property("compactLayout") is expected_compact
        )

        status_bar = _find(root, "queryStatusBar")
        assert status_bar.property("visible") is False
        assert values["bridge"].executeStandard()
        APPLICATION.processEvents()
        assert status_bar.property("visible") is True
        card_names = (
            "remoteRowsCountCard",
            "uniqueCandidatesCountCard",
            "localMatchesCountCard",
            "visibleCountCard",
        )
        cards = tuple(_find_visual_item(root, name) for name in card_names)
        assert all(card is not None for card in cards)
        assert _wait_until(
            lambda: all(card.width() > 0 and card.height() > 0 for card in cards)
        )

        positions = tuple(
            card.mapToItem(status_bar, QPointF(0, 0)) for card in cards
        )
        tolerance = 1.5
        assert abs(positions[0].y() - positions[1].y()) <= tolerance
        assert abs(positions[2].y() - positions[3].y()) <= tolerance
        assert positions[2].y() > positions[0].y()
        assert abs(positions[0].x() - positions[2].x()) <= tolerance
        assert abs(positions[1].x() - positions[3].x()) <= tolerance
        assert positions[1].x() > positions[0].x()
        assert all(
            position.x() >= -tolerance
            and position.x() + card.width() <= status_bar.width() + tolerance
            for card, position in zip(cards, positions, strict=True)
        )
        assert abs(cards[0].width() - cards[1].width()) <= tolerance
        assert abs(cards[2].width() - cards[3].width()) <= tolerance

        visible_card = cards[3]
        visible_position = visible_card.mapToItem(
            root.contentItem(), QPointF(0, 0)
        )
        assert visible_position.x() >= -tolerance
        assert (
            visible_position.x() + visible_card.width()
            <= root.width() + tolerance
        )
    finally:
        _close_window(values)


@pytest.mark.parametrize(
    ("button_name", "expected_mode", "expected_expression", "expected_order"),
    (
        ("searchButton", "standard", "cat", None),
        ("globalRankButton", "rank", None, "rank"),
        ("conditionalRankButton", "rank", "cat", "rank"),
        ("latestButton", "latest", None, None),
    ),
)
def test_each_query_button_starts_exactly_one_execution(
    tmp_path,
    button_name,
    expected_mode,
    expected_expression,
    expected_order,
):
    values = _create_window(tmp_path)
    try:
        button = _find(values["root"], button_name)
        assert button.property("enabled") is True
        _click(values["root"], button)
        assert len(values["executor"].start_calls) == 1
        plan = values["executor"].start_calls[0]
        actual_order = plan.remote_order.value if plan.remote_order is not None else None
        assert actual_order == expected_order
        assert values["core"].definition.mode.value == expected_mode
        assert values["core"].definition.normalized_expression == expected_expression
    finally:
        _close_window(values)


def test_ime_return_does_not_execute_and_completion_selection_only_edits(tmp_path):
    values = _create_window(tmp_path)
    try:
        root = values["root"]
        tags_input = _find(root, "tagsInput")
        tags_input.setProperty("text", "ca OR dog")
        tags_input.setProperty("cursorPosition", 2)
        tags_input.forceActiveFocus()
        root.requestActivate()
        assert _wait_until(
            lambda: _find(root, "suggestionPopup").property("opened")
        )

        QTest.keyClick(root, Qt.Key.Key_Return)
        APPLICATION.processEvents()
        assert values["executor"].start_calls == []
        assert tags_input.property("text") == "cat OR dog"
        assert tags_input.property("cursorPosition") == 3

        tags_input.setProperty("text", "ca")
        tags_input.setProperty("cursorPosition", 2)
        assert _wait_until(
            lambda: _find(root, "suggestionPopup").property("opened")
        )
        QTest.keyClick(root, Qt.Key.Key_Escape)
        assert _wait_until(
            lambda: not _find(root, "suggestionPopup").property("opened")
        )
        assert values["executor"].start_calls == []

        tags_input.setProperty("text", "cat")
        tags_input.setProperty("cursorPosition", 3)

        composing = QInputMethodEvent("猫", [])
        QCoreApplication.sendEvent(tags_input, composing)
        QTest.keyClick(root, Qt.Key.Key_Return)
        assert values["executor"].start_calls == []
        QCoreApplication.sendEvent(tags_input, QInputMethodEvent())

        QTest.keyClick(root, Qt.Key.Key_Return)
        assert len(values["executor"].start_calls) == 1
    finally:
        _close_window(values)


def test_escape_cancels_pending_completion_before_closing_compact_panel(tmp_path):
    values = _create_window(tmp_path)
    try:
        root = values["root"]
        root.setWidth(800)
        APPLICATION.processEvents()
        drawer = _find(root, "queryDrawer")
        assert QMetaObject.invokeMethod(
            drawer,
            "open",
            Qt.ConnectionType.DirectConnection,
        )
        assert _wait_until(lambda: drawer.property("opened"))
        tags_input = _find(root, "tagsInput")
        tags_input.setProperty("text", "unlisted_tag")
        tags_input.setProperty("cursorPosition", len("unlisted_tag"))
        tags_input.forceActiveFocus()
        root.requestActivate()
        assert _wait_until(lambda: values["tags"].debouncePending)
        assert _find(root, "suggestionPopup").property("opened") is False

        QTest.keyClick(root, Qt.Key.Key_Escape)

        assert values["tags"].debouncePending is False
        assert drawer.property("opened") is True
        QTest.qWait(350)
        assert _find(root, "suggestionPopup").property("opened") is False
    finally:
        _close_window(values)


def test_widening_window_closes_compact_query_drawer(tmp_path):
    values = _create_window(tmp_path)
    try:
        root = values["root"]
        root.setWidth(800)
        APPLICATION.processEvents()
        drawer = _find(root, "queryDrawer")
        assert QMetaObject.invokeMethod(
            drawer,
            "open",
            Qt.ConnectionType.DirectConnection,
        )
        assert _wait_until(lambda: drawer.property("opened"))

        root.setWidth(1280)

        assert _wait_until(lambda: not drawer.property("opened"))
        assert root.property("compactLayout") is False
    finally:
        _close_window(values)


def test_tree_revision_clears_stale_selection_before_second_delete(tmp_path):
    values = _create_window(tmp_path)
    try:
        root = values["root"]
        _find(root, "advancedConditionsToggle").setProperty("checked", True)
        bridge = values["bridge"]
        assert bridge.applyAdvancedText("cat AND dog AND bird") is True
        editor = _find(root, "queryEditorPanel")
        editor.setProperty("selectedPath", [0])
        editor.setProperty("selectedKind", "tag")
        remove_button = _find(root, "removeNodeButton")

        _click(root, remove_button)

        assert bridge.canonicalExpression == "dog AND bird"
        assert editor.property("selectedPath") is None
        assert editor.property("selectedKind") == ""
        _click(root, remove_button)
        assert bridge.canonicalExpression == "dog AND bird"
    finally:
        _close_window(values)


def test_clicked_suggestion_row_supplies_its_own_tag_and_artist_category(tmp_path):
    values = _create_window(tmp_path)
    try:
        root = values["root"]
        _find(root, "advancedConditionsToggle").setProperty("checked", True)
        visual_input = _find(root, "visualTagInput")
        visual_input.forceActiveFocus()
        root.requestActivate()
        visual_input.setProperty("text", "ca")
        visual_input.setProperty("cursorPosition", 2)
        assert _wait_until(
            lambda: _find(root, "suggestionPopup").property("opened")
        )
        suggestion_list = _find(root, "suggestionList")
        assert suggestion_list.property("currentIndex") == 0
        second_row = _find_visual_item(root, "suggestionRow_1")
        assert second_row is not None
        _click(root, second_row)
        assert visual_input.property("text") == "cat_ears"
        _click(root, _find(root, "addVisualTagButton"))
        assert "cat_ears" in values["bridge"].canonicalExpression

        artist_input = _find(root, "artistInput")
        artist_input.forceActiveFocus()
        artist_input.setProperty("text", "sample")
        artist_input.setProperty("cursorPosition", len("sample"))
        assert _wait_until(
            lambda: _find(root, "suggestionPopup").property("opened")
        )
        assert suggestion_list.property("currentIndex") == 0
        second_artist = _find_visual_item(root, "suggestionRow_1")
        assert second_artist is not None
        _click(root, second_artist)
        editor = _find(root, "queryEditorPanel")
        artists = editor.property("confirmedArtists")
        if hasattr(artists, "toVariant"):
            artists = artists.toVariant()
        assert artists == [{"name": "sample_painter", "category": 1}]
    finally:
        _close_window(values)


def test_sort_direction_and_filter_text_survive_workspace_recreation(tmp_path):
    values = _create_window(tmp_path)
    try:
        root = values["root"]
        bridge = values["bridge"]
        tabs = _find(root, "workspaceTabs")
        tabs.setProperty("currentIndex", 1)
        APPLICATION.processEvents()
        sort_field = _find(root, "sortFieldInput")
        sort_field.setProperty("currentIndex", 1)
        assert QMetaObject.invokeMethod(
            sort_field,
            "activated",
            Qt.ConnectionType.DirectConnection,
            Q_ARG(int, 1),
        )
        assert bridge.draftFormValues["sortField"] == "score"
        assert bridge.draftFormValues["sortDirection"] == "descending"
        assert bridge.hasDraftError is False

        score_input = _find(root, "minimumScoreInput")
        score_input.setProperty("text", "17")
        assert QMetaObject.invokeMethod(
            score_input,
            "textEdited",
            Qt.ConnectionType.DirectConnection,
        )
        assert bridge.draftFormValues["minimumScore"] == "17"

        root.setWidth(800)
        APPLICATION.processEvents()
        replacement = _find(root, "minimumScoreInput")
        assert replacement is not score_input
        assert replacement.property("text") == "17"
        assert bridge.draftFormValues["minimumScore"] == "17"
    finally:
        _close_window(values)


@pytest.mark.parametrize(
    ("field_index", "field_value"),
    (
        (1, "score"),
        (2, "fav_count"),
        (3, "created_at"),
        (4, "post_id"),
    ),
)
def test_numeric_sort_fields_only_offer_ascending_and_descending(
    tmp_path,
    field_index,
    field_value,
):
    values = _create_window(tmp_path)
    try:
        root = values["root"]
        bridge = values["bridge"]
        _find(root, "workspaceTabs").setProperty("currentIndex", 1)
        sort_field = _find(root, "sortFieldInput")
        direction = _find(root, "sortDirectionInput")
        sort_field.setProperty("currentIndex", field_index)
        assert QMetaObject.invokeMethod(
            sort_field,
            "activated",
            Qt.ConnectionType.DirectConnection,
            Q_ARG(int, field_index),
        )

        assert bridge.draftFormValues["sortField"] == field_value
        assert bridge.draftFormValues["sortDirection"] == "descending"
        assert direction.property("count") == 2
        assert direction.property("currentValue") == "descending"
        assert bridge.hasDraftError is False

        direction.setProperty("currentIndex", 0)
        assert QMetaObject.invokeMethod(
            direction,
            "activated",
            Qt.ConnectionType.DirectConnection,
            Q_ARG(int, 0),
        )
        assert direction.property("currentValue") == "ascending"
        assert bridge.draftFormValues["sortDirection"] == "ascending"
        assert bridge.hasDraftError is False
    finally:
        _close_window(values)


def test_source_and_rank_sort_fields_only_offer_original_direction(tmp_path):
    values = _create_window(tmp_path)
    try:
        root = values["root"]
        bridge = values["bridge"]
        _find(root, "workspaceTabs").setProperty("currentIndex", 1)
        sort_field = _find(root, "sortFieldInput")
        direction = _find(root, "sortDirectionInput")

        assert sort_field.property("currentValue") == "source_order"
        assert direction.property("count") == 1
        assert direction.property("currentValue") == "original"
        assert direction.property("enabled") is False
        assert bridge.hasDraftError is False

        assert bridge.setDraftMode("rank", False) is True
        sort_field.setProperty("currentIndex", 5)
        assert QMetaObject.invokeMethod(
            sort_field,
            "activated",
            Qt.ConnectionType.DirectConnection,
            Q_ARG(int, 5),
        )
        assert bridge.draftFormValues["sortField"] == "rank"
        assert bridge.draftFormValues["sortDirection"] == "original"
        assert direction.property("count") == 1
        assert direction.property("currentValue") == "original"
        assert direction.property("enabled") is False
        assert bridge.hasDraftError is False
    finally:
        _close_window(values)


def test_cancel_button_uses_unified_session_entry_once(tmp_path):
    values = _create_window(tmp_path)
    try:
        _click(values["root"], _find(values["root"], "searchButton"))
        assert values["bridge"].canCancel is True
        assert values["executor"]._active is True
        assert values["executor"].cancel_calls == 0
        _click(values["root"], _find(values["root"], "cancelButton"))

        assert values["executor"].cancel_calls == 1
        assert len(values["executor"].start_calls) == 1
        assert values["executor"].snapshot.state == QueryExecutionState.PARTIAL_CANCELLED
    finally:
        _close_window(values)


def test_refresh_retry_view_and_history_buttons_each_call_once(tmp_path):
    refresh_values = _create_window(tmp_path / "refresh")
    try:
        root = refresh_values["root"]
        executor = refresh_values["executor"]
        _click(root, _find(root, "searchButton"))
        executor.accept_first_page()
        assert refresh_values["bridge"].canRefresh is True
        _click(root, _find(root, "refreshButton"))
        assert len(executor.start_calls) == 2
    finally:
        _close_window(refresh_values)

    retry_values = _create_window(tmp_path / "retry")
    try:
        root = retry_values["root"]
        executor = retry_values["executor"]
        _click(root, _find(root, "searchButton"))
        executor.accept_first_page()
        executor.fail_current_page()
        assert retry_values["bridge"].canRetry is True
        _click(root, _find(root, "retryButton"))
        assert executor.retry_calls == 1
    finally:
        _close_window(retry_values)

    view_values = _create_window(tmp_path / "view")
    try:
        root = view_values["root"]
        executor = view_values["executor"]
        bridge = view_values["bridge"]
        _click(root, _find(root, "searchButton"))
        executor.accept_first_page()
        form = default_query_form_values()
        form["minimumScore"] = "10"
        assert bridge.updateDraftForm(form) is True
        assert bridge.canApplyView is True
        _click(root, _find(root, "applyViewButton"))
        assert view_values["core"].view_generation == 1
        assert len(executor.start_calls) == 1
    finally:
        _close_window(view_values)

    history_values = _create_window(tmp_path / "history")
    try:
        root = history_values["root"]
        executor = history_values["executor"]
        session = history_values["session"]
        _click(root, _find(root, "searchButton"))
        executor.accept_first_page()
        assert session.historyModel.count == 1
        session.selectHistory(0)
        assert session.canRestoreSelected is True
        _click(root, _find(root, "restoreQueryButton"))
        assert len(executor.start_calls) == 2
    finally:
        _close_window(history_values)


class _StatusController(QObject):
    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.values = {
            "queryState": "idle",
            "oldGalleryVisible": False,
            "remoteRows": 0,
            "uniqueCandidates": 0,
            "localMatches": 0,
            "visibleCount": 0,
            "failureErrorText": "",
            "statusText": "尚未开始查询",
            "failureBranch": -1,
            "failurePage": 0,
            "failureHttpStatus": 0,
            "canCancel": False,
        }

    def update(self, **values) -> None:
        self.values.update(values)
        self.changed.emit()

    queryState = Property(str, lambda self: self.values["queryState"], notify=changed)
    oldGalleryVisible = Property(
        bool, lambda self: self.values["oldGalleryVisible"], notify=changed
    )
    remoteRows = Property(int, lambda self: self.values["remoteRows"], notify=changed)
    uniqueCandidates = Property(
        int, lambda self: self.values["uniqueCandidates"], notify=changed
    )
    localMatches = Property(
        int, lambda self: self.values["localMatches"], notify=changed
    )
    visibleCount = Property(
        int, lambda self: self.values["visibleCount"], notify=changed
    )
    failureErrorText = Property(
        str, lambda self: self.values["failureErrorText"], notify=changed
    )
    statusText = Property(str, lambda self: self.values["statusText"], notify=changed)
    failureBranch = Property(
        int, lambda self: self.values["failureBranch"], notify=changed
    )
    failurePage = Property(int, lambda self: self.values["failurePage"], notify=changed)
    failureHttpStatus = Property(
        int, lambda self: self.values["failureHttpStatus"], notify=changed
    )
    canCancel = Property(bool, lambda self: self.values["canCancel"], notify=changed)


class _StatusSession(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_calls = 0

    @Slot(result=bool)
    def cancelM2Query(self) -> bool:
        self.cancel_calls += 1
        return True


def test_status_bar_distinguishes_five_empty_result_states():
    engine = QQmlApplicationEngine()
    controller = _StatusController()
    session = _StatusSession()
    component = QQmlComponent(
        engine,
        QUrl.fromLocalFile(str(QML_DIRECTORY / "QueryStatusBar.qml")),
    )
    item = component.createWithInitialProperties(
        {"controller": controller, "sessionController": session}
    )
    assert item is not None, [error.toString() for error in component.errors()]
    cases = (
        ({}, "尚未取得候选"),
        ({"queryState": "complete"}, "扫描完成，没有取得候选"),
        (
            {"queryState": "complete", "remoteRows": 4},
            "扫描完成，没有取得候选",
        ),
        (
            {"queryState": "complete", "remoteRows": 4, "uniqueCandidates": 3},
            "已有候选，但没有作品命中表达式",
        ),
        (
            {
                "queryState": "complete",
                "remoteRows": 4,
                "uniqueCandidates": 3,
                "localMatches": 2,
            },
            "表达式已有命中，但全部被本地筛选移除",
        ),
        (
            {"queryState": "failed", "oldGalleryVisible": True},
            "当前查询失败、取消或受限，画廊仍显示上一查询结果",
        ),
    )
    try:
        for values, expected in cases:
            controller.values.update(
                {
                    "queryState": "idle",
                    "oldGalleryVisible": False,
                    "remoteRows": 0,
                    "uniqueCandidates": 0,
                    "localMatches": 0,
                    "visibleCount": 0,
                }
            )
            controller.update(**values)
            APPLICATION.processEvents()
            assert item.property("emptyStateText") == expected
        controller.update(queryState="running", oldGalleryVisible=True)
        APPLICATION.processEvents()
        assert item.property("emptyStateText") == "尚未取得候选"
    finally:
        item.deleteLater()
        engine.deleteLater()
        APPLICATION.processEvents()


def test_detail_tag_menu_has_new_query_and_draft_only_actions(tmp_path):
    values = _create_window(tmp_path)
    try:
        core = values["core"]
        core.posts_model.set_posts((_post(),))
        root = values["root"]
        assert QMetaObject.invokeMethod(
            root,
            "openPostDetails",
            Qt.ConnectionType.DirectConnection,
            Q_ARG("QVariant", 0),
        )
        assert _wait_until(lambda: values["details"].isOpen)
        assert _wait_until(
            lambda: _find_visual_item(root, "detailTag_blue_sky") is not None
        )
        tag_button = _find_visual_item(root, "detailTag_blue_sky")
        _click(root, tag_button)
        assert _wait_until(
            lambda: _find_visual_item(
                root, "detailTagAddToQuery_blue_sky"
            ) is not None
        )
        add_action = _find_visual_item(root, "detailTagAddToQuery_blue_sky")
        assert add_action.property("visible") is True
        assert QMetaObject.invokeMethod(
            add_action,
            "triggered",
            Qt.ConnectionType.DirectConnection,
        )
        APPLICATION.processEvents()
        assert values["details"].isOpen is True
        assert values["executor"].start_calls == []
        assert "blue_sky" in values["bridge"].canonicalExpression

        _click(root, tag_button)
        assert _wait_until(
            lambda: _find_visual_item(
                root, "detailTagNewQuery_blue_sky"
            ) is not None
        )
        new_action = _find_visual_item(root, "detailTagNewQuery_blue_sky")
        assert QMetaObject.invokeMethod(
            new_action,
            "triggered",
            Qt.ConnectionType.DirectConnection,
        )
        APPLICATION.processEvents()
        assert values["details"].isOpen is False
        assert len(values["executor"].start_calls) == 1
    finally:
        _close_window(values)


def test_detail_drawer_closes_after_tag_starts_new_query(tmp_path):
    values = _create_window(tmp_path)
    try:
        root = values["root"]
        root.setWidth(820)
        root.setHeight(600)
        values["core"].posts_model.set_posts((_post(),))
        assert QMetaObject.invokeMethod(
            root, "openPostDetails", Qt.ConnectionType.DirectConnection,
            Q_ARG("QVariant", 0),
        )
        APPLICATION.processEvents()
        drawer = _find(root, "detailInformationDrawer")
        assert QMetaObject.invokeMethod(drawer, "open", Qt.ConnectionType.DirectConnection)
        assert _wait_until(lambda: drawer.property("opened"))
        assert values["details"].activateTag("blue_sky")
        assert _wait_until(lambda: not drawer.property("opened"))
        assert values["details"].isOpen is False
        assert len(values["executor"].start_calls) == 1
    finally:
        _close_window(values)


def test_hidden_detail_layout_keeps_active_history_gallery_position(tmp_path):
    values = _create_window(tmp_path)
    try:
        root = values["root"]
        bridge = values["bridge"]
        executor = values["executor"]
        session = values["session"]
        assert bridge.executeStandard()
        executor.accept_first_page()
        executor.snapshot = QueryExecutionSnapshot(
            executor.current_execution_id,
            QueryExecutionState.COMPLETE,
            QueryExecutionCounts(30, 30, 30),
            tuple(_post(index + 1) for index in range(30)),
            (),
        )
        executor.snapshotChanged.emit(executor.snapshot)
        assert _wait_until(lambda: session.activeHistoryId > 0)
        APPLICATION.processEvents()
        QTest.qWait(30)
        grid = _find(root, "galleryGrid")
        grid.setProperty("contentY", 340.0)
        APPLICATION.processEvents()
        assert QMetaObject.invokeMethod(root, "reportGalleryPosition", Qt.ConnectionType.DirectConnection)
        before = session._gallery_position
        assert before is not None and before.query_id == session.activeHistoryId
        assert before.scroll_position == 340.0
        assert QMetaObject.invokeMethod(
            root, "openPostDetails", Qt.ConnectionType.DirectConnection,
            Q_ARG("QVariant", 4),
        )
        APPLICATION.processEvents()
        assert session._gallery_position == before
        grid.setProperty("contentY", 900.0)
        assert QMetaObject.invokeMethod(root, "reportGalleryPosition", Qt.ConnectionType.DirectConnection)
        APPLICATION.processEvents()
        assert session._gallery_position == before
        session.save_final_state()
        saved = HistoryRepository(Database(tmp_path / "qml.sqlite3")).get_query(session.activeHistoryId)
        assert saved.scroll_position == before.scroll_position
        assert saved.anchor_post_id == before.anchor_post_id
        assert saved.anchor_index == before.anchor_index
    finally:
        _close_window(values)


def test_m2_qml_sources_remove_page_navigation_and_history_page_wording():
    main_source = (QML_DIRECTORY / "M2Main.qml").read_text(encoding="utf-8")
    history_source = (QML_DIRECTORY / "HistoryPanel.qml").read_text(encoding="utf-8")

    for forbidden in (
        "previousPageButton",
        "nextPageButton",
        "pageRequestCompleted",
        "updateGalleryPosition(",
        "searchController.search(",
        "searchController.busy",
    ):
        assert forbidden not in main_source
    assert "第 %1 页" not in main_source
    assert "第 %1 页" not in history_source
