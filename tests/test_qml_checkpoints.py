from __future__ import annotations

from dataclasses import replace

from PySide6.QtTest import QTest

from danbooru_multi_search.app.bootstrap import initialize_runtime
from danbooru_multi_search.danbooru.query_definition import GalleryQueryDefinition, QueryMode
from danbooru_multi_search.danbooru.query_planning import QueryRating
from danbooru_multi_search.danbooru.query_types import QueryExecutionLimits, QueryExecutionState
from danbooru_multi_search.danbooru.result_processing import ResultFilterSpec, ResultSortSpec
from tests.m2_qml_harness import create_m2_qml_test_engine, close_m2_qml_test_engine
from tests.test_favorite_controller import APPLICATION, post
from tests.test_qml_favorites import NoNetwork, item, click, wait
from tests.test_query_execution import ControlledPageFactory


def start(tmp_path):
    runtime = initialize_runtime(tmp_path)
    network = NoNetwork()
    engine, bridge = create_m2_qml_test_engine(APPLICATION, danbooru_base_url="https://example.invalid", runtime=runtime, network_manager=network)
    root = engine.rootObjects()[0]
    session = bridge.parent()
    factory = ControlledPageFactory()
    session.query_executor._attempt_factory = factory
    root.requestActivate()
    assert QTest.qWaitForWindowExposed(root)
    return engine, root, session, bridge, factory, network, runtime


def test_history_resume_actual_click_requests_next_page_and_close_settles_checkpoint(tmp_path):
    engine, root, session, bridge, factory, network, runtime = start(tmp_path)
    try:
        definition = GalleryQueryDefinition(QueryMode.STANDARD, "a", QueryRating.ALL, ResultFilterSpec(), ResultSortSpec(), QueryExecutionLimits(page_size=1))
        bridge.loadDraftDefinition(definition)
        assert bridge.executeStandard()
        assert wait(lambda: len(factory.attempts) == 1)
        from danbooru_multi_search.tasks import TaskAttemptResult
        factory.attempts[0].completion(TaskAttemptResult.success((replace(post(42), general_tags=("a",)),)))
        assert wait(lambda: len(factory.attempts) == 2)
        assert session.query_controller.cancel()
        query = session.history_repository.latest_query()
        click(root, item(root, "historyButton"))
        click(root, item(root, "historyRow_" + str(query.id)))
        assert session.query_checkpoints.canResume, session.query_checkpoints.messageText
        click(root, item(root, "resumeCheckpointButton"))
        assert wait(lambda: len(factory.attempts) == 3)
        assert factory.attempts[-1].page == 2 and network.requests == []
        session.shutdown()
        record = session.query_checkpoints._repository.get_for_history(query.id)
        assert not record.checkpoint.execution.active and not record.checkpoint.execution.resume_reason
        assert record.checkpoint.execution.state is QueryExecutionState.PARTIAL_CANCELLED
        assert session.shutdown_completed and session._shutdown_errors == []
    finally:
        close_m2_qml_test_engine(engine)


def test_formal_restart_does_not_resume_or_fetch_images_automatically(tmp_path):
    engine, root, session, bridge, factory, network, runtime = start(tmp_path)
    bridge.applyAdvancedText("a")
    assert bridge.executeStandard()
    assert wait(lambda: len(factory.attempts) == 1)
    history_id = session.history_repository.latest_query().id
    close_m2_qml_test_engine(engine)
    engine, root, session, bridge, factory, network, _runtime = start(tmp_path)
    try:
        QTest.qWait(300)
        assert factory.attempts == [] and network.requests == []
        assert bridge.postsModel.rowCount() == 0
        click(root, item(root, "historyButton"))
        QTest.qWait(50)
        click(root, item(root, "historyRow_" + str(history_id)))
        assert session.query_checkpoints.canResume, (session.query_checkpoints.messageText, session.query_checkpoints.selectionToken, item(root, "workspaceTabs").property("currentIndex"), item(root, "historyRow_" + str(history_id)).property("visible"))
    finally:
        close_m2_qml_test_engine(engine)


def test_final_page_save_error_is_visible_in_query_status_with_empty_latest_expression(tmp_path):
    from danbooru_multi_search.tasks import TaskAttemptResult
    engine, root, session, bridge, factory, network, runtime = start(tmp_path)
    try:
        assert bridge.executeLatest()
        assert wait(lambda: len(factory.attempts) == 1)
        with runtime.database.connect() as connection:
            connection.execute("CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON query_checkpoints BEGIN SELECT RAISE(ABORT, 'final visible failure'); END")
        factory.attempts[0].completion(TaskAttemptResult.success(()))
        assert wait(lambda: item(root, "queryFailureText").property("visible"))
        assert "final visible failure" in item(root, "queryFailureText").property("text")
        assert bridge.queryState == "failed" and network.requests == []
        with runtime.database.connect() as connection:
            connection.execute("DROP TRIGGER reject_checkpoint")
        session.shutdown()
        assert session.shutdown_completed and session._shutdown_errors == []
        saved = session.query_checkpoints._repository.get_for_history(session.history_repository.latest_query().id)
        assert not saved.checkpoint.execution.active and saved.checkpoint.execution.persistence_error
    finally:
        close_m2_qml_test_engine(engine)
