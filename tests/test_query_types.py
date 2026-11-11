"""Keep shared query data independent from runtime controllers and transports."""

import ast
import importlib
from pathlib import Path

import pytest


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "danbooru_multi_search"


@pytest.mark.parametrize(
    "relative_path",
    [
        "danbooru/query_types.py",
        "danbooru/query_definition.py",
        "danbooru/result_processing.py",
        "storage/history.py",
        "storage/query_serialization.py",
    ],
)
def test_shared_query_consumers_do_not_import_runtime_modules(relative_path):
    source = (SOURCE_ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules = (node.module or "",)
        elif isinstance(node, ast.Import):
            modules = tuple(alias.name for alias in node.names)
        else:
            continue
        for module in modules:
            assert not module.startswith("PySide6"), (relative_path, module)
            assert module not in {
                "danbooru_multi_search.danbooru.query_controller",
                "danbooru_multi_search.danbooru.query_execution",
            }, (relative_path, module)


def test_execution_legacy_exports_refer_to_shared_types():
    shared = importlib.import_module("danbooru_multi_search.danbooru.query_types")
    executor = importlib.import_module(
        "danbooru_multi_search.danbooru.query_execution"
    )
    package = importlib.import_module("danbooru_multi_search.danbooru")
    for name in (
        "ACTIVE_EXECUTION_STATES",
        "DEFAULT_ACTIVE_TIME_LIMIT_SECONDS",
        "DEFAULT_BRANCH_PAGE_LIMIT",
        "DEFAULT_CANDIDATE_LIMIT",
        "DEFAULT_EXECUTION_PAGE_SIZE",
        "QueryBranchPosition",
        "QueryBranchProgress",
        "QueryExecutionAccepted",
        "QueryExecutionBound",
        "QueryExecutionCounts",
        "QueryExecutionErrorCode",
        "QueryExecutionFailure",
        "QueryExecutionLimits",
        "QueryExecutionSnapshot",
        "QueryExecutionState",
        "QueryLimitReason",
    ):
        assert getattr(executor, name) is getattr(shared, name)
        assert getattr(package, name) is getattr(shared, name)


def test_result_summary_legacy_and_storage_exports_refer_to_shared_type():
    shared = importlib.import_module("danbooru_multi_search.danbooru.query_types")
    for module_name in (
        "danbooru_multi_search.danbooru",
        "danbooru_multi_search.danbooru.query_controller",
        "danbooru_multi_search.storage.history",
        "danbooru_multi_search.storage.query_serialization",
    ):
        module = importlib.import_module(module_name)
        assert module.GalleryResultSummary is shared.GalleryResultSummary
