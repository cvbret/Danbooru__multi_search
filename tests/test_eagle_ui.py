import time

import pytest
from PySide6.QtCore import QCoreApplication, QEvent

from danbooru_multi_search.integrations.eagle import EagleWebApiBridge, UnavailableEagleBridge
from danbooru_multi_search.platform.contracts import ServiceAvailability
from danbooru_multi_search.tasks import BoundedTaskQueue, TaskKind, TaskQueueConfig
from danbooru_multi_search.ui.eagle import EagleController
from danbooru_multi_search.app.eagle_runtime import EagleRuntime
from danbooru_multi_search.ui.launcher import create_eagle_backend_factory
from tests.test_eagle_client import eagle_server, ServerResponse, app_info, library_info, jsend
from tests.test_favorite_controller import APPLICATION
from tests.test_qml_favorites import wait
from tests.test_task_queue import ManualClock, ManualScheduler, FakeAbortable
from tests.test_eagle_qt_transport import server


def folder(identifier='P', name='父文件夹'):
    return {'id': identifier, 'name': name, 'children': []}


def folder_page(*folders):
    return {'data': list(folders), 'total': len(folders), 'offset': 0, 'limit': 1000}


def responses(*, created=False, post_error=False, path=None):
    library = library_info(path) if path else library_info()
    values = [app_info(), library, folder_page(folder()), library]
    if created:
        values += [folder('NEW', '新目录'), folder_page(folder(), folder('NEW', '新目录')), library]
    else:
        values += [library]
    result = [ServerResponse(jsend(value)) for value in values]
    if post_error:
        result[4:] = [ServerResponse(b'unknown result', status=500)]
    return result


@pytest.fixture
def env(eagle_server):
    values = []
    def create(*reply, capacity=64):
        server = eagle_server(*reply)
        scheduler = ManualScheduler(ManualClock())
        queue = BoundedTaskQueue(TaskQueueConfig(capacity=capacity, minimum_interval_seconds=0),
                                 clock=scheduler.clock, scheduler=scheduler)
        bridge = EagleWebApiBridge(server.base_url)
        runtime = EagleRuntime(create_eagle_backend_factory(bridge), bridge.availability, queue)
        controller = EagleController(runtime)
        values.append((controller, queue))
        return controller, scheduler, queue, server
    yield create
    for controller, queue in values:
        controller.shutdown()
        controller.runtime.shutdown()
        queue.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def connect(controller, scheduler):
    assert controller.refresh()
    scheduler.run_ready()
    assert wait(lambda: not controller.busy)
    assert controller.ready, (controller.statusText, controller.detailsText)


def test_startup_and_target_are_session_only_root_requires_explicit_choice(env):
    controller, scheduler, queue, server = env(*responses())
    APPLICATION.processEvents()
    assert server.requests == [] and not controller.runtime.thread_started and not controller.target['valid']
    connect(controller, scheduler)
    assert len(server.requests) == 5 and not controller.target['valid']
    assert not controller.selectTarget('root', controller.revision - 1)
    assert controller.selectTarget('root', controller.revision)
    assert controller.target == {'key': 'root', 'name': '素材库根目录', 'valid': True}
    assert controller.selectTarget('folder:P', controller.revision)
    assert controller.target['key'] == 'folder:P'


def test_create_unknown_result_is_visible_and_not_automatically_retried(env):
    controller, scheduler, queue, server = env(*(responses() + responses(created=True, post_error=True)))
    connect(controller, scheduler)
    assert controller.selectTarget('root', controller.revision)
    revision = controller.revision
    assert controller.createFolder('新目录', 'root', revision)
    scheduler.run_ready()
    assert wait(lambda: not controller.busy)
    assert controller.uncertain and not controller.ready and '刷新' in controller.statusText
    time.sleep(.08)
    APPLICATION.processEvents()
    assert len([r for r in server.requests if r.method == 'POST']) == 1
    assert not controller.createFolder('第二目录', 'root', revision)


