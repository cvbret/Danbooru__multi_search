import json

import pytest
from PySide6.QtCore import QObject, QMetaObject, Q_ARG, QPointF, Qt, QUrl

from tests.test_favorite_controller import APPLICATION, IMAGE
from tests.test_download_manager import Network
from tests.test_pool_requests import FakeReply
from tests.test_pool_post_parsing import raw_post
from tests.test_qml_pool_reader import start_reader
from tests.test_qml_favorites import click, wait, item
from tests.test_task_queue import ManualClock, ManualScheduler
from tests.m2_qml_harness import close_m2_qml_test_engine


def control(dialog, name):
    result = dialog.findChild(QObject, name)
    assert result is not None, name
    return result


def press(root, dialog, name):
    button = control(dialog, name)
    viewport = control(dialog, 'downloadRequestScroll').property('contentItem')
    center = button.mapToScene(QPointF(button.width() / 2, button.height() / 2))
    middle = viewport.mapToScene(QPointF(0, viewport.height() / 2))
    maximum = max(0, viewport.property('contentHeight') - viewport.height())
    viewport.setProperty('contentY', max(0, min(maximum, viewport.property('contentY') + center.y() - middle.y())))
    APPLICATION.processEvents()
    click(root, button)


@pytest.mark.parametrize('narrow,source', [(False, 'browser'), (True, 'reader')])
def test_real_pool_confirmation_cancel_directory_and_linked_batch(tmp_path, narrow, source):
    engine, root, bridge, session, network = start_reader(narrow=narrow, identifiers=(9, 8, 9))
    warnings = []
    engine.warnings.connect(warnings.extend)
    files = Network(ManualScheduler(ManualClock()))
    session.download_manager._network_manager = files
    try:
        if source == 'browser':
            click(root, item(root, 'readerBackButton'))
        name = 'poolDownloadAllButton' if source == 'browser' else 'readerDownloadAllButton'
        count = len(network.requests)
        history = session.history_repository.list_queries()
        click(root, item(root, name))
        dialog = item(root, 'poolDownloadRequestDialog')
        assert wait(lambda: dialog.property('opened'))
        assert not session.pool_download_manager.repository.summaries() and len(network.requests) == count
        press(root, dialog, 'cancelDownloadRequestButton')
        assert wait(lambda: not dialog.property('opened'))
        assert not session.pool_download_manager.repository.summaries()
        click(root, item(root, name))
        assert wait(lambda: dialog.property('opened'))
        destination = tmp_path / '整池 下载'
        destination.mkdir()
        chooser = control(dialog, 'downloadDestinationFolderDialog')
        token = session.pool_downloads.beginDirectorySelection('request')
        chooser.setProperty('choiceToken', token)
        assert QMetaObject.invokeMethod(chooser, 'acceptSelection', Qt.ConnectionType.DirectConnection,
                                      Q_ARG('QVariant', QUrl.fromLocalFile(str(destination))))
        assert not session.pool_download_manager.repository.summaries()
        control(dialog, 'downloadConflictPolicy').setProperty('currentIndex', 2)
        reply = FakeReply(json.dumps([raw_post(number, file_ext='png', preview_file_url=IMAGE,
            large_file_url=IMAGE, file_url=f'https://example.invalid/{number}.png') for number in (8, 9)]).encode())
        network.replies.append(reply)
        press(root, dialog, 'confirmDownloadRequestButton')
        assert wait(lambda: len(network.requests) == count + 1)
        assert root.property('activePage') == 'downloads'
        assert not session.download_manager._repository.list_batches()
        reply.finish()
        assert wait(lambda: len(session.download_manager._repository.list_batches()) == 1)
        key = session.download_manager._repository.list_batches()[0]
        batch = session.download_manager.snapshot(key)
        assert [entry.request.filename for entry in batch.items] == ['00001_9.png', '00002_8.png', '00003_9.png']
        assert all(entry.directory == destination and entry.policy.value == 'auto_number' for entry in batch.items)
        plan = session.pool_download_manager.repository.summaries()[0].plan
        assert wait(lambda: plan.plan_id in [entry.plan.plan_id for entry in session.pool_downloads.plansModel.items])
        click(root, item(root, 'poolDownloadPlan_' + plan.plan_id))
        assert wait(lambda: len(session.pool_downloads.batchKeys) == 1)
        click(root, item(root, 'viewPoolDownloadBatchButton'))
        assert wait(lambda: session.download_ui.selectedBatchKey == key)
        assert root.property('activePage') == 'downloads'
        assert session.history_repository.list_queries() == history
        assert not [error.toString() for error in warnings if 'TypeError' in error.toString() or 'ReferenceError' in error.toString()]
    finally:
        close_m2_qml_test_engine(engine)
