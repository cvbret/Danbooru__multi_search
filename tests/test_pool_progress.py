from dataclasses import replace
from datetime import datetime, timezone
import sys
import subprocess

import pytest

from danbooru_multi_search.core.pool_progress import (
    PoolProgressError, PoolReadingProgress, normalize_api_source, resolve_pool_progress,
)
from danbooru_multi_search.core.pool_reader import ReaderMode
from danbooru_multi_search.core.pools import DanbooruPool, PoolCategory


def progress(**changes):
    values = dict(source="https://example.invalid/api", pool_id=7, post_id=9,
                  page_number=3, updated_at=datetime(2026, 11, 17, tzinfo=timezone.utc),
                  offset=123, viewport_width=800, viewport_height=600)
    values.update(changes)
    return PoolReadingProgress(**values)


def resolve(value, ids, **layout):
    values = dict(viewport_width=800, viewport_height=600, zoom=1,
                  mode=ReaderMode.CONTINUOUS, scale_mode="fit-width")
    values.update(layout)
    return resolve_pool_progress(value, DanbooruPool(7, "series", PoolCategory.SERIES, tuple(ids)), **values)


def test_source_normalization_separates_endpoints_without_storing_credentials():
    assert normalize_api_source("HTTPS://user:secret@Example.Invalid:443/api/?key=private#token") == "https://example.invalid/api"
    assert normalize_api_source("https://example.invalid:8443/api") != normalize_api_source("https://example.invalid/api")
    assert normalize_api_source("https://example.invalid/api/other") != normalize_api_source("https://example.invalid/api")


@pytest.mark.parametrize("ids,page", [
    ((9, 1, 9, 2), 3), ((9, 1, 2, 9), 4), ((9, 1, 2, 3, 9), 1),
    ((1, 2), 2), ((1, 2, 3, 4), 3),
])
def test_restore_uses_original_or_nearest_occurrence_then_clamps(ids, page):
    result = resolve(progress(), ids)
    assert result.page_number == page
    assert result.offset == (123 if ids[page - 1] == 9 else 0)
    assert resolve(progress(), ()) is None


def test_offset_requires_same_effective_layout_including_fit_window_height():
    value = progress(scale_mode="fit-window")
    assert resolve(value, (1, 2, 9), scale_mode="fit-window").offset == 123
    assert resolve(value, (1, 2, 9), scale_mode="fit-window", viewport_height=400).offset == 0
    assert resolve(progress(), (1, 2, 9), viewport_width=700).offset == 0
    assert resolve(progress(), (1, 2, 9), zoom=1.5).offset == 0
    assert resolve(progress(), (1, 2, 9), mode=ReaderMode.SINGLE).offset == 0


@pytest.mark.parametrize("changes", [
    {"pool_id": True}, {"page_number": 0}, {"offset": -1}, {"offset": float("nan")},
    {"viewport_height": float("inf")}, {"revision": 1.5}, {"mode": "continuous"},
    {"updated_at": datetime(2026, 1, 1)},
])
def test_progress_rejects_invalid_persistent_values(changes):
    with pytest.raises(PoolProgressError):
        replace(progress(), **changes)


def test_pure_restore_types_import_without_qt_or_storage():
    code = "import sys; from danbooru_multi_search.core.pool_progress import PoolReadingProgress; assert not any(n.startswith(('PySide6', 'sqlite3', 'danbooru_multi_search.storage')) for n in sys.modules)"
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True)
