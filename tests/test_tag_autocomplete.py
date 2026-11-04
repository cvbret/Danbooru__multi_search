from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QNetworkReply
from PySide6.QtTest import QTest

from danbooru_multi_search.danbooru import (
    DanbooruTagSuggestionController,
    TagCsvImportCancelled,
    TagCsvImportError,
    TagCsvImportResult,
    TagIndexSnapshot,
    TagSuggestion,
    locate_active_tag_fragment,
    merge_tag_suggestions,
    replace_active_tag_fragment,
)
from danbooru_multi_search.danbooru.tag_translation import TagTranslationStore
from danbooru_multi_search.tasks import TaskQueueConfig


APPLICATION = QGuiApplication.instance() or QGuiApplication([])


def _snapshot(*records: TagSuggestion) -> TagIndexSnapshot:
    return TagIndexSnapshot.build(tuple(records))


@pytest.mark.parametrize(
    ("text", "cursor", "expected"),
    [
        ("cat dog", 0, (0, 3, "cat")),
        ("cat dog", 2, (0, 3, "cat")),
        ("cat dog", 3, (0, 3, "cat")),
        ("cat dog", 4, (4, 7, "dog")),
        ("cat dog", 7, (4, 7, "dog")),
        ("cat  dog", 4, (4, 4, "")),
        ("", 0, (0, 0, "")),
    ],
)
def test_active_fragment_boundaries_follow_cursor_rules(
    text,
    cursor,
    expected,
):
    context = locate_active_tag_fragment(text, cursor)

    assert (context.start, context.end, context.fragment) == expected


@pytest.mark.parametrize(
    "tag_name",
    [
        "fate/grand_order",
        "girls'_frontline",
        "honkai:_star_rail",
        "love_live!",
    ],
)
def test_fragment_replacement_preserves_real_tag_punctuation(tag_name):
    replacement = replace_active_tag_fragment("cat dog", 1, tag_name)

    assert replacement.text == f"{tag_name} dog"
    assert replacement.cursor == len(tag_name)
    assert replacement.changed is True
    assert replacement.should_search is True


def test_replacement_preserves_spacing_and_waits_for_all_chinese_to_be_mapped():
    first = replace_active_tag_fragment("猫   狗", 1, "cat")
    second = replace_active_tag_fragment(first.text, len(first.text), "dog")

    assert first.text == "cat   狗"
    assert first.should_search is False
    assert second.text == "cat   dog"
    assert second.should_search is True
    assert replace_active_tag_fragment("cat dog bird", 5, "wolf").changed is False
    assert replace_active_tag_fragment("cat", 1, "dog wolf").changed is False


def test_merge_is_order_independent_and_preserves_none_zero_and_remote_priority():
    local = [
        TagSuggestion("cat", ("猫",), None, 4),
        TagSuggestion("cat_ears", ("猫耳",), 0, 0),
        TagSuggestion("cat_tail", ("猫尾",), None, 0),
    ]
    remote = [
        TagSuggestion("cat_tail", (), 7, 4),
        TagSuggestion("cat", (), 0, 0),
        TagSuggestion("cat_ears", (), None, None),
    ]

    forward = merge_tag_suggestions(local, remote, "cat")
    reverse = merge_tag_suggestions(list(reversed(local)), list(reversed(remote)), "cat")

    assert forward == reverse
    assert [item.name for item in forward] == ["cat", "cat_tail", "cat_ears"]
    assert forward[0].post_count == 0
    assert forward[0].category == 0
    assert forward[1].post_count == 7
    assert forward[2].post_count == 0


def test_chinese_merge_prefers_first_matching_alias_at_the_best_rank():
    suggestions = merge_tag_suggestions(
        [
            TagSuggestion("hero", ("虚构英雄", "英雄"), 4, 4),
            TagSuggestion("heroine", ("女英雄",), 10, 4),
        ],
        [],
        "英雄",
    )

    assert [item.name for item in suggestions] == ["hero", "heroine"]
    assert suggestions[0].chinese_name == "英雄"
    assert suggestions[0].chinese_name_text == "英雄、虚构英雄"
    assert suggestions[1].chinese_name == "女英雄"


