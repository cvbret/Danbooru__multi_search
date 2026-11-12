"""Helpers that exercise the formal M2 assembly without external requests."""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent

from danbooru_multi_search.core.bootstrap import initialize_runtime
from danbooru_multi_search.ui.launcher import create_qml_engine
from danbooru_multi_search.ui.window_state import ScreenGeometry, WindowGeometry


class RejectingNetworkManager:
    def get(self, request):
        raise AssertionError(f"unexpected network request: {request.url().toString()}")


def create_m2_qml_test_engine(application, *, screen_geometries=None, **kwargs):
    """Use the production graph with isolated data and an explicit test manager."""

    temporary = None
    if kwargs.get("runtime") is None:
        temporary_root = Path(__file__).resolve().parents[1] / ".tools"
        temporary_root.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="m2-qml-", dir=temporary_root)
        kwargs["runtime"] = initialize_runtime(Path(temporary.name))
    if kwargs.get("network_manager") is None:
        kwargs["network_manager"] = RejectingNetworkManager()
    if screen_geometries is None:
        screen_geometries = (
            ScreenGeometry(WindowGeometry(0, 0, 1280, 820), primary=True),
        )
    engine, bridge = create_qml_engine(
        application, screen_geometries=screen_geometries, **kwargs
    )
    engine._test_temporary_root = temporary
    assert engine.rootObjects(), "formal M2 QML did not load"
    return engine, bridge


def close_m2_qml_test_engine(engine):
    """Run the production shutdown, then drain deferred QML destruction."""

    roots = engine.rootObjects()
    bridge = engine.rootContext().contextProperty("searchController")
    bridge.parent().shutdown()
    for root in roots:
        root.close()
    temporary = engine._test_temporary_root
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.processEvents()
    if temporary is not None:
        temporary.cleanup()
