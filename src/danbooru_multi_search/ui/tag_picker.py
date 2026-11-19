"""QML-facing tag browser, selection and shared favorite actions."""

from __future__ import annotations

from itertools import islice
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Property, Signal, Slot

from danbooru_multi_search.app.tag_suggestions import TagSuggestionService
from danbooru_multi_search.core.favorites import FavoriteError, FavoriteTagCategory
from danbooru_multi_search.danbooru.query_language import QueryParseError, parse_query_expression
from danbooru_multi_search.danbooru.query_planning import QueryPlanError, build_standard_query_plan
from danbooru_multi_search.danbooru.tag_catalog import (
    CATEGORY_CODES, CATEGORY_NAMES, STARTING_TAGS, browse_tags, gallery_tags, selection_expression, selection_groups,
)
from danbooru_multi_search.danbooru.tag_types import TagSuggestion
from danbooru_multi_search.storage.favorites import FavoriteRepository
from danbooru_multi_search.ui.favorites import FavoriteController
from danbooru_multi_search.ui.gallery import DanbooruPostListModel

if TYPE_CHECKING:
    from danbooru_multi_search.ui.query_state import M2QueryBridgeController


_PAGE_SIZE = 24


class TagPickerController(QObject):
    changed = Signal()
    selectionChanged = Signal()
    noticeChanged = Signal()

    def __init__(
        self, repository: FavoriteRepository, favorites: FavoriteController, gallery: DanbooruPostListModel,
        lookup: TagSuggestionService, parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._repository, self._favorites, self._gallery, self._lookup = repository, favorites, gallery, lookup
        self._store = lookup.translation_store
        self._category, self._term, self._notice = "general", "", ""
        self._limit, self._has_more, self._closed = _PAGE_SIZE, False, False
        self._lookup_interrupted = False
        self._selected: dict[str, TagSuggestion] = {}
        self._groups: dict[str, str] = {}
        self._query: M2QueryBridgeController | None = None
        self._draft: tuple[str, bool] | None = None
        self._simple = True
        self._pending: dict[str, str] | None = None
        self._saved: dict[str, TagSuggestion] = {}
        self._online: dict[str, TagSuggestion] = {}
        self._visible: dict[str, TagSuggestion] = {}
        self._gallery_tags = gallery_tags(gallery.posts)
        self._saved_error = ""
        self._reload_favorites()
        favorites.collectionChanged.connect(self._reload_favorites)
        gallery.revisionChanged.connect(self._gallery_changed)
        self._store.snapshotChanged.connect(self._refresh)
        lookup.suggestionsReplaced.connect(self._suggestions)
        lookup.viewChanged.connect(self.noticeChanged)
        lookup.remoteStateChanged.connect(self.noticeChanged)
        lookup.noticeTextChanged.connect(self.noticeChanged)

    def bindQuery(self, query: M2QueryBridgeController) -> None:
        """Follow every change to the shared query draft, including history restores."""
        if self._query is not None:
            self._query.draftChanged.disconnect(self._sync_draft)
        self._query = query
        self._draft = None
        query.draftChanged.connect(self._sync_draft)
        self._sync_draft()

    def _sync_draft(self) -> None:
        if self._closed or self._query is None:
            return
        text = self._query.advancedText
        draft = (text, self._query.visualEditingEnabled)
        if self._pending is not None and text == selection_expression(self._pending):
            groups = self._pending
        elif draft == self._draft:
            return
        else:
            groups = selection_groups(self._query.canonicalExpression) if draft[1] else ({} if not text else None)
        self._draft = draft
        self._show_groups(groups)

    def _show_groups(self, groups: dict[str, str] | None) -> None:
        simple = groups is not None
        proposed = groups or {}
        if self._simple == simple and tuple(self._groups.items()) == tuple(proposed.items()):
            return
        self._simple = simple
        self._selected = {
            name: (self._visible.get(name) or self._selected.get(name) or self._store.snapshot.records_by_name.get(name)
                   or self._saved.get(name) or self._gallery_tags.get(name) or STARTING_TAGS.get(name) or TagSuggestion(name))
            for name in proposed
        }
        self._groups = proposed
        self._publish_selection()

    def _apply_groups(self, groups: dict[str, str]) -> bool:
        if self._query is None:
            self._show_groups(groups)
            return True
        expression = selection_expression(groups)
        if self._simple and self._query.advancedText == expression and (expression or not self._query.canonicalExpression):
            self._show_groups(groups)
            return True
        self._pending = groups
        try:
            if expression:
                self._query.applyAdvancedText(expression)
            else:
                self._query.clearDraftExpression()
        finally:
            self._pending = None
        return self._query.advancedText == expression and self._groups == groups and self._simple

    def _reload_favorites(self) -> None:
        if self._closed:
            return
        try:
            self._saved = {
                name: TagSuggestion(name, category=CATEGORY_CODES[category.value])
                for name, category in self._repository.list_tag_names()
            }
            self._saved_error = ""
        except (FavoriteError, ValueError) as exc:
            self._saved_error = f"标签收藏读取失败：{exc}"
        self._refresh()
        self.noticeChanged.emit()

    def _gallery_changed(self) -> None:
        if not self._closed:
            self._gallery_tags = gallery_tags(self._gallery.posts)
            self._refresh()

    def _refresh(self) -> None:
        if self._closed:
            return
        records = browse_tags(self._category, self._term, self._store.snapshot,
                              self._saved, self._gallery_tags, self._online)
        page = tuple(islice(records, self._limit + 1))
        self._has_more = len(page) > self._limit
        self._visible = {item.name: item for item in page[:self._limit]}
        self.changed.emit()

    def _row(self, item: TagSuggestion) -> dict:
        chinese = self._store.chinese_names(item.name) or item.chinese_names
        return {
            "key": item.name, "name": item.name, "chinese": "、".join(chinese),
            "category": CATEGORY_NAMES.get(item.category, ""), "favorite": item.name in self._saved,
            "selected": item.name in self._groups, "group": self._groups.get(item.name, ""),
        }

    @Property("QVariantList", notify=changed)
    def rows(self) -> list[dict]:
        return [self._row(item) for item in self._visible.values()]

    @Property("QVariantList", notify=changed)
    def selectedRows(self) -> list[dict]:
        return [self._row(item) for item in self._selected.values()]

    @Property(str, notify=selectionChanged)
    def expression(self) -> str:
        return selection_expression(self._groups)

    @Property(bool, notify=selectionChanged)
    def simpleEditable(self) -> bool:
        return self._simple

    @Property(str, notify=changed)
    def category(self) -> str:
        return self._category

    @Property(str, notify=changed)
    def searchText(self) -> str:
        return self._term

    @Property(bool, notify=changed)
    def hasMore(self) -> bool:
        return self._has_more

    @Property(str, notify=noticeChanged)
    def noticeText(self) -> str:
        return self._notice or self._saved_error or self._lookup.notice_text

    @Property(bool, notify=noticeChanged)
    def lookupBusy(self) -> bool:
        return self._lookup.debounce_pending or self._lookup.remote_busy

    def _search(self) -> None:
        self._lookup_interrupted = False
        generation = self._lookup.invalidate_request()
        if self._closed or not self._lookup.is_current_request(generation):
            return
        self._online.clear()
        self._limit, self._notice = _PAGE_SIZE, ""
        term = self._term.strip()
        self._lookup.suggest(
            term if term and term.isascii() and self._category != "favorites" else None,
            generation=generation, category=CATEGORY_CODES.get(self._category),
        )
        self._refresh()
        self.noticeChanged.emit()

    def _suggestions(self, generation: int, records: tuple[TagSuggestion, ...]) -> None:
        if not self._closed and self._lookup.is_current_request(generation):
            self._online = {item.name: item for item in records}
            self._refresh()

    @Slot(str)
    def setCategory(self, value: str) -> None:
        if not self._closed and value != self._category and (value in CATEGORY_CODES or value == "favorites"):
            self._category = value
            self._search()

    @Slot(str)
    def setSearchText(self, text: str) -> None:
        if not self._closed and text != self._term:
            self._term = text
            self._search()

    @Slot()
    def showMore(self) -> None:
        if self._has_more and not self._closed:
            self._limit += _PAGE_SIZE
            self._refresh()

    @Slot(str, str, result=bool)
    def toggleTag(self, name: str, group: str) -> bool:
        item = self._visible.get(name) or self._selected.get(name)
        if self._closed or not self._simple or item is None or group not in {"all", "any", "exclude"}:
            return False
        if self._groups.get(name) == group:
            return self.removeTag(name)
        proposed = self._groups | {name: group}
        try:
            build_standard_query_plan(parse_query_expression(selection_expression(proposed)), post_counts={})
        except (QueryParseError, QueryPlanError) as exc:
            if exc.code != "pure_negative_expression":
                self._notice = str(exc)
                self.noticeChanged.emit()
                return False
        return self._apply_groups(proposed)

    @Slot(str, result=bool)
    def removeTag(self, name: str) -> bool:
        if self._closed or not self._simple or name not in self._groups:
            return False
        return self._apply_groups({key: value for key, value in self._groups.items() if key != name})

    @Slot(result=bool)
    def clearSelection(self) -> bool:
        if self._closed or not self._simple or not self._groups:
            return False
        return self._apply_groups({})

    @Slot(result=bool)
    def resetToPicker(self) -> bool:
        """Explicitly replace a handwritten expression that the picker cannot represent."""
        return not self._closed and not self._simple and self._apply_groups({})

    def _publish_selection(self) -> None:
        self._notice = ("请再选择一个包含的标签。" if self._groups and set(self._groups.values()) == {"exclude"} else "")
        self.changed.emit()
        self.selectionChanged.emit()
        self.noticeChanged.emit()

    @Slot(str, bool, result=bool)
    def setFavorite(self, name: str, desired: bool) -> bool:
        item = self._visible.get(name) or self._selected.get(name)
        if self._closed or not item or item.category not in CATEGORY_NAMES:
            return False
        if self._saved_error:
            self._reload_favorites()
            if self._saved_error:
                return False
        saved = self._favorites.set_tag(
            name, FavoriteTagCategory(CATEGORY_NAMES[item.category]), desired,
            guard=lambda: not self._closed,
        )
        self._notice = "" if saved else self._favorites.noticeText
        self.noticeChanged.emit()
        return saved

    @Slot()
    def stopLookup(self) -> None:
        if self._lookup.debounce_pending or self._lookup.remote_busy:
            self._lookup_interrupted = True
        self._lookup.invalidate_request()
        self.noticeChanged.emit()

    @Slot()
    def resumeLookup(self) -> None:
        if not self._closed and self._lookup_interrupted:
            self._search()

    def shutdown(self) -> None:
        self._closed = True
        self._lookup.shutdown()
