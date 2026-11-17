import ast
from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = PROJECT_ROOT / 'src' / 'danbooru_multi_search'


def test_service_imports_and_executes_without_qt_or_ui():
    code = '''
import importlib.abc, sys
class Reject(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('PySide6', 'danbooru_multi_search.ui')):
            raise AssertionError(fullname)
sys.meta_path.insert(0, Reject())
from pathlib import Path
from types import SimpleNamespace as NS
from danbooru_multi_search.app.eagle_service import EagleRequest, EagleOperation, EagleBackend, execute_eagle_request
from danbooru_multi_search.integrations.eagle.contracts import EagleConnectionInfo, EagleLibraryInfo
def success(value): return NS(succeeded=True, value=value)
library = EagleLibraryInfo('A', 'Library', Path('unused.library'))
bridge = NS(test_connection=lambda: success(EagleConnectionInfo('4.0.0')),
            get_current_library=lambda: success(library), list_folders=lambda **kw: success(()))
result = execute_eagle_request(EagleRequest('request', EagleOperation.REFRESH), EagleBackend(bridge, lambda: False), lambda: False)
assert result.snapshot.library is library
assert not any(name.startswith(('PySide6', 'danbooru_multi_search.ui')) for name in sys.modules)
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=PROJECT_ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_ui_has_no_backend_factory_thread_or_queue_implementation():
    source = (PACKAGE / 'ui' / 'eagle.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    forbidden = {'QThread', 'QNetworkAccessManager', 'QtEagleHttpTransport', 'EagleWebApiBridge', 'Event', 'BoundedTaskQueue'}
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert names.isdisjoint(forbidden)
    assert 'with_transport' not in (PACKAGE / 'integrations' / 'eagle' / 'contracts.py').read_text(encoding='utf-8')
    assert not (PACKAGE / 'integrations' / 'eagle' / 'jobs.py').exists()


def test_public_eagle_exports_keep_identity_caching_and_unknown_name_behavior():
    import importlib
    import pytest
    package = importlib.import_module('danbooru_multi_search.integrations.eagle')
    assert set(package.__all__).issubset(dir(package))
    for name in package.__all__:
        module = importlib.import_module(package._EXPORTS[name])
        assert getattr(package, name) is getattr(module, name)
        assert getattr(package, name) is package.__dict__[name]
    with pytest.raises(AttributeError):
        getattr(package, 'missing_eagle_export')