class FakeReply(QObject):
    metaDataChanged = Signal()
    readyRead = Signal()
    finished = Signal()

    def __init__(
        self,
        payload: bytes,
        *,
        status: int = 200,
        network_error=QNetworkReply.NetworkError.NoError,
        finish_on_abort: bool = True,
        retry_after: bytes = b"",
    ) -> None:
        super().__init__()
        self.payload = payload
        self.status = status
        self.network_error = network_error
        self.finish_on_abort = finish_on_abort
        self.retry_after = retry_after
        self.abort_count = 0
        self.delete_later_count = 0

    def abort(self) -> None:
        self.abort_count += 1
        self.network_error = QNetworkReply.NetworkError.OperationCanceledError
        if self.finish_on_abort:
            self.finished.emit()

    def readAll(self) -> bytes:
        payload = self.payload
        self.payload = b""
        return payload

    def error(self):
        return self.network_error

    def errorString(self) -> str:
        return "simulated network error" if self.network_error else ""

    def attribute(self, attribute):
        return self.status

    def rawHeader(self, header_name: str) -> bytes:
        if header_name == "Retry-After":
            return self.retry_after
        if header_name == "Content-Length":
            return str(len(self.payload)).encode("ascii")
        return b""

    def finish(self) -> None:
        self.finished.emit()

    def deleteLater(self) -> None:
        self.delete_later_count += 1


class FakeNetworkManager:
    def __init__(self, replies: list[FakeReply]) -> None:
        self.replies = replies
        self.requests = []

    def get(self, request):
        self.requests.append(request)
        return self.replies.pop(0)


def _tag_payload(*items: dict[str, object]) -> bytes:
    return json.dumps(list(items)).encode("utf-8")


def _wait_until(predicate, timeout_ms: int = 2_000) -> bool:
    elapsed = 0
    while elapsed < timeout_ms:
        APPLICATION.processEvents()
        if predicate():
            return True
        QTest.qWait(10)
        elapsed += 10
    return predicate()


def test_controller_shows_local_immediately_then_merges_fixed_remote_query():
    reply = FakeReply(
        _tag_payload(
            {"name": "cat", "post_count": 9, "category": 0},
            {"name": "cat_tail", "post_count": 5, "category": 0},
        )
    )
    manager = FakeNetworkManager([reply])
    controller = DanbooruTagSuggestionController(
        "https://example.invalid/api",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        index_snapshot=_snapshot(
            TagSuggestion("cat", ("猫",), 1, 4),
            TagSuggestion("cat_ears", ("猫耳",), 3, 0),
        ),
    )

    controller.updateInput("ca", 2)

    assert [item.name for item in controller.suggestionsModel.items] == [
        "cat_ears",
        "cat",
    ]
    assert controller.debouncePending is True
    assert manager.requests == []
    assert _wait_until(lambda: len(manager.requests) == 1, 1_000)
    query = parse_qs(urlsplit(manager.requests[0].url().toString()).query)
    assert query["search[name_matches]"] == ["ca*"]
    assert len(query) == 6

    reply.finish()

    assert [item.name for item in controller.suggestionsModel.items] == [
        "cat",
        "cat_tail",
        "cat_ears",
    ]
    assert controller.suggestionsModel.items[0].chinese_name == "猫"
    assert controller.suggestionsModel.items[0].category == 0
    assert controller.remoteBusy is False


def test_chinese_empty_and_third_fragments_never_request_remote():
    manager = FakeNetworkManager([])
    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        index_snapshot=_snapshot(TagSuggestion("cat", ("猫",), 1, 0)),
    )

    controller.updateInput("猫", 1)
    assert controller.suggestionsModel.count == 1
    QTest.qWait(350)
    controller.updateInput("cat ", 4)
    controller.updateInput("cat dog bird", 12)
    QTest.qWait(350)

    assert manager.requests == []
    assert controller.suggestionsModel.count == 0


