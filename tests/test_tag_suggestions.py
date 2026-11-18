from __future__ import annotations

import os
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest

from danbooru_multi_search.app.tag_suggestions import TagSuggestionService
from danbooru_multi_search.danbooru.tag_index import (
    TagCsvImportResult, TagIndexSnapshot, load_tag_csv,
)
from danbooru_multi_search.danbooru.tag_types import TagSuggestion
from danbooru_multi_search.tasks import BoundedTaskQueue, QtScheduler, SystemClock, TaskQueueConfig


APPLICATION = QGuiApplication.instance() or QGuiApplication([])


def _snapshot(*records: TagSuggestion) -> TagIndexSnapshot:
    return TagIndexSnapshot.build(tuple(records))


def _import_result(name: str) -> TagCsvImportResult:
    return TagCsvImportResult(
        _snapshot(TagSuggestion(name, ("虚构名称",), 1, 0)),
        1, 1, 0, 0, 1, "utf-8",
    )


def _wait_until(predicate) -> bool:
    for _ in range(100):
        APPLICATION.processEvents()
        if predicate():
            return True
        QTest.qWait(10)
    return predicate()


def test_begin_shutdown_rejects_late_import_result_before_formal_shutdown(
    tmp_path,
):
    csv_path = Path(tmp_path) / "late.csv"
    csv_path.write_text("placeholder", encoding="utf-8")
    saved_paths: list[Path] = []
    old_snapshot = _snapshot(TagSuggestion("old", ("旧值",), 1, 0))
    controller = TagSuggestionService(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        csv_path_saver=saved_paths.append,
        index_snapshot=old_snapshot,
    )
    controller._import_generation = 7
    controller._import_path = csv_path.resolve()
    controller._persist_import_path = True

    controller.begin_shutdown()
    controller._import_finished(7, _import_result("late"), None)

    assert controller.translation_store.snapshot == old_snapshot
    assert saved_paths == []
    assert controller._import_generation == 8


def test_shared_queue_shutdown_keeps_local_suggestions_and_csv_completion(tmp_path):
    queue = BoundedTaskQueue(TaskQueueConfig(), clock=SystemClock(), scheduler=QtScheduler(APPLICATION))
    queue.shutdown()
    service = TagSuggestionService(
        "https://example.invalid", task_queue=queue,
        index_snapshot=_snapshot(TagSuggestion("cat", ("猫",), 1, 0)),
    )
    suggestions = []
    outcomes = []
    refreshes = []
    service.suggestionsReplaced.connect(lambda generation, items: suggestions.append((generation, items)))
    service.importCompleted.connect(outcomes.append)
    service.refreshRequested.connect(lambda imported, requested: refreshes.append((imported, requested)))
    try:
        request_generation = service.invalidate_request()
        service.suggest("cat", generation=request_generation)
        assert suggestions[-1][1][0].name == "cat"
        assert not service.debounce_pending and not service.remote_busy
        assert not service.closed and service.is_current_request(request_generation)
        path = tmp_path / "标签.csv"
        path.write_text("name,cn_name,post_count,category\ncat_ears,猫耳,2,0\n", encoding="utf-8")
        import_generation = service.begin_import()
        service.start_import(import_generation, path, persist_path=False)
        assert _wait_until(lambda: len(outcomes) == 1)
        assert outcomes[0].succeeded and outcomes[0].path_saved is None
        assert refreshes == [(import_generation, request_generation)]
        assert service.translation_store.snapshot.records[0].name == "cat_ears"
        generation = service.invalidate_request()
        service.suggest("cat", generation=generation)
        assert suggestions[-1][1][0].name == "cat_ears"
    finally:
        service.shutdown()


def test_csv_worker_completion_returns_to_service_thread_before_publication(tmp_path):
    path = tmp_path / "标签.csv"
    path.write_text("name,cn_name,post_count,category\ncat,猫,1,0\n", encoding="utf-8")
    gui_thread = APPLICATION.thread()
    loader_on_gui = []
    events = []

    def load(path, *, cancel_event):
        loader_on_gui.append(QThread.currentThread() is gui_thread)
        return load_tag_csv(path, cancel_event=cancel_event)

    def save(path):
        events.append(("save", QThread.currentThread() is gui_thread))
        assert path == source.resolve()

    source = path
    service = TagSuggestionService("https://example.invalid", csv_loader=load, csv_path_saver=save)
    service.translation_store.snapshotChanged.connect(
        lambda: events.append(("snapshot", QThread.currentThread() is gui_thread))
    )
    service.importStateChanged.connect(
        lambda: events.append(("state", QThread.currentThread() is gui_thread)) if not service.import_busy else None
    )
    service.importCompleted.connect(
        lambda outcome: events.append(("complete", QThread.currentThread() is gui_thread))
    )
    try:
        generation = service.begin_import()
        service.start_import(generation, path, persist_path=True)
        assert _wait_until(lambda: any(name == "complete" for name, _ in events))
        assert loader_on_gui == [False]
        assert events == [("snapshot", True), ("save", True), ("state", True), ("complete", True)]
    finally:
        service.shutdown()


def test_shutdown_waits_for_running_csv_and_rejects_queued_completion(tmp_path):
    entered = threading.Event()
    cancelled = threading.Event()

    def load(_path, *, cancel_event):
        entered.set()
        assert cancel_event.wait(2)
        cancelled.set()
        return _import_result("late")

    service = TagSuggestionService("https://example.invalid", csv_loader=load)
    outcomes = []
    service.importCompleted.connect(outcomes.append)
    generation = service.begin_import()
    service.start_import(generation, tmp_path / "source.csv", persist_path=False)
    assert _wait_until(entered.is_set)
    service.shutdown()
    assert cancelled.is_set() and service.closed and not service.import_busy
    APPLICATION.processEvents()
    assert outcomes == [] and service.local_tag_count == 0
    service.shutdown()


def test_public_tag_values_rules_and_lazy_ui_exports_keep_owner_identity():
    from danbooru_multi_search import danbooru
    from danbooru_multi_search.app import tag_suggestions
    from danbooru_multi_search.danbooru import autocomplete, tag_syntax, tag_types, tags
    from danbooru_multi_search.ui import autocomplete as ui_autocomplete

    for name in ("TagSuggestion", "DEFAULT_TAG_SUGGESTION_LIMIT", "MAX_TAG_RESPONSE_BYTES"):
        assert getattr(tags, name) is getattr(tag_types, name)
        assert getattr(danbooru, name) is getattr(tag_types, name)
    assert tags.normalize_tag_fragment is tag_syntax.normalize_tag_fragment
    assert danbooru.normalize_tag_fragment is tag_syntax.normalize_tag_fragment
    assert tags.normalize_tag_name is tag_syntax.normalize_tag_name
    assert autocomplete.TagImportOutcome is tag_suggestions.TagImportOutcome
    assert danbooru.TagImportOutcome is tag_suggestions.TagImportOutcome
    for name in ("DanbooruTagSuggestionController", "TagSuggestionListModel", "TagSuggestionRole"):
        assert getattr(autocomplete, name) is getattr(ui_autocomplete, name)
        assert getattr(danbooru, name) is getattr(ui_autocomplete, name)
