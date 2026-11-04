from __future__ import annotations

import pytest

from danbooru_multi_search.danbooru.tag_index import TagIndexSnapshot
from danbooru_multi_search.danbooru.tag_translation import TagTranslationStore
from danbooru_multi_search.danbooru.tags import TagSuggestion


def _snapshot(*records: TagSuggestion) -> TagIndexSnapshot:
    return TagIndexSnapshot.build(tuple(records))


def test_translation_store_exposes_one_immutable_snapshot_and_all_aliases():
    snapshot = _snapshot(
        TagSuggestion("fictional_tag", ("虚构标签", "示例标签"), 3, 0)
    )
    store = TagTranslationStore(snapshot)

    assert store.snapshot is snapshot
    assert store.chinese_names("FICTIONAL_TAG") == ("虚构标签", "示例标签")
    assert store.chinese_names("missing_tag") == ()
    assert store.chinese_names("中文") == ()


def test_translation_store_emits_once_after_each_atomic_replacement():
    initial = _snapshot(TagSuggestion("first", ("第一",), 1, 0))
    replacement = _snapshot(TagSuggestion("second", ("第二",), 2, 0))
    store = TagTranslationStore(initial)
    observed = []
    store.snapshotChanged.connect(lambda: observed.append(store.snapshot))

    store.replace_snapshot(replacement)

    assert observed == [replacement]
    assert store.snapshot is replacement


def test_translation_store_rejects_mutable_or_unknown_replacements():
    store = TagTranslationStore()
    observed = []
    store.snapshotChanged.connect(lambda: observed.append(True))

    with pytest.raises(TypeError):
        store.replace_snapshot({})

    assert observed == []