def test_new_input_and_close_abort_old_reply_and_ignore_late_completion():
    old_reply = FakeReply(
        _tag_payload({"name": "cat", "post_count": 1, "category": 0}),
        finish_on_abort=False,
    )
    new_reply = FakeReply(
        _tag_payload({"name": "dog", "post_count": 2, "category": 0})
    )
    manager = FakeNetworkManager([old_reply, new_reply])
    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )

    controller.updateInput("cat", 3)
    assert _wait_until(lambda: len(manager.requests) == 1)
    controller.updateInput("dog", 3)
    assert old_reply.abort_count == 1
    assert _wait_until(lambda: len(manager.requests) == 2)
    new_reply.finish()
    assert [item.name for item in controller.suggestionsModel.items] == ["dog"]

    old_reply.finish()
    assert [item.name for item in controller.suggestionsModel.items] == ["dog"]
    controller.closeSuggestions()
    assert controller.suggestionsModel.count == 0
    assert controller.isOpen is False


def test_remote_failure_keeps_local_suggestions_and_sets_nonblocking_notice():
    reply = FakeReply(b"unavailable", status=500)
    manager = FakeNetworkManager([reply])
    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0, max_retries=0),
        index_snapshot=_snapshot(TagSuggestion("cat", ("猫",), 1, 0)),
    )

    controller.updateInput("cat", 3)
    assert _wait_until(lambda: len(manager.requests) == 1)
    reply.finish()

    assert [item.name for item in controller.suggestionsModel.items] == ["cat"]
    assert controller.noticeText
    assert controller.isOpen is True


def test_remote_start_signal_can_close_without_leaving_busy_state_or_request():
    manager = FakeNetworkManager([])
    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        index_snapshot=_snapshot(TagSuggestion("cat", (), 1, 0)),
    )
    controller.updateInput("cat", 3)
    closed = False

    def close_during_remote_start():
        nonlocal closed
        if not closed and not controller.debouncePending:
            closed = True
            controller.closeSuggestions()

    controller.remoteStateChanged.connect(close_during_remote_start)
    QTest.qWait(350)
    APPLICATION.processEvents()

    assert closed is True
    assert manager.requests == []
    assert controller.remoteBusy is False
    assert controller.debouncePending is False
    assert controller.suggestionsModel.count == 0


def test_queue_state_signal_can_close_before_starter_sends_request():
    reply = FakeReply(_tag_payload({"name": "cat", "post_count": 1}))
    manager = FakeNetworkManager([reply])
    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        index_snapshot=_snapshot(TagSuggestion("cat", (), 1, 0)),
    )
    controller.updateInput("cat", 3)
    busy_signal_count = 0

    def close_from_queue_state():
        nonlocal busy_signal_count
        if not controller.remoteBusy:
            return
        busy_signal_count += 1
        if busy_signal_count == 2:
            controller.closeSuggestions()

    controller.remoteStateChanged.connect(close_from_queue_state)
    QTest.qWait(350)
    APPLICATION.processEvents()

    assert busy_signal_count >= 2
    assert manager.requests == []
    assert controller.remoteBusy is False
    assert controller._task_queue.has_work is False


def test_model_count_listener_can_shutdown_before_debounce_starts():
    manager = FakeNetworkManager([])
    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        index_snapshot=_snapshot(TagSuggestion("cat", (), 1, 0)),
    )
    controller.suggestionsModel.countChanged.connect(controller.shutdown)

    controller.updateInput("cat", 3)
    QTest.qWait(350)
    APPLICATION.processEvents()

    assert manager.requests == []
    assert controller.suggestionsModel.count == 0
    assert controller.remoteBusy is False
    assert controller.debouncePending is False


def test_remote_merge_keeps_candidates_beyond_initial_visible_twenty():
    local = tuple(
        TagSuggestion(
            f"cat_{index:02d}",
            (),
            1_000 - index if index < 20 else 500 - index,
            0,
        )
        for index in range(25)
    )
    reply = FakeReply(
        _tag_payload(
            *(
                {"name": f"cat_{index:02d}", "post_count": 0, "category": 0}
                for index in range(20)
            )
        )
    )
    manager = FakeNetworkManager([reply])
    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        network_manager=manager,
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        index_snapshot=_snapshot(*local),
    )

    controller.updateInput("cat", 3)
    assert controller.suggestionsModel.count == 20
    assert _wait_until(lambda: len(manager.requests) == 1)
    reply.finish()

    final_names = [item.name for item in controller.suggestionsModel.items]
    assert len(final_names) == 20
    assert final_names[:5] == [f"cat_{index:02d}" for index in range(20, 25)]


