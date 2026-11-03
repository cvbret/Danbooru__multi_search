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
from danbooru_multi_search.danbooru import DEFAULT_TIMEOUT_MS, run_danbooru_smoke
from danbooru_multi_search.platform import PlatformAdapter
from danbooru_multi_search.ui.launcher import launch_gui


ORGANIZATION_NAME = "DanbooruMultiSearch"
APPLICATION_NAME = "DanbooruMultiSearch"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="danbooru-multi-search")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--headless-check",
        action="store_true",
        help="Initialize settings and database, print a JSON result, then exit.",
    )
    mode.add_argument(
        "--danbooru-smoke",
        action="store_true",
        help="Request one Danbooru post through QtNetwork and print JSON.",
    )
    mode.add_argument(
        "--wallpaper-probe",
        action="store_true",
        help="Read the current desktop wallpaper and print JSON without changing it.",
    )
    parser.add_argument(
        "--danbooru-tags",
        default="rating:g",
        help="Tags used by --danbooru-smoke (default: rating:g).",
    )
    parser.add_argument(
        "--danbooru-timeout-ms",
        type=int,
        default=DEFAULT_TIMEOUT_MS,
        help=f"Network timeout for --danbooru-smoke (default: {DEFAULT_TIMEOUT_MS}).",
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


def wallpaper_probe_result(platform: PlatformAdapter) -> tuple[dict[str, object], int]:
    """Build the stable JSON result for the read-only wallpaper probe."""

    result = platform.services.wallpaper.get_current()
    if not result.succeeded or result.value is None:
        return (
            {
                "status": "error",
                "platform": platform.capabilities.os_name,
                "code": result.code or "wallpaper_read_failed",
                "message": result.message or "Unable to read the current wallpaper.",
            },
            2,
        )

    snapshot = result.value
    return (
        {
            "status": "ok",
            "platform": platform.capabilities.os_name,
            "wallpaper": {
                "path": str(snapshot.path) if snapshot.path is not None else None,
                "style": snapshot.style.value if snapshot.style is not None else None,
                "wallpaper_style_value": snapshot.wallpaper_style_value,
                "tile_wallpaper_value": snapshot.tile_wallpaper_value,
            },
        },
        0,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the desktop application or its headless initialization check."""

    args = _build_parser().parse_args(argv)
    _configure_application_identity()

    headless_mode = (
        args.headless_check or args.danbooru_smoke or args.wallpaper_probe
    )
    application_type = QCoreApplication if headless_mode else QGuiApplication
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

    if args.danbooru_smoke:
        try:
            result = run_danbooru_smoke(
                runtime.settings.danbooru_api_url,
                args.danbooru_tags,
                timeout_ms=args.danbooru_timeout_ms,
            )
        except Exception as exc:
            payload = {
                "status": "error",
                "error_kind": "internal",
                "message": f"Danbooru smoke check failed: {exc}",
                "tags": args.danbooru_tags.strip(),
            }
            print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
            return 1
        print(json.dumps(result.payload, ensure_ascii=True, sort_keys=True))
        return result.exit_code

    if args.wallpaper_probe:
        payload, exit_code = wallpaper_probe_result(runtime.platform)
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return exit_code

    return launch_gui(
        application,
        danbooru_base_url=runtime.settings.danbooru_api_url,
    )


if __name__ == "__main__":
    raise SystemExit(main())