def test_successful_creation_refreshes_folders_and_selection_stays_explicit(env):
    controller, scheduler, queue, server = env(*(responses() + responses(created=True)))
    connect(controller, scheduler)
    assert controller.selectTarget('folder:P', controller.revision)
    assert controller.createFolder('新目录', 'folder:P', controller.revision)
    scheduler.run_ready()
    assert wait(lambda: not controller.busy)
    assert controller.ready and controller.target['key'] == 'folder:P'
    assert controller.selectTarget('folder:NEW', controller.revision)
    assert controller.target['key'] == 'folder:NEW'


def test_queued_old_success_cannot_undo_user_cancel(env):
    controller, scheduler, queue, server = env(*responses())
    assert controller.refresh()
    scheduler.run_ready()
    deadline = time.monotonic() + 2
    while len(server.requests) < 5 and time.monotonic() < deadline:
        time.sleep(.01)
    assert len(server.requests) == 5 and controller.busy
    assert controller.cancel()
    assert wait(lambda: not controller.busy)
    assert not controller.ready and '取消' in controller.statusText


def test_queue_rejection_and_sync_close_start_zero_requests_and_keep_foreign_task(env):
    controller, scheduler, queue, server = env(*responses(), capacity=1)
    external = queue.submit(TaskKind.METADATA, lambda _attempt, _done: FakeAbortable(), lambda _result: None)
    assert not controller.refresh()
    assert '队列' in controller.statusText and not controller.busy and server.requests == []
    assert not external.terminal
    queue.cancel(external.identifier)
    controller.changed.connect(lambda: controller.shutdown() if controller.busy else None)
    assert not controller.refresh()
    scheduler.run_ready()
    assert server.requests == []


def test_unsupported_bridge_never_starts_a_thread_or_queue_operation():
    scheduler = ManualScheduler(ManualClock())
    queue = BoundedTaskQueue(TaskQueueConfig(), clock=scheduler.clock, scheduler=scheduler)
    bridge = UnavailableEagleBridge(ServiceAvailability.unavailable('unsupported', 'not supported'), False)
    runtime = EagleRuntime(create_eagle_backend_factory(bridge), bridge.availability, queue)
    controller = EagleController(runtime)
    assert not controller.refresh() and not controller.supported and '不支持' in controller.statusText
    assert not runtime.thread_started and not queue.has_work
    controller.shutdown()
    runtime.shutdown()


def test_shutdown_while_waiting_for_headers_does_not_need_gui_completed_slot(server):
    instance, url = server('headers')
    scheduler = ManualScheduler(ManualClock())
    queue = BoundedTaskQueue(TaskQueueConfig(), clock=scheduler.clock, scheduler=scheduler)
    bridge = EagleWebApiBridge(url.removesuffix('app/info'))
    runtime = EagleRuntime(create_eagle_backend_factory(bridge), bridge.availability, queue)
    controller = EagleController(runtime)
    try:
        assert controller.refresh()
        scheduler.run_ready()
        deadline = time.monotonic() + 2
        while not instance.requests and time.monotonic() < deadline:
            time.sleep(.01)
        assert instance.requests == 1
        started = time.monotonic()
        controller.shutdown()
        runtime.shutdown()  # GUI completion slots cannot run during wait.
        assert time.monotonic() - started < 1 and runtime.shutdown_completed
        assert not queue.is_shutdown
    finally:
        controller.shutdown()
        runtime.shutdown()
        queue.shutdown()


@pytest.mark.parametrize('notification', ['changed', 'foldersChanged'])
def test_old_start_and_cancel_do_not_clear_reentrant_new_request(env, notification):
    controller, scheduler, queue, server = env(*responses())
    replaced = False
    def replace_initial():
        nonlocal replaced
        if controller.busy and not replaced:
            replaced = True
            controller.cancel()
            assert controller.refresh()
    getattr(controller, notification).connect(replace_initial)
    assert not controller.refresh()
    current = controller.runtime.active_request
    assert current is not None
    scheduler.run_ready()
    assert wait(lambda: not controller.busy)
    assert controller.ready and len(server.requests) == 5


