from dataclasses import replace
from pathlib import Path

import pytest

from danbooru_multi_search.integrations.eagle.contracts import EagleConnectionInfo, EagleFolderInfo, EagleLibraryInfo
from danbooru_multi_search.app.eagle_service import EagleBackend, EagleRequest, EagleOperation, execute_eagle_request
from danbooru_multi_search.platform.contracts import ValueResult


class Bridge:
    def __init__(self):
        self.library = EagleLibraryInfo('A', '素材库', Path('A.library'))
        self.folder = EagleFolderInfo('P', '父文件夹')
        self.folders = (self.folder,)
        self.calls = []
        self.sent = False
        self.cancelled = False
        self.change_after_post = False
        self.post_error = False

    def test_connection(self):
        self.calls.append('app')
        return ValueResult(True, EagleConnectionInfo('4.0.0'))

    def get_current_library(self):
        self.calls.append('library')
        return ValueResult(True, self.library)

    def list_folders(self, **limits):
        assert limits['maximum_pages'] == 20 and limits['maximum_folders'] == 20000
        self.calls.append('folders')
        return ValueResult(True, self.folders)

    def create_folder(self, name, *, parent_identifier=None):
        self.calls.append('POST')
        self.sent = True
        if self.change_after_post:
            self.library = replace(self.library, identifier='B')
        if self.post_error:
            return ValueResult.unavailable('eagle_timeout', 'timeout')
        created = EagleFolderInfo('NEW', name, parent_identifier)
        self.folders += (created,)
        return ValueResult(True, created)


def execute(bridge, operation=EagleOperation.CREATE_FOLDER, **kwargs):
    request = EagleRequest('request', operation, 'A', '新文件夹', kwargs.get('parent', 'P'))
    return execute_eagle_request(request, EagleBackend(bridge, lambda: bridge.sent), lambda: bridge.cancelled)


def test_refresh_is_one_complete_library_snapshot_without_mutation():
    bridge = Bridge()
    result = execute(bridge, EagleOperation.REFRESH)
    assert result.snapshot.library.identifier == 'A' and result.snapshot.folders == bridge.folders
    assert 'POST' not in bridge.calls and not result.uncertain


def test_create_verifies_parent_and_library_and_refreshes_created_folder():
    bridge = Bridge()
    result = execute(bridge)
    assert result.created_identifier == 'NEW' and result.snapshot.folders[-1].parent_identifier == 'P'
    assert bridge.calls.count('POST') == 1


@pytest.mark.parametrize('failure', ['library', 'parent', 'cancel'])
def test_preflight_failures_send_no_post(failure):
    bridge = Bridge()
    if failure == 'library':
        bridge.library = replace(bridge.library, identifier='B')
    elif failure == 'parent':
        bridge.folders = ()
    else:
        bridge.cancelled = True
    result = execute(bridge)
    assert result.error_code and not bridge.sent and not result.uncertain


@pytest.mark.parametrize('failure', ['switch', 'timeout', 'cancel'])
def test_post_started_failure_is_uncertain_and_never_repeated(failure):
    bridge = Bridge()
    bridge.change_after_post = failure == 'switch'
    bridge.post_error = failure == 'timeout'
    original = bridge.create_folder
    def create(*args, **kwargs):
        result = original(*args, **kwargs)
        bridge.cancelled = failure == 'cancel'
        return result
    bridge.create_folder = create
    result = execute(bridge)
    assert result.uncertain and result.post_started and result.diagnostic and result.snapshot is None
    assert bridge.calls.count('POST') == 1