def _import_result(name: str) -> TagCsvImportResult:
    snapshot = _snapshot(TagSuggestion(name, ("虚构名称",), 1, 0))
    return TagCsvImportResult(snapshot, 1, 1, 0, 0, 1, "utf-8")


def test_successful_import_replaces_index_saves_absolute_path_before_completion(
    tmp_path,
):
    csv_path = Path(tmp_path) / "nested" / "success.csv"
    csv_path.parent.mkdir()
    csv_path.write_text("placeholder", encoding="utf-8")
    translations = TagTranslationStore()
    events: list[tuple[str, object]] = []
    controller = None

    def save_path(path: Path) -> None:
        assert controller is not None
        events.append(("save", path))
        assert controller._index.records[0].name == "imported"

    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        csv_loader=lambda path, *, cancel_event: _import_result("imported"),
        csv_path_saver=save_path,
        translation_store=translations,
    )
    translations.snapshotChanged.connect(
        lambda: events.append(("snapshot", translations.snapshot.records[0].name))
    )
    controller.importStateChanged.connect(
        lambda: (
            events.append(("state", controller.importStatusText))
            if not controller.importBusy
            else None
        )
    )
    outcomes = []
    controller.importCompleted.connect(
        lambda outcome: (events.append(("complete", outcome)), outcomes.append(outcome))
    )

    controller.importCsv(QUrl.fromLocalFile(str(csv_path)))

    assert _wait_until(lambda: len(outcomes) == 1)
    resolved_path = csv_path.resolve()
    assert [event[0] for event in events] == [
        "snapshot",
        "save",
        "state",
        "complete",
    ]
    assert events[1] == ("save", resolved_path)
    assert outcomes[0].path == resolved_path
    assert outcomes[0].generation == 1
    assert outcomes[0].path_saved is True


def test_successful_import_without_path_saver_does_not_claim_path_was_saved(
    tmp_path,
):
    csv_path = Path(tmp_path) / "success.csv"
    csv_path.write_text("placeholder", encoding="utf-8")
    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        csv_loader=lambda path, *, cancel_event: _import_result("imported"),
    )
    outcomes = []
    controller.importCompleted.connect(outcomes.append)

    controller.importCsv(QUrl.fromLocalFile(str(csv_path)))

    assert _wait_until(lambda: len(outcomes) == 1)
    assert outcomes[0].succeeded is True
    assert outcomes[0].path_saved is None


def test_begin_shutdown_rejects_late_import_result_before_formal_shutdown(
    tmp_path,
):
    csv_path = Path(tmp_path) / "late.csv"
    csv_path.write_text("placeholder", encoding="utf-8")
    saved_paths: list[Path] = []
    old_snapshot = _snapshot(TagSuggestion("old", ("旧值",), 1, 0))
    controller = DanbooruTagSuggestionController(
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

    assert controller._index == old_snapshot
    assert saved_paths == []
    assert controller._import_generation == 8


def test_success_callback_starting_new_import_never_resaves_old_path(tmp_path):
    first_path = Path(tmp_path) / "first.csv"
    second_path = Path(tmp_path) / "second.csv"
    first_path.write_text("placeholder", encoding="utf-8")
    second_path.write_text("placeholder", encoding="utf-8")
    saved_paths: list[Path] = []
    outcomes = []

    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        csv_loader=lambda path, *, cancel_event: _import_result(path.stem),
        csv_path_saver=saved_paths.append,
    )

    def start_second_import(outcome) -> None:
        outcomes.append(outcome)
        if outcome.path == first_path.resolve():
            controller.importCsv(QUrl.fromLocalFile(str(second_path)))

    controller.importCompleted.connect(start_second_import)
    controller.importCsv(QUrl.fromLocalFile(str(first_path)))

    assert _wait_until(lambda: len(outcomes) == 2)
    assert saved_paths == [first_path.resolve(), second_path.resolve()]
    assert [outcome.generation for outcome in outcomes] == [1, 2]
    assert controller._index.records[0].name == "second"


