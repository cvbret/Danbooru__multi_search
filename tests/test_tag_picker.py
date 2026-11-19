from urllib.parse import parse_qs, urlsplit

import pytest

from danbooru_multi_search.app.tag_suggestions import TagSuggestionService
from danbooru_multi_search.core.favorites import FavoriteCandidate, FavoriteTagCategory
from danbooru_multi_search.danbooru.query_language import parse_query_expression
from danbooru_multi_search.danbooru.tag_index import TagIndexSnapshot
from danbooru_multi_search.danbooru.tag_types import TagSuggestion
from danbooru_multi_search.tasks import TaskQueueConfig
from danbooru_multi_search.ui.gallery import DanbooruPostListModel
from danbooru_multi_search.ui.query_state import M2QueryBridgeController
from danbooru_multi_search.ui.tag_picker import TagPickerController
from tests.test_favorite_controller import build, post
from tests.test_query_controller import _harness
from tests.test_tag_autocomplete import FakeNetworkManager, FakeReply, _wait_until


@pytest.fixture
def browser(tmp_path):
    repository, favorites = build(tmp_path)
    network = FakeNetworkManager([])
    service = TagSuggestionService(
        "https://example.invalid", network_manager=network,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0), connect_application_quit=False,
    )
    gallery = DanbooruPostListModel()
    picker = TagPickerController(repository, favorites, gallery, service)
    try:
        yield picker, service, gallery, favorites, repository, network
    finally:
        picker.shutdown()
        favorites.shutdown()


def test_fresh_install_browses_four_categories_and_combines_checked_tags_without_requests(browser):
    picker, service, _, _, _, network = browser
    assert not picker.selectedRows and not picker.expression
    for category, tag in (("general", "sky"), ("artist", "kantoku"), ("character", "hatsune_miku"), ("copyright", "touhou")):
        picker.setCategory(category)
        assert tag in [row["name"] for row in picker.rows]
        assert all(row["category"] == category for row in picker.rows)
        group = "all" if category == "general" else "exclude" if category == "artist" else "any"
        assert picker.toggleTag(tag, group)
    assert picker.expression == "sky AND (hatsune_miku OR touhou) AND NOT kantoku"
    assert parse_query_expression(picker.expression).normalized_text == picker.expression
    assert picker.toggleTag("kantoku", "all")
    assert picker.expression == "sky AND kantoku AND (hatsune_miku OR touhou)"
    assert picker.removeTag("touhou")
    assert picker.clearSelection() and not picker.expression
    assert picker.toggleTag("touhou", "exclude") and picker.noticeText
    assert not service.debounce_pending and not network.requests


def test_gallery_and_csv_tags_can_be_browsed_paged_and_found_by_chinese_alias(browser):
    picker, service, gallery, _, _, network = browser
    gallery.set_posts((post(),))
    picker.setCategory("artist")
    assert "sample_artist" in [row["name"] for row in picker.rows]
    records = tuple(TagSuggestion(f"csv_{i:03d}", (f"画师{i}",), category=1) for i in range(100))
    service.translation_store.replace_snapshot(TagIndexSnapshot.build(records))
    assert picker.hasMore
    first = len(picker.rows)
    picker.showMore()
    assert len(picker.rows) > first
    picker.setSearchText("画师99")
    assert [(row["name"], row["chinese"]) for row in picker.rows] == [("csv_099", "画师99")]
    assert picker.toggleTag("csv_099", "all")
    picker.setCategory("general")
    assert not picker.rows and picker.selectedRows[0]["name"] == "csv_099"
    assert not service.debounce_pending and not network.requests


def test_picker_and_existing_collection_use_same_persisted_stars(browser):
    picker, _, _, favorites, repository, _ = browser
    assert picker.setFavorite("sky", True)
    identity = FavoriteCandidate.for_tag("sky", FavoriteTagCategory.GENERAL).identity
    assert repository.get_by_identity(identity).tag_category is FavoriteTagCategory.GENERAL
    assert favorites.state(identity)["saved"]
    picker.setCategory("favorites")
    assert [(row["name"], row["favorite"]) for row in picker.rows] == [("sky", True)]
    assert favorites.set_tag("a_new_artist", FavoriteTagCategory.ARTIST, True, guard=lambda: True)
    assert picker.rows[0]["name"] == "a_new_artist" and picker.rows[0]["category"] == "artist"
    assert picker.toggleTag("a_new_artist", "any")
    assert picker.setFavorite("a_new_artist", False)
    assert picker.selectedRows[0]["favorite"] is False
    assert [row["name"] for row in picker.rows] == ["sky"]


