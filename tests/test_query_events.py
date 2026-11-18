from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import pytest

from danbooru_multi_search.danbooru import query_events
from danbooru_multi_search.danbooru.query_definition import GalleryQueryDefinition, QueryMode
from danbooru_multi_search.danbooru.query_types import (
    GalleryResultSummary, QueryExecutionLimits, QueryExecutionState,
)
from danbooru_multi_search.danbooru.query_planning import QueryRating
from danbooru_multi_search.danbooru.result_processing import ResultFilterSpec, ResultSortSpec


@pytest.mark.parametrize("first_import", ["module", "package"])
def test_events_cold_import_without_qt_controller_or_ui(first_import):
    code = """
import importlib.abc
import sys

class Reject(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("PySide6", "danbooru_multi_search.ui", "danbooru_multi_search.danbooru.query_controller")):
            raise AssertionError(fullname)

sys.meta_path.insert(0, Reject())
if sys.argv[1] == "module":
    from danbooru_multi_search.danbooru import query_events as events
else:
    from danbooru_multi_search.danbooru import GalleryQueryAccepted
    from danbooru_multi_search.danbooru import query_events as events
    assert GalleryQueryAccepted is events.GalleryQueryAccepted
from danbooru_multi_search import danbooru
for name in ("GalleryQueryOrigin", "GalleryQueryAccepted", "GalleryViewDefinitionChanged", "GalleryResultEvent"):
    assert danbooru._EXPORTS[name] == "danbooru_multi_search.danbooru.query_events"
    assert getattr(danbooru, name) is getattr(events, name)
assert not any(name.startswith(("PySide6", "danbooru_multi_search.ui", "danbooru_multi_search.danbooru.query_controller")) for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, first_import],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_controller_and_package_exports_are_the_same_event_objects():
    from danbooru_multi_search import danbooru
    from danbooru_multi_search.danbooru import query_controller

    for name in (
        "GalleryQueryOrigin", "GalleryQueryAccepted",
        "GalleryViewDefinitionChanged", "GalleryResultEvent",
    ):
        assert getattr(query_controller, name) is getattr(query_events, name)
        assert getattr(danbooru, name) is getattr(query_events, name)
        assert name in danbooru.__all__ and name in dir(danbooru)


def test_all_three_event_values_remain_frozen():
    definition = GalleryQueryDefinition(
        QueryMode.STANDARD, "cat", QueryRating.ALL,
        ResultFilterSpec(), ResultSortSpec(), QueryExecutionLimits(),
    )
    summary = GalleryResultSummary(QueryExecutionState.COMPLETE, 0, 0, 0, 0, None, None)
    values = (
        query_events.GalleryQueryAccepted(
            1, 1, definition, "cat", query_events.GalleryQueryOrigin.STANDARD,
            ("cat",), datetime.now(timezone.utc), True,
        ),
        query_events.GalleryViewDefinitionChanged(1, 1, 0, definition, True),
        query_events.GalleryResultEvent(1, 1, 0, definition, summary, 1),
    )
    for value in values:
        with pytest.raises(FrozenInstanceError):
            value.operation_id = 2