def test_success_callback_shutdown_does_not_save_another_path(tmp_path):
    csv_path = Path(tmp_path) / "success.csv"
    csv_path.write_text("placeholder", encoding="utf-8")
    saved_paths: list[Path] = []
    outcomes = []
    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        csv_loader=lambda path, *, cancel_event: _import_result("imported"),
        csv_path_saver=saved_paths.append,
    )

    def shutdown_on_completion(outcome) -> None:
        outcomes.append(outcome)
        controller.shutdown()

    controller.importCompleted.connect(shutdown_on_completion)
    controller.importCsv(QUrl.fromLocalFile(str(csv_path)))

    assert _wait_until(lambda: len(outcomes) == 1)
    APPLICATION.processEvents()
    assert saved_paths == [csv_path.resolve()]
    assert outcomes[0].path_saved is True
    assert controller.importBusy is False


def test_path_save_failure_keeps_new_index_and_persisted_old_path(tmp_path):
    old_path = Path(tmp_path) / "old.csv"
    new_path = Path(tmp_path) / "new.csv"
    new_path.write_text("placeholder", encoding="utf-8")
    persisted_paths = [old_path]
    save_attempts: list[Path] = []

    def failing_saver(path: Path) -> None:
        save_attempts.append(path)
        raise OSError("simulated settings failure")

    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        csv_loader=lambda path, *, cancel_event: _import_result("new_index"),
        csv_path_saver=failing_saver,
        index_snapshot=_snapshot(TagSuggestion("old_index", ("旧索引",), 1, 0)),
    )
    outcomes = []
    controller.importCompleted.connect(outcomes.append)

    controller.importCsv(QUrl.fromLocalFile(str(new_path)))

    assert _wait_until(lambda: len(outcomes) == 1)
    assert save_attempts == [new_path.resolve()]
    assert persisted_paths == [old_path]
    assert controller._index.records[0].name == "new_index"
    assert outcomes[0].succeeded is True
    assert outcomes[0].error_code == "settings_save_failed"
    assert outcomes[0].path_saved is False
    assert "路径保存失败" in controller.importStatusText


def test_saved_csv_load_rebuilds_index_without_resaving_path(tmp_path):
    csv_path = Path(tmp_path) / "saved.csv"
    csv_path.write_text("placeholder", encoding="utf-8")
    save_attempts: list[Path] = []
    loaded_paths: list[Path] = []

    def loader(path: Path, *, cancel_event: threading.Event):
        loaded_paths.append(path)
        return _import_result("restored")

    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        csv_loader=loader,
        csv_path_saver=save_attempts.append,
    )
    outcomes = []
    controller.importCompleted.connect(outcomes.append)

    controller.importSavedCsv(str(csv_path))

    assert _wait_until(lambda: len(outcomes) == 1)
    assert loaded_paths == [csv_path.resolve()]
    assert save_attempts == []
    assert controller._index.records[0].name == "restored"
    assert outcomes[0].path == csv_path.resolve()
    assert outcomes[0].path_saved is None


@pytest.mark.parametrize(
    ("startup_load", "error_code"),
    [
        (True, "file_missing"),
        (True, "csv_error"),
        (False, "csv_error"),
    ],
)
def test_failed_csv_load_preserves_existing_index_and_saved_path(
    tmp_path,
    startup_load,
    error_code,
):
    saved_path = Path(tmp_path) / "saved.csv"
    failed_path = Path(tmp_path) / "missing.csv"
    persisted_paths = [saved_path]
    save_attempts: list[Path] = []

    def failing_loader(path: Path, *, cancel_event: threading.Event):
        raise TagCsvImportError(error_code, "标签 CSV 无法载入。")

    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        index_snapshot=_snapshot(TagSuggestion("existing", ("现有",), 1, 0)),
        csv_loader=failing_loader,
        csv_path_saver=save_attempts.append,
    )
    outcomes = []
    controller.importCompleted.connect(outcomes.append)

    if startup_load:
        controller.importSavedCsv(str(failed_path))
    else:
        controller.importCsv(QUrl.fromLocalFile(str(failed_path)))

    assert _wait_until(lambda: len(outcomes) == 1)
    assert controller._index.records[0].name == "existing"
    assert persisted_paths == [saved_path]
    assert save_attempts == []
    assert outcomes[0].succeeded is False
    assert outcomes[0].error_code == error_code


