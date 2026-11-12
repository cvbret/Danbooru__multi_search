"""Exercise pure imports in a fresh process and preserve public lazy exports."""

from __future__ import annotations

import ast
import importlib
import importlib.abc
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGES = (
    "danbooru_multi_search.danbooru",
    "danbooru_multi_search.storage",
    "danbooru_multi_search.tasks",
)
PURE_MODULES = (
    "danbooru_multi_search.core.downloads",
    "danbooru_multi_search.storage.downloads",
    "danbooru_multi_search.storage.download_files",
    "danbooru_multi_search.core.favorites",
    "danbooru_multi_search.danbooru.models",
    "danbooru_multi_search.danbooru.post_validation",
    "danbooru_multi_search.danbooru.query_types",
    "danbooru_multi_search.danbooru.query_language",
    "danbooru_multi_search.danbooru.query_planning",
    "danbooru_multi_search.danbooru.query_definition",
    "danbooru_multi_search.danbooru.result_processing",
    "danbooru_multi_search.storage.database",
    "danbooru_multi_search.storage.history",
    "danbooru_multi_search.storage.query_serialization",
    "danbooru_multi_search.storage.favorites",
    "danbooru_multi_search.storage.favorite_codec",
    "danbooru_multi_search.storage.favorite_transfer",
    "danbooru_multi_search.storage.post_snapshot",
    "danbooru_multi_search.tasks.queue",
    "danbooru_multi_search.tasks.scheduling",
)


class _RejectQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "PySide6" or fullname.startswith("PySide6."):
            raise AssertionError(f"Pure imports attempted to load {fullname}")
        return None


def _probe_pure_imports(data_root: Path) -> None:
    sys.meta_path.insert(0, _RejectQt())
    for name in PACKAGES:
        package = importlib.import_module(name)
        assert set(package.__all__) <= set(dir(package))
        try:
            getattr(package, "unknown_public_name")
        except AttributeError:
            pass
        else:
            raise AssertionError(f"Unknown export accepted by {name}")
    for name in PURE_MODULES:
        importlib.import_module(name)

    from danbooru_multi_search.danbooru import DanbooruPost, QueryExecutionLimits
    from danbooru_multi_search.storage import (
        Database,
        FavoriteCandidate,
        FavoriteRepository,
        HistoryRepository,
        decode_query_definition,
        encode_query_definition,
    )
    from danbooru_multi_search.tasks import BoundedTaskQueue, TaskQueueConfig

    database = Database(data_root / "pure-imports.sqlite3")
    database.initialize()
    from danbooru_multi_search.core.downloads import DownloadRequest, DownloadState
    from danbooru_multi_search.storage.downloads import DownloadRepository
    from danbooru_multi_search.danbooru import DownloadState as PublicDownloadState
    assert PublicDownloadState is DownloadState
    downloads = DownloadRepository(database)
    batch = downloads.create_batch((DownloadRequest(321, "https://example.invalid/321.jpg", "jpg"),), data_root)
    downloads.recover_unfinished()
    assert downloads.get_batch(batch.batch_id).items[0].state.value == "awaiting_resume"
    history = HistoryRepository(database)
    query = history.record_accepted_query(
        display_text="cat",
        normalized_tags=("cat",),
        rating="general",
        page_size=40,
        page=1,
        count_tag_usage=False,
    )
    assert query is not None
    assert query.definition.limits == QueryExecutionLimits(page_size=40)
    assert decode_query_definition(encode_query_definition(query.definition)) == (
        query.definition
    )
    post = DanbooruPost(
        id=1,
        preview_url="https://example.invalid/preview.jpg",
        large_file_url=None,
        file_url=None,
        score=-1,
        fav_count=0,
        rating="g",
        width=100,
        height=100,
        file_ext="jpg",
        general_tags=("cat",),
    )
    favorites = FavoriteRepository(database)
    saved = favorites.add_favorite(FavoriteCandidate.for_post(post))
    assert favorites.get_favorite(saved.id).post == post
    assert BoundedTaskQueue is importlib.import_module(
        "danbooru_multi_search.tasks.queue"
    ).BoundedTaskQueue
    assert TaskQueueConfig().metadata_concurrency > 0
    assert not any(name.startswith("PySide6") for name in sys.modules)


