import base64
import json
from dataclasses import replace

from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QNetworkAccessManager
from PySide6.QtTest import QTest

from danbooru_multi_search.app.bootstrap import initialize_runtime
from danbooru_multi_search.app.eagle_import_types import EaglePostImportOutcome
from danbooru_multi_search.app.eagle_service import EagleOperation, EagleResult
from danbooru_multi_search.danbooru import TagIndexSnapshot, TagSuggestion
from danbooru_multi_search.integrations.eagle import EagleWebApiBridge
from danbooru_multi_search.platform import PlatformAdapter
from tests.m2_qml_harness import create_m2_qml_test_engine, close_m2_qml_test_engine
from tests.test_eagle_client import ServerResponse, eagle_server
from tests.test_eagle_ui import responses
from tests.test_favorite_controller import APPLICATION, IMAGE, post
from tests.test_m2_session_state import _definition
from tests.test_qml_details import _open_post_details
from tests.test_qml_eagle import press
from tests.test_qml_favorites import click, item, wait


def test_uncertain_status_distinguishes_previous_attempt_from_current_write():
    engine, bridge = create_m2_qml_test_engine(APPLICATION, danbooru_base_url="https://example.invalid")
    session = bridge.parent()
    try:
        for sent, wording in (
            (False, "先前的导入结果待核实，请在 Eagle 中查看这件作品。"),
            (True, "本次写入请求已发送，结果待核实。请在 Eagle 中查看这件作品。"),
        ):
            session.eagle_import_job.result = EagleResult(
                "request", uncertain=True, post_started=sent,
                import_result=EaglePostImportOutcome("uncertain", item_identifier="EAGLE-42"),
            )
            assert session.eagle_import.statusText == wording + " · 素材编号 EAGLE-42"
    finally:
        close_m2_qml_test_engine(engine)


def test_detail_dialog_selects_real_target_and_passes_checked_original_and_options(
    tmp_path, eagle_server, monkeypatch,
):
    png = base64.b64decode(IMAGE.split(",", 1)[1])
    original = eagle_server(ServerResponse(png, headers={"Content-Type": "image/png"}))
    eagle = eagle_server(*responses())
    runtime = initialize_runtime(tmp_path)
    services = replace(runtime.platform.services, eagle=EagleWebApiBridge(eagle.base_url))
    runtime = replace(runtime, platform=PlatformAdapter(runtime.platform.capabilities, services))
    network = QNetworkAccessManager()
    engine, bridge = create_m2_qml_test_engine(APPLICATION, danbooru_base_url="https://example.invalid",
                                              runtime=runtime, network_manager=network)
    root, session = engine.rootObjects()[0], bridge.parent()
    dialog = root.findChild(QObject, "eagleImportDialog")
    panel = root.findChild(QObject, "eaglePanel")
    try:
        work = replace(post(42), file_url=original.base_url + "original.png", file_size=len(png))
        bridge.postsModel.set_posts((work,))
        bridge._gallery_definition = _definition("cat")
        session.translation_store.replace_snapshot(TagIndexSnapshot.build((
            TagSuggestion("sample_artist", ("示例画师",), 1, 1),
        )))
        root.requestActivate()
        assert QTest.qWaitForWindowExposed(root)
        _open_post_details(root, 0)
        bridge._gallery_definition = _definition("dog")
        click(root, item(root, "detailEagleImportButton"))
        assert wait(lambda: dialog.property("opened"))
        assert not item(root, "confirmEagleImport").isEnabled()
        assert eagle.requests == [] and original.requests == []
        click(root, item(root, "chooseEagleImportTarget"))
        assert wait(lambda: panel.property("opened"))
        press(root, panel, "refreshEagleButton")
        assert wait(lambda: session.eagle.ready)
        press(root, panel, "eagleFolder_folder:P")
        press(root, panel, "closeEaglePanelButton")
        assert wait(lambda: not panel.property("opened"))

        # Exercise the production QML, source capture, media download, retry file and
        # runtime dispatch. Isolate the import worker's Eagle writes from this UI test.
        captured = []
        def dispatch(request):
            captured.append(request)
            if request.operation == EagleOperation.PRECHECK_POST:
                outcome = EaglePostImportOutcome(state="ready")
            else:
                assert request.post_import.file_path.read_bytes() == png
                outcome = EaglePostImportOutcome(state="updated", item_identifier="EXISTING42")
            QTimer.singleShot(0, lambda: session.eagle_runtime.completed.emit(
                EagleResult(request.request_id, import_result=outcome)))
            return True

        monkeypatch.setattr(session.eagle_runtime, "start", dispatch)
        item(root, "eagleDuplicatePolicy").setProperty("currentIndex", 1)
        click(root, item(root, "eagleIncludeGeneral"))
        click(root, item(root, "eagleIncludeChinese"))
        click(root, item(root, "eagleIncludeQuery"))
        click(root, item(root, "confirmEagleImport"))
        assert wait(lambda: "EXISTING42" in session.eagle_import.statusText)
        assert [req.operation for req in captured] == [EagleOperation.PRECHECK_POST, EagleOperation.IMPORT_POST]
        confirmed = captured[-1].post_import
        assert confirmed.post == work and confirmed.post_url == "https://example.invalid/posts/42"
        assert confirmed.folder_identifier == "P" and confirmed.include_general and confirmed.update_duplicates
        assert not confirmed.include_meta and confirmed.include_chinese
        assert confirmed.translations == (("sample_artist", ("示例画师",)),)
        assert json.loads(confirmed.query_text)["normalized_expression"] == "cat"
        assert confirmed.pool_text == "未取得" and not confirmed.file_path.exists()
        assert len(original.requests) == 1 and all(req.method == "GET" for req in eagle.requests)

        source_revision = session.eagle_import.sourceRevision
        target_revision = session.eagle.revision
        assert session.eagle.selectTarget("root", target_revision)
        assert not session.eagle_import.confirm("42", source_revision, target_revision,
                                               False, False, False, False, False)
        assert session.detail_controller.open_post(replace(work, id=43))
        assert not item(root, "confirmEagleImport").isEnabled()
        assert not session.eagle_import.confirm("42", source_revision, session.eagle.revision,
                                               False, False, False, False, False)
        assert len(captured) == 2
    finally:
        close_m2_qml_test_engine(engine)