def test_controller_imports_local_qurl_recomputes_and_preserves_index_on_failure(
    tmp_path,
):
    good_path = Path(tmp_path) / "标签 库 100%.csv"
    good_path.write_text(
        "name,cn_name,post_count,category\nfictional_tag,虚构名称,3,0\n",
        encoding="utf-8",
    )
    bad_path = Path(tmp_path) / "bad.csv"
    bad_path.write_text("wrong,header\n", encoding="utf-8")
    translations = TagTranslationStore()
    snapshot_changes = []
    translations.snapshotChanged.connect(
        lambda: snapshot_changes.append(translations.snapshot)
    )
    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        translation_store=translations,
    )
    outcomes = []
    controller.importCompleted.connect(outcomes.append)
    controller.updateInput("虚构", 2)

    controller.importCsv(QUrl.fromLocalFile(str(good_path)))
    assert controller.importBusy is True
    assert _wait_until(lambda: len(outcomes) == 1)
    assert outcomes[-1].succeeded is True
    assert controller.localTagCount == 1
    assert controller.suggestionsModel.items[0].name == "fictional_tag"
    assert "总记录 1" in controller.importStatusText

    controller.importCsv(QUrl.fromLocalFile(str(bad_path)))
    assert _wait_until(lambda: len(outcomes) == 2)
    assert outcomes[-1].succeeded is False
    assert controller.localTagCount == 1
    assert controller.suggestionsModel.items[0].name == "fictional_tag"

    controller.importCsv(QUrl("https://example.invalid/tags.csv"))
    assert outcomes[-1].error_code == "invalid_file_url"
    assert controller.localTagCount == 1
    assert len(snapshot_changes) == 1


def test_new_import_cancels_old_worker_and_shutdown_waits_for_worker(tmp_path):
    first_path = Path(tmp_path) / "first.csv"
    second_path = Path(tmp_path) / "second.csv"
    first_path.write_text("placeholder", encoding="utf-8")
    second_path.write_text("placeholder", encoding="utf-8")
    first_started = threading.Event()
    first_cancelled = threading.Event()
    shutdown_started = threading.Event()
    shutdown_exited = threading.Event()

    def loader(path: Path, *, cancel_event: threading.Event):
        if path.name == "first.csv":
            first_started.set()
            while not cancel_event.wait(0.005):
                pass
            first_cancelled.set()
            raise TagCsvImportCancelled()
        if path.name == "second.csv":
            return _import_result("second_tag")
        shutdown_started.set()
        while not cancel_event.wait(0.005):
            pass
        time.sleep(0.03)
        shutdown_exited.set()
        raise TagCsvImportCancelled()

    translations = TagTranslationStore()
    snapshot_changes = []
    translations.snapshotChanged.connect(lambda: snapshot_changes.append(True))
    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        csv_loader=loader,
        translation_store=translations,
    )
    controller.importCsv(QUrl.fromLocalFile(str(first_path)))
    assert _wait_until(first_started.is_set)
    controller.importCsv(QUrl.fromLocalFile(str(second_path)))
    assert first_cancelled.is_set()
    assert _wait_until(lambda: controller.localTagCount == 1)
    assert controller.importBusy is False

    third_path = Path(tmp_path) / "third.csv"
    third_path.write_text("placeholder", encoding="utf-8")
    controller.importCsv(QUrl.fromLocalFile(str(third_path)))
    assert _wait_until(shutdown_started.is_set)
    controller.shutdown()

    assert shutdown_exited.is_set()
    assert controller.importBusy is False
    assert controller.suggestionsModel.count == 0
    assert snapshot_changes == [True]


def test_import_state_listener_can_shutdown_before_worker_starts(tmp_path):
    csv_path = Path(tmp_path) / "never-started.csv"
    csv_path.write_text("placeholder", encoding="utf-8")
    loader_called = threading.Event()

    def loader(path: Path, *, cancel_event: threading.Event):
        loader_called.set()
        return _import_result("unexpected")

    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        csv_loader=loader,
    )

    def shutdown_while_starting():
        if controller.importBusy:
            controller.shutdown()

    controller.importStateChanged.connect(shutdown_while_starting)
    controller.importCsv(QUrl.fromLocalFile(str(csv_path)))
    APPLICATION.processEvents()

    assert loader_called.is_set() is False
    assert controller.importBusy is False
    assert controller.localTagCount == 0


