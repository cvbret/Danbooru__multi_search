"""Verify QML resources from an installed wheel without using the source tree."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QML_DISABLE_DISK_CACHE", "1")

from PySide6.QtGui import QGuiApplication

import danbooru_multi_search
from danbooru_multi_search.ui.launcher import create_qml_engine, main_qml_path


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-root",
        type=Path,
        required=True,
        help="Source package directory that must not provide the imported package.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    package_file = Path(danbooru_multi_search.__file__).resolve()
    environment_root = Path(sys.prefix).resolve()
    source_root = args.source_root.resolve()

    if not _is_relative_to(package_file, environment_root):
        raise RuntimeError(
            f"Package was not imported from the isolated environment: {package_file}"
        )
    if _is_relative_to(package_file, source_root):
        raise RuntimeError(f"Package was imported from the source tree: {package_file}")

    qml_file = main_qml_path().resolve()
    if not qml_file.is_file():
        raise RuntimeError(f"Installed QML resource is missing: {qml_file}")
    if not _is_relative_to(qml_file, environment_root):
        raise RuntimeError(
            f"QML resource was not loaded from the isolated environment: {qml_file}"
        )

    application = QGuiApplication.instance() or QGuiApplication([])
    engine, controller = create_qml_engine(
        application,
        danbooru_base_url="https://example.invalid",
    )
    try:
        if not engine.rootObjects():
            raise RuntimeError("Installed QML resource did not create a root object.")
    finally:
        controller.shutdown()

    print(
        json.dumps(
            {
                "installed_package": True,
                "qml_exists": True,
                "qml_loaded": True,
                "status": "ok",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
