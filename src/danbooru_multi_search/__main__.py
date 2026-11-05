"""Application command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Sequence, TextIO

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QGuiApplication

from danbooru_multi_search import __version__
from danbooru_multi_search.core.bootstrap import initialize_runtime
from danbooru_multi_search.core.check_output import (
    CheckOutputError,
    write_check_output,
)
from danbooru_multi_search.danbooru import (
    DEFAULT_TIMEOUT_MS,
    run_danbooru_download_smoke,
    run_danbooru_smoke,
)
from danbooru_multi_search.integrations.eagle import EagleImportResult
from danbooru_multi_search.platform import PlatformAdapter
from danbooru_multi_search.ui.launcher import launch_gui, run_gui_smoke_check

if TYPE_CHECKING:
    from danbooru_multi_search.core.bootstrap import RuntimeContext


ORGANIZATION_NAME = "DanbooruMultiSearch"
APPLICATION_NAME = "DanbooruMultiSearch"


class _SafeArgumentParser(argparse.ArgumentParser):
    """Keep argparse usable when a windowed executable has no console streams."""

    def _print_message(self, message: str, file: TextIO | None = None) -> None:
        if not message or file is None:
            return
        try:
            file.write(message)
        except (AttributeError, OSError, ValueError):
            return


def _build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="danbooru-multi-search")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--headless-check",
        action="store_true",
        help="Initialize settings and database, print a JSON result, then exit.",
    )
    mode.add_argument(
        "--gui-smoke-check",
        action="store_true",
        help="Load the real QML shell, verify packaged capabilities, and exit.",
    )
    mode.add_argument(
        "--danbooru-smoke",
        action="store_true",
        help="Request one Danbooru post through QtNetwork and print JSON.",
    )
    mode.add_argument(
        "--danbooru-download-smoke",
        action="store_true",
        help="Request one Danbooru post, download its media, and print JSON.",
    )
    mode.add_argument(
        "--wallpaper-probe",
        action="store_true",
        help="Read the current desktop wallpaper and print JSON without changing it.",
    )
    mode.add_argument(
        "--eagle-probe",
        action="store_true",
        help="Read Eagle application and current-library information without writing.",
    )
    mode.add_argument(
        "--eagle-import-smoke",
        type=Path,
        metavar="FILE",
        help="Explicitly import one local file into the current Eagle library.",
    )
    parser.add_argument(
        "--check-output",
        type=Path,
        help=(
            "Write headless or GUI check JSON to a new result file instead "
            "of using the console."
        ),
    )
    parser.add_argument(
        "--danbooru-tags",
        default="rating:g",
        help=(
            "Tags used by --danbooru-smoke and --danbooru-download-smoke "
            "(default: rating:g)."
        ),
    )
    parser.add_argument(
        "--danbooru-timeout-ms",
        type=int,
        default=DEFAULT_TIMEOUT_MS,
        help=(
            "Network timeout for Danbooru smoke checks "
            f"(default: {DEFAULT_TIMEOUT_MS})."
        ),
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        help="Override application directories for development and automated checks.",
    )
    parser.add_argument(
        "--eagle-folder",
        help="Target folder identifier used by --eagle-import-smoke.",
    )
    parser.add_argument(
        "--eagle-name",
        help="Optional item name used by --eagle-import-smoke.",
    )
    parser.add_argument(
        "--eagle-website",
        help="Optional source website used by --eagle-import-smoke.",
    )
    parser.add_argument(
        "--eagle-tag",
        action="append",
        help="Optional item tag; repeat this option for multiple tags.",
    )
    parser.add_argument(
        "--eagle-annotation",
        help="Optional annotation used by --eagle-import-smoke.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def _configure_application_identity() -> None:
    QCoreApplication.setOrganizationName(ORGANIZATION_NAME)
    QCoreApplication.setApplicationName(APPLICATION_NAME)
    QCoreApplication.setApplicationVersion(__version__)


def _safe_write_line(stream: TextIO | None, text: str) -> None:
    if stream is None:
        return
    try:
        stream.write(text + "\n")
        stream.flush()
    except (AttributeError, OSError, ValueError):
        return


def _safe_write_json(payload: dict[str, object]) -> None:
    _safe_write_line(
        sys.stdout,
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


def _validate_check_arguments(
    args: argparse.Namespace,
) -> str | None:
    check_mode = args.headless_check or args.gui_smoke_check
    if args.check_output is not None and not check_mode:
        return "--check-output is only valid with a headless or GUI smoke check."
    if args.gui_smoke_check and args.data_root is None:
        return "--gui-smoke-check requires an explicit --data-root."
    if args.gui_smoke_check and args.check_output is None:
        return "--gui-smoke-check requires an explicit --check-output."
    output_path = args.check_output
    if output_path is not None and args.data_root is None:
        return "File-based checks require an explicit --data-root."
    if output_path is not None:
        if not output_path.parent.is_dir():
            return "The check output parent directory does not exist."
        if os.path.lexists(output_path):
            return "The check output path must not already exist."
    return None


def _emit_check_result(
    payload: dict[str, object],
    exit_code: int,
    output_path: Path | None,
) -> int:
    if output_path is None:
        _safe_write_json(payload)
        return exit_code
    try:
        write_check_output(output_path, payload)
    except CheckOutputError as exc:
        _safe_write_line(sys.stderr, f"Check result write failed: {exc}")
        return 1
    return exit_code


def _headless_result(runtime: RuntimeContext) -> dict[str, object]:
    return {
        "status": "ok",
        "mode": "headless",
        "version": __version__,
        "platform_name": runtime.platform.capabilities.os_name,
        "schema_version": runtime.database_version,
        "settings_created": runtime.settings_created,
        "data_directory": str(runtime.paths.data_dir),
    }


def _failed_headless_result(data_root: Path) -> dict[str, object]:
    return {
        "status": "error",
        "mode": "headless",
        "version": __version__,
        "platform_name": "windows" if os.name == "nt" else "linux",
        "schema_version": 0,
        "settings_created": False,
        "data_directory": str(data_root / "data"),
    }


def _failed_gui_result(data_root: Path) -> dict[str, object]:
    return {
        "status": "error",
        "mode": "gui",
        "version": __version__,
        "schema_version": 0,
        "qml_loaded": False,
        "shutdown_completed": False,
        "platform_name": QGuiApplication.platformName().casefold(),
        "tls_backends": [],
        "supported_image_formats": [],
        "data_directory": str(data_root / "data"),
    }


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


def _eagle_error_payload(
    platform: PlatformAdapter,
    *,
    operation: str,
    code: str | None,
    message: str | None,
) -> tuple[dict[str, object], int]:
    return (
        {
            "status": "error",
            "platform": platform.capabilities.os_name,
            "operation": operation,
            "code": code or "eagle_error",
            "message": message or "Eagle operation failed.",
        },
        2,
    )


def eagle_probe_result(platform: PlatformAdapter) -> tuple[dict[str, object], int]:
    """Read only Eagle application and current-library information."""

    connection = platform.services.eagle.test_connection()
    if not connection.succeeded or connection.value is None:
        return _eagle_error_payload(
            platform,
            operation="connection",
            code=connection.code,
            message=connection.message,
        )

    library = platform.services.eagle.get_current_library()
    if not library.succeeded or library.value is None:
        return _eagle_error_payload(
            platform,
            operation="library",
            code=library.code,
            message=library.message,
        )

    connection_value = connection.value
    library_value = library.value
    return (
        {
            "status": "ok",
            "platform": platform.capabilities.os_name,
            "eagle": {
                "version": connection_value.version,
                "prerelease_version": connection_value.prerelease_version,
                "build_version": connection_value.build_version,
                "build_number": connection_value.build_number,
                "platform": connection_value.platform,
            },
            "library": {
                "identifier": library_value.identifier,
                "name": library_value.name,
                "path": str(library_value.path),
            },
        },
        0,
    )


def eagle_import_smoke_result(
    platform: PlatformAdapter,
    file_path: Path,
    *,
    folder_identifier: str | None = None,
    metadata: dict[str, object] | None = None,
) -> tuple[dict[str, object], int]:
    """Perform the explicit, write-capable Eagle import smoke operation."""

    result = platform.services.eagle.import_file(
        file_path,
        folder_identifier=folder_identifier,
        metadata=metadata,
    )
    if not result.succeeded or result.value is None:
        return _eagle_error_payload(
            platform,
            operation="import",
            code=result.code,
            message=result.message,
        )
    import_result: EagleImportResult = result.value
    return (
        {
            "status": "ok",
            "platform": platform.capabilities.os_name,
            "file": str(file_path.resolve()),
            "item_identifier": import_result.item_identifier,
        },
        0,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the desktop application or its headless initialization check."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    argument_error = _validate_check_arguments(args)
    if argument_error is not None:
        _safe_write_line(sys.stderr, argument_error)
        return 2
    _configure_application_identity()

    headless_mode = (
        args.headless_check
        or args.danbooru_smoke
        or args.danbooru_download_smoke
        or args.wallpaper_probe
        or args.eagle_probe
        or args.eagle_import_smoke is not None
    )
    application_type = QCoreApplication if headless_mode else QGuiApplication
    application = application_type([APPLICATION_NAME])

    try:
        runtime = initialize_runtime(data_root=args.data_root)
    except Exception as exc:  # startup failures must be visible to CLI and GUI launches
        _safe_write_line(sys.stderr, f"Application initialization failed: {exc}")
        if args.gui_smoke_check and args.data_root is not None:
            return _emit_check_result(
                _failed_gui_result(args.data_root),
                1,
                args.check_output,
            )
        if args.headless_check and args.data_root is not None:
            return _emit_check_result(
                _failed_headless_result(args.data_root),
                1,
                args.check_output,
            )
        return 1

    if args.headless_check:
        return _emit_check_result(_headless_result(runtime), 0, args.check_output)

    if args.gui_smoke_check:
        try:
            result, exit_code = run_gui_smoke_check(
                application,
                danbooru_base_url=runtime.settings.danbooru_api_url,
                runtime=runtime,
            )
        except Exception as exc:
            _safe_write_line(sys.stderr, f"GUI smoke check failed: {exc}")
            result = _failed_gui_result(args.data_root)
            exit_code = 1
        return _emit_check_result(result, exit_code, args.check_output)

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
            _safe_write_json(payload)
            return 1
        _safe_write_json(result.payload)
        return result.exit_code

    if args.danbooru_download_smoke:
        try:
            result = run_danbooru_download_smoke(
                runtime.settings.danbooru_api_url,
                args.danbooru_tags,
                runtime.paths.download_dir,
                timeout_ms=args.danbooru_timeout_ms,
            )
        except Exception as exc:
            payload = {
                "status": "error",
                "operation": "download",
                "error_kind": "internal",
                "message": f"Danbooru download smoke check failed: {exc}",
                "tags": args.danbooru_tags.strip(),
            }
            _safe_write_json(payload)
            return 1
        _safe_write_json(result.payload)
        return result.exit_code

    if args.wallpaper_probe:
        payload, exit_code = wallpaper_probe_result(runtime.platform)
        _safe_write_json(payload)
        return exit_code

    if args.eagle_probe:
        payload, exit_code = eagle_probe_result(runtime.platform)
        _safe_write_json(payload)
        return exit_code

    if args.eagle_import_smoke is not None:
        metadata: dict[str, object] = {}
        for field, value in (
            ("name", args.eagle_name),
            ("website", args.eagle_website),
            ("annotation", args.eagle_annotation),
        ):
            if value is not None:
                metadata[field] = value
        if args.eagle_tag is not None:
            metadata["tags"] = args.eagle_tag
        payload, exit_code = eagle_import_smoke_result(
            runtime.platform,
            args.eagle_import_smoke,
            folder_identifier=args.eagle_folder,
            metadata=metadata,
        )
        _safe_write_json(payload)
        return exit_code

    return launch_gui(
        application,
        danbooru_base_url=runtime.settings.danbooru_api_url,
        runtime=runtime,
    )


if __name__ == "__main__":
    raise SystemExit(main())