def test_import_state_listener_can_replace_not_yet_started_worker(tmp_path):
    first_path = Path(tmp_path) / "first.csv"
    second_path = Path(tmp_path) / "second.csv"
    first_path.write_text("placeholder", encoding="utf-8")
    second_path.write_text("placeholder", encoding="utf-8")
    loaded_paths: list[str] = []

    def loader(path: Path, *, cancel_event: threading.Event):
        loaded_paths.append(path.name)
        return _import_result(path.stem)

    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        csv_loader=loader,
    )
    replaced = False

    def replace_while_starting():
        nonlocal replaced
        if controller.importBusy and not replaced:
            replaced = True
            controller.importCsv(QUrl.fromLocalFile(str(second_path)))

    controller.importStateChanged.connect(replace_while_starting)
    controller.importCsv(QUrl.fromLocalFile(str(first_path)))

    assert _wait_until(lambda: controller.localTagCount == 1)
    assert loaded_paths == ["second.csv"]
    assert controller.suggestionsModel.count == 0
    assert controller._index.records[0].name == "second"


def test_invalid_url_state_listener_shutdown_suppresses_late_completion():
    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
    )
    outcomes = []
    controller.importCompleted.connect(outcomes.append)
    controller.importStateChanged.connect(controller.shutdown)

    controller.importCsv(QUrl("https://example.invalid/tags.csv"))

    assert outcomes == []
    assert controller.importBusy is False


def test_invalid_url_state_listener_new_import_suppresses_old_completion(tmp_path):
    csv_path = Path(tmp_path) / "replacement.csv"
    csv_path.write_text("placeholder", encoding="utf-8")
    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        csv_loader=lambda path, *, cancel_event: _import_result("replacement"),
    )
    outcomes = []
    controller.importCompleted.connect(outcomes.append)
    started = False

    def start_valid_import():
        nonlocal started
        if not started:
            started = True
            controller.importCsv(QUrl.fromLocalFile(str(csv_path)))

    controller.importStateChanged.connect(start_valid_import)
    controller.importCsv(QUrl("https://example.invalid/tags.csv"))

    assert _wait_until(lambda: len(outcomes) == 1)
    assert outcomes[0].succeeded is True
    assert controller._index.records[0].name == "replacement"


def test_invalid_url_cancels_running_import_and_preserves_existing_index(tmp_path):
    csv_path = Path(tmp_path) / "slow.csv"
    csv_path.write_text("placeholder", encoding="utf-8")
    started = threading.Event()
    cancelled = threading.Event()

    def loader(path: Path, *, cancel_event: threading.Event):
        started.set()
        while not cancel_event.wait(0.005):
            pass
        cancelled.set()
        raise TagCsvImportCancelled()

    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        index_snapshot=_snapshot(TagSuggestion("existing", ("现有",), 1, 0)),
        csv_loader=loader,
    )
    outcomes = []
    controller.importCompleted.connect(outcomes.append)
    controller.importCsv(QUrl.fromLocalFile(str(csv_path)))
    assert _wait_until(started.is_set)

    controller.importCsv(QUrl())
    APPLICATION.processEvents()

    assert cancelled.is_set()
    assert len(outcomes) == 1
    assert outcomes[0].error_code == "invalid_file_url"
    assert controller._index.records[0].name == "existing"
    assert controller.importBusy is False


def test_import_state_listener_invalid_url_cancels_unstarted_worker(tmp_path):
    csv_path = Path(tmp_path) / "never-started.csv"
    csv_path.write_text("placeholder", encoding="utf-8")
    loader_called = threading.Event()

    def loader(path: Path, *, cancel_event: threading.Event):
        loader_called.set()
        return _import_result("unexpected")

    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        csv_loader=loader,
    )
    outcomes = []
    controller.importCompleted.connect(outcomes.append)
    replaced = False

    def replace_with_invalid_url():
        nonlocal replaced
        if controller.importBusy and not replaced:
            replaced = True
            controller.importCsv(QUrl("https://example.invalid/tags.csv"))

    controller.importStateChanged.connect(replace_with_invalid_url)
    controller.importCsv(QUrl.fromLocalFile(str(csv_path)))
    APPLICATION.processEvents()

    assert loader_called.is_set() is False
    assert len(outcomes) == 1
    assert outcomes[0].error_code == "invalid_file_url"
    assert controller.localTagCount == 0


