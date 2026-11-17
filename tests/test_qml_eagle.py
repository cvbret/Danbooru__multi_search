import json
from dataclasses import replace

import pytest
from PySide6.QtCore import QObject, QPointF
from PySide6.QtTest import QTest

from danbooru_multi_search.integrations.eagle import EagleWebApiBridge
from danbooru_multi_search.core.bootstrap import initialize_runtime
from danbooru_multi_search.platform import PlatformAdapter
from tests.test_eagle_client import eagle_server, ServerResponse, app_info, jsend
from tests.test_eagle_ui import responses
from tests.test_favorite_controller import APPLICATION, post
from tests.test_qml_favorites import NoNetwork, item, click, wait
from tests.m2_qml_harness import create_m2_qml_test_engine, close_m2_qml_test_engine


def reveal(panel, name):
    def find():
        pending = [panel.property('contentItem')]
        while pending:
            candidate = pending.pop()
            if candidate.objectName() == name and candidate.width() > 0:
                return candidate
            pending.extend(candidate.childItems())
        return None
    assert wait(lambda: find() is not None), name
    control = find()
    scroll = panel.findChild(QObject, 'eaglePanelScroll').property('contentItem')
    center = control.mapToScene(QPointF(control.width() / 2, control.height() / 2))
    middle = scroll.mapToScene(QPointF(0, scroll.height() / 2))
    maximum = max(0, scroll.property('contentHeight') - scroll.height())
    scroll.setProperty('contentY', max(0, min(maximum, scroll.property('contentY') + center.y() - middle.y())))
    APPLICATION.processEvents()
    return control


def press(root, panel, name):
    control = reveal(panel, name)
    center = control.mapToScene(QPointF(control.width() / 2, control.height() / 2))
    assert 0 < center.x() < root.width() and 0 < center.y() < root.height()
    click(root, control)


@pytest.mark.parametrize('narrow', [False, True])
def test_eagle_real_error_retry_long_library_path_target_and_create(narrow, tmp_path, eagle_server):
    path = 'D:/' + '很长的素材库目录 with spaces/' * 12 + '演示.library'
    server = eagle_server(ServerResponse(jsend(app_info(version='3.0.0'))),
                          *responses(path=path), *responses(created=True, path=path))
    network = NoNetwork()
    runtime = initialize_runtime(tmp_path)
    services = replace(runtime.platform.services, eagle=EagleWebApiBridge(server.base_url))
    runtime = replace(runtime, platform=PlatformAdapter(runtime.platform.capabilities, services))
    engine, bridge = create_m2_qml_test_engine(APPLICATION, danbooru_base_url='https://example.invalid',
                                            runtime=runtime, network_manager=network)
    root, session = engine.rootObjects()[0], bridge.parent()
    # Resolve popup/entry wrappers before asynchronous gallery image objects.
    # The actual image-filled gallery and every mouse operation remain enabled.
    panel = root.findChild(QObject, 'eaglePanel')
    assert panel is not None
    entry = item(root, 'eagleButton')
    bridge.postsModel.set_posts((post(1), post(2)))
    root.requestActivate()
    assert QTest.qWaitForWindowExposed(root)
    if narrow:
        root.setWidth(820)
        root.setHeight(600)
    warnings = []
    engine.warnings.connect(warnings.extend)
    try:
        assert server.requests == [] and not session.eagle_runtime.thread_started
        click(root, entry)
        assert wait(lambda: panel.property('opened'))
        QTest.qWait(40)
        assert server.requests == []
        press(root, panel, 'refreshEagleButton')
        assert wait(lambda: not session.eagle.busy and '版本过低' in session.eagle.statusText)
        assert not session.eagle.ready and session.eagle.detailsText
        press(root, panel, 'refreshEagleButton')
        assert wait(lambda: session.eagle.ready)
        assert str(session.eagle._snapshot.library.path) == session.eagle.library['path']
        assert len(session.eagle.library['path']) > 260 and not session.eagle.target['valid']
        press(root, panel, 'eagleFolder_root')
        assert session.eagle.target['key'] == 'root'
        name = reveal(panel, 'newEagleFolderName')
        name.setProperty('text', 'M5 new folder')
        press(root, panel, 'createEagleFolderButton')
        assert wait(lambda: not session.eagle.busy and session.eagle.ready)
        write_requests = [request for request in server.requests if request.method == 'POST']
        assert len(write_requests) == 1 and json.loads(write_requests[0].body) == {'name': 'M5 new folder'}
        assert session.eagle.target['key'] == 'root'
        press(root, panel, 'eagleFolder_folder:NEW')
        assert session.eagle.target['key'] == 'folder:NEW'
        press(root, panel, 'closeEaglePanelButton')
        assert wait(lambda: not panel.property('opened'))
        assert network.requests == []
        assert not [error.toString() for error in warnings if 'TypeError' in error.toString() or 'ReferenceError' in error.toString()]
    finally:
        session.shutdown()
        assert session.eagle_runtime.shutdown_completed
        close_m2_qml_test_engine(engine)
