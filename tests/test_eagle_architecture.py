import ast
from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = PROJECT_ROOT / 'src' / 'danbooru_multi_search'


def test_service_imports_and_executes_without_qt_or_ui(tmp_path):
    code = '''
import importlib.abc
import json
import sys
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

class Reject(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("PySide6", "danbooru_multi_search.ui", "danbooru_multi_search.platform")):
            raise AssertionError(fullname)
sys.meta_path.insert(0, Reject())
from danbooru_multi_search.core.results import ServiceAvailability, OperationResult, ValueResult
from danbooru_multi_search.app.eagle_service import EagleRequest, EagleOperation, EagleBackend, execute_eagle_request
from danbooru_multi_search.integrations.eagle.contracts import EagleBridge
from danbooru_multi_search.integrations.eagle.client import EagleWebApiBridge
from danbooru_multi_search.integrations.eagle.stubs import UnavailableEagleBridge
from danbooru_multi_search.integrations.eagle.transport import UrllibEagleHttpTransport

library_path = Path(sys.argv[1]) / "Example.library"
class Response(BytesIO):
    def __init__(self, data):
        body = json.dumps({"status": "success", "data": data}).encode("utf-8")
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body))}
    def getcode(self):
        return 200

class Opener:
    def __init__(self):
        self.requests = []
    def open(self, request, timeout):
        assert request.get_method() == "GET" and timeout > 0
        self.requests.append(request.full_url)
        path = urlsplit(request.full_url).path
        if path.endswith("/app/info"):
            return Response({
                "version": "4.0.0", "prereleaseVersion": None,
                "buildVersion": "build21", "platform": "win32",
            })
        if path.endswith("/library/info"):
            return Response({"name": "Library", "path": str(library_path)})
        assert path.endswith("/folder/get")
        return Response({"data": [], "total": 0, "offset": 0, "limit": 1000})

opener = Opener()
bridge = EagleWebApiBridge(transport=UrllibEagleHttpTransport(opener=opener))
assert isinstance(bridge.test_connection(), ValueResult)
assert isinstance(bridge.availability, ServiceAvailability)
result = execute_eagle_request(
    EagleRequest("request", EagleOperation.REFRESH),
    EagleBackend(bridge, lambda: False), lambda: False,
)
assert result.snapshot is not None, (result.error_code, result.diagnostic)
assert result.snapshot.library.name == "Library"
assert result.snapshot.library.path == library_path
assert result.snapshot.folders == () and len(opener.requests) == 6, opener.requests
assert not library_path.exists()
stub = UnavailableEagleBridge(ServiceAvailability.unavailable("disabled", "reason"), False)
assert isinstance(stub.test_connection(), ValueResult)
assert stub.test_connection().code == "disabled"
assert OperationResult.unavailable("disabled", "reason").succeeded is False
assert not any(name.startswith(("PySide6", "danbooru_multi_search.ui", "danbooru_multi_search.platform")) for name in sys.modules)
'''
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


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
