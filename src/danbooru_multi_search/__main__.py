"""Application command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QGuiApplication

from danbooru_multi_search import __version__
from danbooru_multi_search.core.bootstrap import initialize_runtime
from danbooru_multi_search.ui.launcher import launch_gui


ORGANIZATION_NAME = "DanbooruMultiSearch"
APPLICATION_NAME = "DanbooruMultiSearch"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="danbooru-multi-search")
    parser.add_argument(
        "--headless-check",
        action="store_true",
        help="Initialize settings and database, print a JSON result, then exit.",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        help="Override application directories for development and automated checks.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def _configure_application_identity() -> None:
    QCoreApplication.setOrganizationName(ORGANIZATION_NAME)
    QCoreApplication.setApplicationName(APPLICATION_NAME)
    QCoreApplication.setApplicationVersion(__version__)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the desktop application or its headless initialization check."""

    args = _build_parser().parse_args(argv)
    _configure_application_identity()

    application_type = QCoreApplication if args.headless_check else QGuiApplication
    application = application_type([APPLICATION_NAME])

    try:
        runtime = initialize_runtime(data_root=args.data_root)
    except Exception as exc:  # startup failures must be visible to CLI and GUI launches
        print(f"Application initialization failed: {exc}", file=sys.stderr)
        return 1

    if args.headless_check:
        result = {
            "status": "ok",
            "platform": runtime.platform.capabilities.os_name,
            "schema_version": runtime.database_version,
            "settings_created": runtime.settings_created,
            "data_directory": str(runtime.paths.data_dir),
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    return launch_gui(application)


if __name__ == "__main__":
    raise SystemExit(main())