def test_target_change_invalidates_old_creation_and_sync_selection_return(env):
    controller, scheduler, queue, server = env(*responses())
    connect(controller, scheduler)
    assert controller.selectTarget('root', controller.revision)
    old_revision = controller.revision
    assert controller.selectTarget('folder:P', controller.revision)
    assert not controller.createFolder('旧动作', 'root', old_revision)
    assert not controller.createFolder('旧父目录', 'root', controller.revision)
    replaced = False
    def replace_selection():
        nonlocal replaced
        if not replaced:
            replaced = True
            controller.selectTarget('folder:P', controller.revision)
    controller.changed.connect(replace_selection)
    assert not controller.selectTarget('root', controller.revision)
    assert controller.target['key'] == 'folder:P'


@pytest.mark.parametrize('notification', ['changed', 'foldersChanged'])
def test_cancel_notification_can_start_new_queued_job_without_old_message_overwrite(env, notification):
    controller, scheduler, queue, server = env(*responses())
    assert controller.refresh()
    old = controller.runtime.active_request
    invoked = False
    def restart():
        nonlocal invoked
        if not controller.busy and not invoked:
            invoked = True
            assert controller.refresh()
    getattr(controller, notification).connect(restart)
    assert controller.cancel()
    assert controller.runtime.active_request is not None and controller.runtime.active_request is not old
    assert controller.statusText == '等待本机 Eagle 请求队列…'
    scheduler.run_ready()
    assert wait(lambda: not controller.busy)
    assert controller.ready and len(server.requests) == 5


def test_assembly_factory_preserves_configuration_and_owns_resources_in_worker(monkeypatch):
    from threading import Thread, get_ident
    from tests.test_eagle_client import RecordingTransport, transport_response
    import danbooru_multi_search.integrations.eagle.qt_transport as module
    original_transport = RecordingTransport()
    bridge = EagleWebApiBridge('http://127.0.0.1:41596/api/v2/', transport=original_transport,
        timeout_seconds=.7, maximum_response_bytes=1234, user_agent='configured agent')
    captured = []
    class Transport(RecordingTransport):
        def __init__(self, cancelled, deadline):
            super().__init__(transport_response(app_info()))
            self.sent_post = False
            self.created = get_ident()
            captured.append(self)
        def close(self):
            self.closed = get_ident()
    monkeypatch.setattr(module, 'QtEagleHttpTransport', Transport)
    factory = create_eagle_backend_factory(bridge)
    results = []
    def run():
        with factory(lambda: False, time.monotonic() + 15) as backend:
            results.append(backend.bridge.test_connection())
    worker = Thread(target=run)
    worker.start()
    worker.join(2)
    assert not worker.is_alive() and results[0].succeeded
    transport = captured[0]
    assert transport.created == transport.closed != get_ident()
    assert transport.requests[0].url == 'http://127.0.0.1:41596/api/v2/app/info'
    assert transport.options == [(.7, 1234)]
    assert transport.requests[0].headers['User-Agent'] == 'configured agent'
    assert original_transport.requests == []


def test_late_cancel_keeps_confirmed_created_identifier_without_publishing_target(env):
    controller, scheduler, queue, server = env(*(responses() + responses(created=True)))
    connect(controller, scheduler)
    assert controller.selectTarget('root', controller.revision)
    assert controller.createFolder('新目录', 'root', controller.revision)
    scheduler.run_ready()
    deadline = time.monotonic() + 2
    while len(server.requests) < 12 and time.monotonic() < deadline:
        time.sleep(.01)
    assert len(server.requests) == 12 and controller.busy
    assert controller.cancel()
    assert wait(lambda: not controller.busy)
    assert not controller.ready and not controller.target['valid']
    assert controller._result.post_started and controller._result.created_identifier == 'NEW'
    assert '创建' in controller.statusText and '刷新' in controller.statusText
    assert len([request for request in server.requests if request.method == 'POST']) == 1