def test_malformed_csv_failure_preserves_existing_index(tmp_path):
    csv_path = Path(tmp_path) / "malformed.csv"
    csv_path.write_text(
        "name,cn_name,post_count,category\ncat,\"未闭合,1,0\n",
        encoding="utf-8",
    )
    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        index_snapshot=_snapshot(TagSuggestion("existing", ("现有",), 1, 0)),
    )
    outcomes = []
    controller.importCompleted.connect(outcomes.append)

    controller.importCsv(QUrl.fromLocalFile(str(csv_path)))

    assert _wait_until(lambda: len(outcomes) == 1)
    assert outcomes[0].succeeded is False
    assert outcomes[0].error_code == "csv_error"
    assert controller._index.records[0].name == "existing"


def test_success_completion_close_keeps_recomputed_suggestions_closed(tmp_path):
    csv_path = Path(tmp_path) / "success.csv"
    csv_path.write_text("placeholder", encoding="utf-8")
    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        csv_loader=lambda path, *, cancel_event: _import_result("imported"),
    )
    controller.updateInput("虚构", 2)

    def close_after_success(outcome):
        if outcome.succeeded:
            controller.closeSuggestions()

    controller.importCompleted.connect(close_after_success)
    controller.importCsv(QUrl.fromLocalFile(str(csv_path)))

    assert _wait_until(lambda: controller.localTagCount == 1)
    APPLICATION.processEvents()
    assert controller.suggestionsModel.count == 0
    assert controller.isOpen is False


def test_success_completion_shutdown_prevents_recompute(tmp_path):
    csv_path = Path(tmp_path) / "success.csv"
    csv_path.write_text("placeholder", encoding="utf-8")
    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        csv_loader=lambda path, *, cancel_event: _import_result("imported"),
    )
    controller.updateInput("虚构", 2)
    controller.importCompleted.connect(
        lambda outcome: controller.shutdown() if outcome.succeeded else None
    )

    controller.importCsv(QUrl.fromLocalFile(str(csv_path)))

    assert _wait_until(lambda: controller.localTagCount == 1)
    APPLICATION.processEvents()
    assert controller.suggestionsModel.count == 0
    assert controller.isOpen is False
    assert controller.importBusy is False


def test_success_completion_new_import_suppresses_old_recompute(tmp_path):
    first_path = Path(tmp_path) / "first.csv"
    second_path = Path(tmp_path) / "second.csv"
    first_path.write_text("placeholder", encoding="utf-8")
    second_path.write_text("placeholder", encoding="utf-8")
    second_started = threading.Event()
    release_second = threading.Event()

    def loader(path: Path, *, cancel_event: threading.Event):
        if path.name == "first.csv":
            return _import_result("first")
        second_started.set()
        while not release_second.wait(0.005):
            if cancel_event.is_set():
                raise TagCsvImportCancelled()
        return _import_result("second")

    controller = DanbooruTagSuggestionController(
        "https://example.invalid",
        queue_config=TaskQueueConfig(minimum_interval_seconds=0),
        csv_loader=loader,
    )
    controller.updateInput("虚构", 2)
    outcomes = []

    def start_second_import(outcome):
        outcomes.append(outcome)
        if outcome.succeeded and outcome.result.snapshot.records[0].name == "first":
            controller.importCsv(QUrl.fromLocalFile(str(second_path)))

    controller.importCompleted.connect(start_second_import)
    controller.importCsv(QUrl.fromLocalFile(str(first_path)))

    assert _wait_until(second_started.is_set)
    assert controller.suggestionsModel.count == 0
    assert controller.isOpen is False
    release_second.set()
    assert _wait_until(lambda: len(outcomes) == 2)
    assert controller._index.records[0].name == "second"
    assert controller.suggestionsModel.items[0].name == "second"
