from __future__ import annotations

from pathlib import Path

import pytest

from danbooru_multi_search.__main__ import (
    _build_parser,
    eagle_import_smoke_result,
    eagle_probe_result,
)
from danbooru_multi_search.integrations.eagle import (
    EagleConnectionInfo,
    EagleFolderInfo,
    EagleImportResult,
    EagleLibraryInfo,
)
from danbooru_multi_search.platform import (
    OperationResult,
    ServiceAvailability,
    ValueResult,
    WallpaperStyle,
    create_platform_adapter,
)


class FakeWallpaperService:
    availability = ServiceAvailability.unavailable("test", "not used")

    def get_current(self):
        raise AssertionError("Eagle CLI test read the wallpaper.")

    def set_wallpaper(
        self,
        image_path: Path,
        style: WallpaperStyle | None = None,
    ) -> OperationResult:
        raise AssertionError("Eagle CLI test changed the wallpaper.")

    def restore_previous(self) -> OperationResult:
        raise AssertionError("Eagle CLI test restored the wallpaper.")


class ProbeEagleBridge:
    availability = ServiceAvailability(available=True)
    is_supported = True
    unavailable_reason = None

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.connection_result = ValueResult(
            True,
            EagleConnectionInfo(
                version="4.0.0",
                prerelease_version=None,
                build_version="build21",
                platform="win32",
                build_number=21,
            ),
        )
        self.library_result = ValueResult(
            True,
            EagleLibraryInfo(
                identifier="library-hash",
                name="Library",
                path=Path(r"D:\Library.library"),
            ),
        )
        self.import_result = ValueResult(
            True,
            EagleImportResult(item_identifier="ITEM-1"),
        )
        self.import_arguments: tuple[object, ...] | None = None

    def test_connection(self):
        self.calls.append("connection")
        return self.connection_result

    def get_current_library(self):
        self.calls.append("library")
        return self.library_result

    def list_folders(self):
        self.calls.append("folders")
        return ValueResult(True, (EagleFolderInfo("folder", "Folder"),))

    def import_file(
        self,
        file_path: Path,
        *,
        folder_identifier: str | None = None,
        metadata=None,
    ):
        self.calls.append("import")
        self.import_arguments = (file_path, folder_identifier, metadata)
        return self.import_result


def make_adapter(tmp_path, bridge):
    return create_platform_adapter(
        "win32",
        override_root=tmp_path,
        wallpaper_service=FakeWallpaperService(),
        eagle_bridge=bridge,
    )


def test_eagle_probe_only_reads_app_and_library(tmp_path):
    bridge = ProbeEagleBridge()
    adapter = make_adapter(tmp_path, bridge)

    payload, exit_code = eagle_probe_result(adapter)

    assert exit_code == 0
    assert bridge.calls == ["connection", "library"]
    assert payload == {
        "status": "ok",
        "platform": "windows",
        "eagle": {
            "version": "4.0.0",
            "prerelease_version": None,
            "build_version": "build21",
            "build_number": 21,
            "platform": "win32",
        },
        "library": {
            "identifier": "library-hash",
            "name": "Library",
            "path": str(Path(r"D:\Library.library")),
        },
    }


def test_eagle_probe_stops_after_connection_failure(tmp_path):
    bridge = ProbeEagleBridge()
    bridge.connection_result = ValueResult.unavailable(
        "eagle_connection_error",
        "refused",
    )
    adapter = make_adapter(tmp_path, bridge)

    payload, exit_code = eagle_probe_result(adapter)

    assert exit_code == 2
    assert bridge.calls == ["connection"]
    assert payload["operation"] == "connection"
    assert payload["code"] == "eagle_connection_error"


def test_eagle_import_smoke_is_an_explicit_single_bridge_call(tmp_path):
    bridge = ProbeEagleBridge()
    adapter = make_adapter(tmp_path, bridge)
    source_file = tmp_path / "image.png"
    source_file.write_bytes(b"image")
    metadata: dict[str, object] = {"name": "Image", "tags": ["cat"]}

    payload, exit_code = eagle_import_smoke_result(
        adapter,
        source_file,
        folder_identifier="folder",
        metadata=metadata,
    )

    assert exit_code == 0
    assert payload["item_identifier"] == "ITEM-1"
    assert bridge.calls == ["import"]
    assert bridge.import_arguments == (source_file, "folder", metadata)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--headless-check", "--eagle-probe"],
        ["--danbooru-smoke", "--eagle-probe"],
        ["--wallpaper-probe", "--eagle-probe"],
        ["--eagle-probe", "--eagle-import-smoke", "image.png"],
    ],
)
def test_eagle_cli_modes_are_mutually_exclusive(arguments):
    with pytest.raises(SystemExit):
        _build_parser().parse_args(arguments)


def test_eagle_import_cli_parses_optional_metadata():
    args = _build_parser().parse_args(
        [
            "--eagle-import-smoke",
            "image.png",
            "--eagle-folder",
            "folder",
            "--eagle-name",
            "Image",
            "--eagle-website",
            "https://example.com",
            "--eagle-tag",
            "cat",
            "--eagle-tag",
            "illustration",
            "--eagle-annotation",
            "note",
        ]
    )

    assert args.eagle_import_smoke == Path("image.png")
    assert args.eagle_folder == "folder"
    assert args.eagle_name == "Image"
    assert args.eagle_website == "https://example.com"
    assert args.eagle_tag == ["cat", "illustration"]
    assert args.eagle_annotation == "note"
