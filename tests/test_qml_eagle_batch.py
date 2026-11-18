import json
from contextlib import contextmanager
from dataclasses import replace

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QNetworkAccessManager
from PySide6.QtTest import QTest

from danbooru_multi_search.app.bootstrap import initialize_runtime
from danbooru_multi_search.app.eagle_service import EagleBackend
from danbooru_multi_search.integrations.eagle import EagleWebApiBridge
from danbooru_multi_search.platform import PlatformAdapter
from danbooru_multi_search.storage import Database
from danbooru_multi_search.storage.eagle_imports import EagleImportRepository
from tests.m2_qml_harness import close_m2_qml_test_engine, create_m2_qml_test_engine
from tests.test_eagle_client import ServerResponse, eagle_server
from tests.test_eagle_import import _Bridge, _Server, _png
from tests.test_favorite_controller import APPLICATION, IMAGE
from tests.test_qml_eagle import press
from tests.test_qml_favorites import click, item, wait


def test_gallery_collects_three_posts_through_download_worker_and_repository(tmp_path, eagle_server, monkeypatch):
    png = _png()
    response = ServerResponse(png, headers={"Content-Type": "image/png"})
    media = eagle_server(response, response)
    posts = [dict(id=number, score=3, fav_count=7, rating="g", file_ext="png",
                  image_width=1, image_height=1, file_size=len(png), preview_file_url=IMAGE,
                  tag_string_artist="artist", tag_string_meta="", tag_string_general="",
                  tag_string_character="", tag_string_copyright="",
                  file_url=media.base_url + f"{number}.png" if number != 102 else None)
             for number in (103, 102, 101)]
    query_server = eagle_server(ServerResponse(json.dumps(posts).encode(), headers={"Content-Type": "application/json"}))
    base_url = query_server.base_url.split("/api/")[0]
    remote = _Server()

    def factory(_configured_bridge, database_path):
        @contextmanager
        def backend(cancelled, deadline):
            bridge = _Bridge(remote)
            yield EagleBackend(bridge, lambda: bridge.sent, EagleImportRepository(Database(database_path)))
        return backend

    monkeypatch.setattr("danbooru_multi_search.ui.launcher.create_eagle_backend_factory", factory)
    runtime = initialize_runtime(tmp_path)
    services = replace(runtime.platform.services, eagle=EagleWebApiBridge(query_server.base_url))
    runtime = replace(runtime, platform=PlatformAdapter(runtime.platform.capabilities, services))
    manager = QNetworkAccessManager()
    engine, bridge = create_m2_qml_test_engine(APPLICATION, danbooru_base_url=base_url, runtime=runtime, network_manager=manager)
    root, session = engine.rootObjects()[0], bridge.parent()
    dialog = root.findChild(QObject, "eagleBatchDialog")
    panel = root.findChild(QObject, "eaglePanel")
    try:
        root.requestActivate()
        assert QTest.qWaitForWindowExposed(root)
        assert bridge.executeLatest()
        assert wait(lambda: bridge.visibleCount == 3), bridge.errorText
        click(root, item(root, "collectQueryEagle"))
        assert wait(lambda: dialog.property("opened")), session.eagle_batch.errorText
        assert item(root, "batchCandidateCount").property("value") == 20
        assert not item(root, "confirmEagleBatch").isEnabled()
        click(root, item(root, "chooseEagleBatchTarget"))
        assert wait(lambda: panel.property("opened"))
        press(root, panel, "refreshEagleButton")
        assert wait(lambda: session.eagle.ready)
        press(root, panel, "eagleFolder_folder:folder-A")
        press(root, panel, "closeEaglePanelButton")
        assert wait(lambda: not panel.property("opened"))
        assert not remote.writes and not media.requests
        click(root, item(root, "confirmEagleBatch"))
        assert wait(lambda: session.eagle_batch.progress["phase"] == "completed", 10000), (
            session.eagle_batch.errorText, session.eagle_batch.progress, session.eagle_batch.rows)
        stats = session.eagle_batch.progress
        assert (stats["planned"], stats["count"], stats["processed"], stats["success"], stats["skipped"], stats["failed"]) == (20,3,3,1,1,1)
        assert stats["ended"] and "不足" in stats["scope"]
        rows = session.eagle_batch.rows
        assert [r["key"] for r in rows] == ["103","102","101"]
        assert [(r["state"], r["quality"], r["format"]) for r in rows] == [
            ("已导入","原图","png"), ("失败","",""), ("重复跳过","原图","png")]
        assert len(media.requests) == 2 and len(remote.writes) == 1
        assert remote.writes[0][2] == "folder-A"
        repo = EagleImportRepository(runtime.database)
        assert repo.find_by_post("library-A", 103).item_id == repo.find_by_post("library-A", 101).item_id == "item-1"
        assert repo.find_by_post("library-A", 102) is None
        assert not list((runtime.paths.data_dir / "eagle-imports").glob("**/*.png"))
        click(root, item(root, "closeEagleBatch"))
        assert wait(lambda: not dialog.property("opened"))
        root.setWidth(820)
        root.setHeight(600)
        APPLICATION.processEvents()
        click(root, item(root, "postSelect_101"))
        click(root, item(root, "postSelect_103"))
        click(root, item(root, "collectSelectedEagle"))
        assert wait(lambda: dialog.property("opened")) and dialog.property("selectedOnly")
        # A new same-operation view can reorder existing rows. The confirmed
        # selection still belongs to the captured order and the same two IDs.
        assert bridge.postsModel.set_processed_posts(bridge.galleryOperationId, tuple(reversed(bridge.postsModel.processed_posts)))
        assert session.eagle_batch.pending["valid"]
        click(root, item(root, "confirmEagleBatch"))
        assert wait(lambda: session.eagle_batch.progress["phase"] == "completed" and
                    session.eagle_batch.progress["planned"] == 2)
        assert [r["key"] for r in session.eagle_batch.rows] == ["103","101"]
        assert session.eagle_batch.progress["skipped"] == 2
        assert len(media.requests) == 2 and len(remote.writes) == 1 and len(query_server.requests) == 1
    finally:
        close_m2_qml_test_engine(engine)
