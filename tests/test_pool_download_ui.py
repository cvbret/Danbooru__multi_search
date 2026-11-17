from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QUrl, Signal

from danbooru_multi_search.core.downloads import DownloadError
from danbooru_multi_search.core.settings import AppSettings, SettingsController, SettingsError, SettingsStore
from danbooru_multi_search.ui.pool_downloads import PoolDownloadController
from danbooru_multi_search.app.download_submission import DownloadSubmissionService
from tests.test_pool_download_manager import harness


class Source(QObject):
    changed = Signal()

    def __init__(self, pool):
        super().__init__()
        self.current_pool = pool
        self.generation = self.readingSession = 1
        self.active = True


@pytest.fixture
def env(tmp_path):
    value = harness(tmp_path, 81, immediate=False)
    value.browser, value.reader = Source(value.value), Source(value.value)
    store = SettingsStore(tmp_path / 'settings.json')
    initial = AppSettings(download_directory=str(tmp_path))
    store.save(initial)
    value.settings = SettingsController(store, initial)
    value.ui = PoolDownloadController(value.manager, value.settings,
        SimpleNamespace(defaultDirectory=str(tmp_path)), value.browser, value.reader,
        submission=DownloadSubmissionService(value.settings))
    value.ui.setSourceVisible('browser', True)
    yield value
    value.ui.shutdown()
    value.manager.begin_shutdown()
    value.manager.finish_shutdown()


def test_capture_and_directory_rejection_are_zero_requests_zero_plans(env, tmp_path):
    ui = env.ui
    token = ui.capturePool('browser', '1')
    assert token and ui.pending['count'] == 81
    choice = ui.beginDirectorySelection('request')
    ui.cancelDirectorySelection(choice)
    assert not ui.acceptDirectory(QUrl.fromLocalFile(str(tmp_path)), choice)
    ui.cancelRequest(token)
    env.scheduler.run_ready()
    assert not env.pages.requests and env.repo.summaries() == ()


@pytest.mark.parametrize('stage', ['directory', 'settings', 'busy'])
def test_source_change_before_confirmation_never_persists_old_pool(env, tmp_path, monkeypatch, stage):
    ui = env.ui
    token = ui.capturePool('browser', '1')
    if stage == 'directory':
        choice = ui.beginDirectorySelection('request')
        ui.setSourceVisible('browser', False)
        assert not ui.acceptDirectory(QUrl.fromLocalFile(str(tmp_path)), choice)
    elif stage == 'settings':
        original = env.settings.set_download_directory
        def save(path):
            original(path)
            env.browser.generation += 1
        monkeypatch.setattr(env.settings, 'set_download_directory', save)
    else:
        ui.changed.connect(lambda: ui.shutdown() if ui.busy else None)
    assert not ui.confirmRequest(token, 'skip', stage == 'settings')
    assert not env.pages.requests and env.repo.summaries() == ()


def test_default_save_failure_keeps_confirmed_settings_and_creates_no_plan(env, monkeypatch):
    previous = env.settings.settings
    def fail(path):
        raise SettingsError('无法保存设置')
    monkeypatch.setattr(env.settings, 'set_download_directory', fail)
    token = env.ui.capturePool('browser', '1')
    assert not env.ui.confirmRequest(token, 'auto_number', True)
    assert '无法保存设置' in env.ui.errorText
    assert env.settings.settings is previous and env.repo.summaries() == ()
    assert env.ui.openRequest(token)


def test_confirmation_snapshots_pool_and_survives_later_navigation(env):
    ui = env.ui
    token = ui.capturePool('browser', '1')
    assert ui.confirmRequest(token, 'auto_number', False)
    env.browser.current_pool = None
    env.browser.generation += 1
    env.browser.changed.emit()
    env.scheduler.run_ready()
    assert len(env.pages.requests) == 1 and len(env.pages.requests[0].references) == 40
    assert env.repo.summaries()[0].plan.policy.value == 'auto_number'


def test_hidden_and_reselected_panel_rejects_old_actions_but_normal_progress_does_not(env):
    ui = env.ui
    token = ui.capturePool('browser', '1')
    assert ui.confirmRequest(token, 'skip', False)
    key = env.manager.active.plan_id
    ui.setActive(True)
    assert ui.selectPlan(key)
    action = ui.captureAction(key, ui.selected['generation'], 'stop')
    env.manager.changed.emit(key)
    assert ui.runAction(action)
    action = ui.captureAction(key, ui.selected['generation'], 'resume')
    ui.setActive(False)
    assert not ui.runAction(action)
    ui.setActive(True)
    assert not ui.runAction(action)


def test_read_failure_and_failed_pagination_keep_visible_rows_and_offsets(env, monkeypatch):
    ui = env.ui
    token = ui.capturePool('browser', '1')
    assert ui.confirmRequest(token, 'skip', False)
    key = env.manager.active.plan_id
    ui.setActive(True)
    ui.selectPlan(key)
    before = ui.pagesModel.items
    def fail(*args, **kwargs):
        raise DownloadError('storage_error', '读取失败')
    monkeypatch.setattr(env.repo, 'pages', fail)
    ui.turnPages(1)
    assert ui.selected['firstPage'] == 1 and ui.pagesModel.items == before and '读取失败' in ui.errorText
    monkeypatch.setattr(env.repo, 'get', fail)
    assert ui.captureAction(key, ui.selected['generation'], 'stop') == ''
    assert '读取失败' in ui.errorText


def test_model_notification_shutdown_does_not_publish_remaining_old_selection(env):
    ui = env.ui
    token = ui.capturePool('browser', '1')
    assert ui.confirmRequest(token, 'skip', False)
    ui.plansModel.changed.connect(ui.shutdown)
    ui.setActive(True)
    assert ui._closed and not ui._selected and ui.pagesModel.count == 0