def test_english_lookup_for_unlisted_artist_uses_category_and_cancels_stale_replies(browser):
    picker, service, _, _, _, manager = browser
    stale = FakeReply(b'[{"name":"stale_artist","category":1}]', finish_on_abort=False)
    reply = FakeReply(b'[{"name":"fresh_artist","category":1,"post_count":6}]')
    manager.replies.extend([stale, reply])
    picker.setCategory("artist")
    picker.setSearchText("sta")
    assert _wait_until(lambda: len(manager.requests) == 1)
    picker.setSearchText("fresh")
    assert stale.abort_count == 1
    stale.finish()
    assert _wait_until(lambda: len(manager.requests) == 2)
    parts = urlsplit(manager.requests[-1].url().toString())
    query = parse_qs(parts.query)
    assert parts.path == "/tags.json"
    assert query["search[name_matches]"] == ["fresh*"] and query["search[category]"] == ["1"]
    reply.finish()
    assert _wait_until(lambda: [row["name"] for row in picker.rows] == ["fresh_artist"])
    assert picker.toggleTag("fresh_artist", "all") and picker.expression == "fresh_artist"
    picker.setSearchText("later")
    picker.stopLookup()
    assert not service.debounce_pending
    picker.resumeLookup()
    assert service.debounce_pending


def test_picker_follows_existing_draft_history_and_preserves_handwritten_expressions(browser):
    picker = browser[0]
    harness = _harness()
    bridge = M2QueryBridgeController(harness.controller)
    try:
        assert bridge.applyAdvancedText("sky AND (flower OR cat) AND NOT rain")
        history = bridge.draft_definition
        picker.bindQuery(bridge)
        assert [(r["name"], r["group"]) for r in picker.selectedRows] == [
            ("sky", "all"), ("flower", "any"), ("cat", "any"), ("rain", "exclude"),
        ]
        edits = []
        bridge.draftEdited.connect(lambda: edits.append(True))
        assert picker.removeTag("sky") and bridge.advancedText == "(flower OR cat) AND NOT rain"
        assert len(edits) == 1
        assert bridge.loadDraftDefinition(history)
        assert picker.expression == bridge.advancedText and picker.selectedRows[0]["name"] == "sky"
        assert bridge.loaded_history_definition == history
        assert bridge.applyAdvancedText("(sky AND dog) OR flower")
        handwritten = bridge.advancedText
        assert not picker.simpleEditable and not picker.selectedRows
        assert not picker.toggleTag("sky", "all") and not picker.clearSelection()
        assert bridge.advancedText == handwritten
        assert not bridge.applyAdvancedText("sky AND (")
        assert not picker.simpleEditable and not picker.removeTag("sky") and bridge.advancedText == "sky AND ("
        assert picker.resetToPicker() and picker.simpleEditable
        assert bridge.visualEditingEnabled and bridge.advancedText == bridge.canonicalExpression == ""
        assert not bridge.discardInvalidText()
        assert picker.toggleTag("sky", "exclude") and picker.simpleEditable
        assert picker.expression == bridge.advancedText == "NOT sky"
        assert picker.toggleTag("flower", "all") and bridge.advancedText == "flower AND NOT sky"
        assert bridge.visualEditingEnabled
        assert picker.clearSelection() and bridge.advancedText == bridge.canonicalExpression == ""
    finally:
        bridge.beginClosing()
        bridge.detach()
        harness.controller.shutdown()
        harness.queue.shutdown()


@pytest.mark.parametrize("mode", ["latest", "rank"])
def test_loading_tagless_history_clears_old_text_and_checked_tags(browser, mode):
    from danbooru_multi_search.danbooru.query_definition import QueryMode
    from tests.test_m2_session_state import _definition

    picker = browser[0]
    harness = _harness()
    bridge = M2QueryBridgeController(harness.controller)
    try:
        picker.bindQuery(bridge)
        assert bridge.applyAdvancedText("sky")
        assert [row["name"] for row in picker.selectedRows] == ["sky"]
        definition = _definition(None, mode=QueryMode(mode))
        assert bridge.loadDraftDefinition(definition)
        assert bridge.loaded_history_definition == definition
        assert bridge.advancedText == bridge.canonicalExpression == ""
        assert not picker.selectedRows and not picker.expression
        assert not harness.factory.attempts
    finally:
        bridge.beginClosing()
        bridge.detach()
        harness.controller.shutdown()
        harness.queue.shutdown()