def _probe_qt_scheduling() -> None:
    from PySide6.QtCore import QCoreApplication
    from danbooru_multi_search.tasks import QtScheduler
    from danbooru_multi_search.tasks import qt_scheduling, scheduling

    assert scheduling.QtScheduler is qt_scheduling.QtScheduler is QtScheduler
    assert scheduling.QtScheduledCall is qt_scheduling.QtScheduledCall
    application = QCoreApplication([])
    scheduler = QtScheduler(application)
    called = []
    completed = scheduler.call_later(0, lambda: called.append("completed"))
    cancelled = scheduler.call_later(0, lambda: called.append("cancelled"))
    cancelled.cancel()
    application.processEvents()
    assert called == ["completed"]
    assert completed.active is False
    assert cancelled.active is False


def _probe_query_forms() -> None:
    blocked = (
        "danbooru_multi_search.ui.query_state",
        "danbooru_multi_search.danbooru.query_controller",
        "danbooru_multi_search.danbooru.query_execution",
    )

    class RejectBridge(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname in blocked:
                raise AssertionError(f"query forms imported {fullname}")
            return None

    sys.meta_path.insert(0, RejectBridge())
    from PySide6.QtCore import QTimeZone
    from danbooru_multi_search.danbooru.query_definition import QueryMode
    from danbooru_multi_search.ui.query_forms import (
        build_draft_definition,
        default_query_form_values,
        parse_query_form,
        query_form_values_from_definition,
    )

    values = default_query_form_values()
    zone = QTimeZone(b"UTC")
    form = parse_query_form(values, mode=QueryMode.STANDARD, time_zone=zone)
    definition = build_draft_definition(
        form, mode=QueryMode.STANDARD, normalized_expression="cat dog"
    )
    assert definition.normalized_expression == "cat AND dog"
    assert query_form_values_from_definition(definition, time_zone=zone) == values
    assert not any(name in sys.modules for name in blocked)


def _run_probe(mode: str, data_root: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), mode, str(data_root)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_pure_modules_and_storage_work_with_qt_imports_blocked(tmp_path):
    _run_probe("pure", tmp_path)


def test_query_forms_work_without_the_bridge_or_executor(tmp_path):
    _run_probe("forms", tmp_path)


def test_legacy_qt_scheduler_imports_and_timer_cancellation(tmp_path):
    _run_probe("qt", tmp_path)


def test_package_exports_are_discoverable_cached_and_keep_owner_identity():
    import pytest

    for module_name in PACKAGES:
        package = importlib.import_module(module_name)
        assert set(package.__all__) <= set(dir(package))
        for name in package.__all__:
            owner = importlib.import_module(package._EXPORTS[name])
            value = getattr(package, name)
            assert value is getattr(owner, name)
            assert package.__dict__[name] is value
        with pytest.raises(AttributeError, match="unknown_public_name"):
            getattr(package, "unknown_public_name")


def test_windows_bundle_includes_every_lazy_export_target():
    spec_path = PROJECT_ROOT / "packaging" / "windows" / "DanbooruMultiSearch.spec"
    tree = ast.parse(spec_path.read_text(encoding="utf-8"))
    hidden_imports = next(
        set(ast.literal_eval(node.value))
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "hidden_imports"
            for target in node.targets
        )
    )
    for module_name in PACKAGES:
        package = importlib.import_module(module_name)
        assert set(package._EXPORTS.values()) <= hidden_imports


if __name__ == "__main__":
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    if sys.argv[1] == "pure":
        _probe_pure_imports(Path(sys.argv[2]))
    elif sys.argv[1] == "qt":
        _probe_qt_scheduling()
    elif sys.argv[1] == "forms":
        _probe_query_forms()
    else:
        raise ValueError("Unknown probe mode")
