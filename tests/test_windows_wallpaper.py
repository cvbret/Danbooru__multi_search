from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from danbooru_multi_search.__main__ import wallpaper_probe_result
from danbooru_multi_search.platform import (
    OperationResult,
    ServiceAvailability,
    ValueResult,
    WallpaperSnapshot,
    WallpaperStyle,
    create_platform_adapter,
)
from danbooru_multi_search.platform.windows_wallpaper import (
    MAX_WALLPAPER_PATH_CHARS,
    SET_WALLPAPER_FLAGS,
    SPI_GETDESKWALLPAPER,
    SPI_SETDESKWALLPAPER,
    TILE_WALLPAPER_VALUE,
    WALLPAPER_STYLE_VALUE,
    CtypesWallpaperSystemBackend,
    RegistryValueSnapshot,
    WinRegistryWallpaperBackend,
    WindowsWallpaperService,
    create_default_windows_wallpaper_service,
    style_from_windows_values,
    style_to_windows_values,
)


REG_SZ = 1


@dataclass
class FakeSystemBackend:
    current_path: str
    events: list[tuple[Any, ...]] = field(default_factory=list)
    fail_get: bool = False
    fail_set_calls: set[int] = field(default_factory=set)
    mutate_before_failure: bool = False
    get_calls: int = 0
    set_calls: list[str] = field(default_factory=list)

    def get_wallpaper(self) -> str:
        self.get_calls += 1
        self.events.append(("system_get",))
        if self.fail_get:
            raise OSError("read denied")
        return self.current_path

    def set_wallpaper(self, absolute_path: str) -> None:
        self.set_calls.append(absolute_path)
        call_number = len(self.set_calls)
        self.events.append(("system_set", absolute_path))
        if call_number in self.fail_set_calls:
            if self.mutate_before_failure:
                self.current_path = absolute_path
            raise OSError(f"set failure {call_number}")
        self.current_path = absolute_path


@dataclass
class FakeRegistryBackend:
    values: dict[str, RegistryValueSnapshot]
    events: list[tuple[Any, ...]] = field(default_factory=list)
    fail_read: bool = False
    fail_set_before: set[int] = field(default_factory=set)
    fail_set_after: set[int] = field(default_factory=set)
    fail_restore_before: set[int] = field(default_factory=set)
    fail_restore_after: set[int] = field(default_factory=set)
    read_calls: list[str] = field(default_factory=list)
    set_calls: list[tuple[str, str]] = field(default_factory=list)
    restore_calls: list[tuple[str, RegistryValueSnapshot]] = field(
        default_factory=list
    )

    def read_value(self, name: str) -> RegistryValueSnapshot:
        self.read_calls.append(name)
        self.events.append(("registry_read", name))
        if self.fail_read:
            raise OSError("registry read denied")
        return self.values.get(name, RegistryValueSnapshot(False))

    def set_string_value(self, name: str, value: str) -> None:
        self.set_calls.append((name, value))
        call_number = len(self.set_calls)
        self.events.append(("registry_set", name, value))
        if call_number in self.fail_set_before:
            raise OSError(f"registry set failure {call_number}")
        self.values[name] = RegistryValueSnapshot(True, value, REG_SZ)
        if call_number in self.fail_set_after:
            raise OSError(f"registry set failure {call_number}")

    def restore_value(self, name: str, snapshot: RegistryValueSnapshot) -> None:
        self.restore_calls.append((name, snapshot))
        call_number = len(self.restore_calls)
        self.events.append(("registry_restore", name, snapshot))
        if call_number in self.fail_restore_before:
            raise OSError(f"registry restore failure {call_number}")
        if snapshot.exists:
            self.values[name] = snapshot
        else:
            self.values.pop(name, None)
        if call_number in self.fail_restore_after:
            raise OSError(f"registry restore failure {call_number}")


def make_service(
    current_path: str,
    *,
    wallpaper_style: RegistryValueSnapshot | None = None,
    tile_wallpaper: RegistryValueSnapshot | None = None,
) -> tuple[WindowsWallpaperService, FakeSystemBackend, FakeRegistryBackend]:
    events: list[tuple[Any, ...]] = []
    system = FakeSystemBackend(current_path, events=events)
    registry = FakeRegistryBackend(
        {
            WALLPAPER_STYLE_VALUE: wallpaper_style
            or RegistryValueSnapshot(True, "6", REG_SZ),
            TILE_WALLPAPER_VALUE: tile_wallpaper
            or RegistryValueSnapshot(True, "0", REG_SZ),
        },
        events=events,
    )
    return WindowsWallpaperService(system, registry), system, registry


def make_image(tmp_path: Path, name: str) -> Path:
    image = tmp_path / name
    image.write_bytes(b"test image placeholder")
    return image


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        (WallpaperStyle.CENTER, ("0", "0")),
        (WallpaperStyle.TILE, ("0", "1")),
        (WallpaperStyle.STRETCH, ("2", "0")),
        (WallpaperStyle.FIT, ("6", "0")),
        (WallpaperStyle.FILL, ("10", "0")),
    ],
)
def test_wallpaper_style_mapping_round_trips(style, expected):
    assert style_to_windows_values(style) == expected
    assert style_from_windows_values(*expected) is style


@pytest.mark.parametrize(
    "values",
    [(None, None), ("0", "2"), ("10", "1"), ("6", None), ("FILL", "0")],
)
def test_unknown_registry_combinations_are_not_guessed(values):
    assert style_from_windows_values(*values) is None


@pytest.mark.parametrize("invalid_style", ["fill", "FILL", "unknown", None])
def test_forward_mapping_requires_a_wallpaper_style_enum(invalid_style):
    with pytest.raises(ValueError, match="Unsupported wallpaper style"):
        style_to_windows_values(invalid_style)  # type: ignore[arg-type]


def test_invalid_style_is_rejected_before_any_backend_call(tmp_path):
    image = make_image(tmp_path, "image.png")
    service, system, registry = make_service("")

    result = service.set_wallpaper(image, "FILL")  # type: ignore[arg-type]

    assert result.code == "invalid_wallpaper_style"
    assert system.get_calls == 0
    assert system.set_calls == []
    assert registry.read_calls == []


@pytest.mark.parametrize(
    "name",
    [
        "image.gif",
        "image.webm",
        "image.mp4",
        "image.svg",
        "image.webp",
        "image",
        "image.jpg.exe",
    ],
)
def test_unsupported_formats_do_not_call_backends(tmp_path, name):
    path = make_image(tmp_path, name)
    service, system, registry = make_service("")

    result = service.set_wallpaper(path)

    assert result.code == "unsupported_format"
    assert system.get_calls == 0
    assert system.set_calls == []
    assert registry.read_calls == []


def test_missing_path_and_directory_do_not_call_backends(tmp_path):
    service, system, registry = make_service("")

    missing = service.set_wallpaper(tmp_path / "missing.png")
    directory = service.set_wallpaper(tmp_path)

    assert missing.code == "file_not_found"
    assert directory.code == "not_a_file"
    assert system.get_calls == 0
    assert system.set_calls == []
    assert registry.read_calls == []


def test_network_path_is_rejected_before_any_file_or_backend_access():
    service, system, registry = make_service("")

    result = service.set_wallpaper(Path(r"\\server\share\image.png"))

    assert result.code == "network_path_unsupported"
    assert system.get_calls == 0
    assert system.set_calls == []
    assert registry.read_calls == []


@pytest.mark.parametrize(
    "extension",
    [".bmp", ".dib", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".PNG"],
)
def test_supported_static_formats_use_an_absolute_path(tmp_path, extension):
    old_image = make_image(tmp_path, "old.jpg").resolve()
    new_image = make_image(tmp_path, f"new{extension}")
    service, system, registry = make_service(str(old_image))

    result = service.set_wallpaper(new_image)

    assert result.succeeded is True
    assert system.set_calls == [str(new_image.resolve())]
    assert registry.set_calls == []


def test_current_snapshot_preserves_unknown_raw_values_and_empty_path():
    original_style = RegistryValueSnapshot(True, "17", 99)
    original_tile = RegistryValueSnapshot(True, "3", 98)
    service, _, _ = make_service(
        "",
        wallpaper_style=original_style,
        tile_wallpaper=original_tile,
    )

    result = service.get_current()

    assert result.succeeded is True
    assert result.value == WallpaperSnapshot(
        path=None,
        style=None,
        wallpaper_style_value="17",
        tile_wallpaper_value="3",
    )


def test_set_and_one_time_restore_preserve_exact_registry_state(tmp_path):
    old_image = make_image(tmp_path, "old.jpg").resolve()
    new_image = make_image(tmp_path, "new.png").resolve()
    original_style = RegistryValueSnapshot(True, "17", 99)
    original_tile = RegistryValueSnapshot(False)
    service, system, registry = make_service(
        str(old_image),
        wallpaper_style=original_style,
        tile_wallpaper=original_tile,
    )

    set_result = service.set_wallpaper(new_image, WallpaperStyle.FILL)
    restore_result = service.restore_previous()
    repeated_result = service.restore_previous()

    assert set_result.succeeded is True
    assert registry.set_calls == [
        (WALLPAPER_STYLE_VALUE, "10"),
        (TILE_WALLPAPER_VALUE, "0"),
    ]
    assert restore_result.succeeded is True
    assert system.set_calls == [str(new_image), str(old_image)]
    assert registry.values[WALLPAPER_STYLE_VALUE] == original_style
    assert TILE_WALLPAPER_VALUE not in registry.values
    assert repeated_result.code == "no_restore_point"
    assert len(system.set_calls) == 2


def test_consecutive_successes_restore_the_most_recently_replaced_image(tmp_path):
    image_a = make_image(tmp_path, "a.jpg").resolve()
    image_b = make_image(tmp_path, "b.jpg").resolve()
    image_c = make_image(tmp_path, "c.jpg").resolve()
    service, system, _ = make_service(str(image_a))

    assert service.set_wallpaper(image_b).succeeded
    assert service.set_wallpaper(image_c).succeeded
    assert service.restore_previous().succeeded

    assert system.set_calls == [str(image_b), str(image_c), str(image_b)]


def test_partial_registry_write_is_rolled_back_exactly(tmp_path):
    old_image = make_image(tmp_path, "old.jpg").resolve()
    new_image = make_image(tmp_path, "new.jpg").resolve()
    original_style = RegistryValueSnapshot(True, "17", 99)
    original_tile = RegistryValueSnapshot(False)
    service, system, registry = make_service(
        str(old_image),
        wallpaper_style=original_style,
        tile_wallpaper=original_tile,
    )
    registry.fail_set_after.add(2)

    result = service.set_wallpaper(new_image, WallpaperStyle.TILE)
    restore_result = service.restore_previous()

    assert result.code == "registry_write_failed"
    assert restore_result.code == "no_restore_point"
    assert system.set_calls == []
    assert system.get_calls == 1
    assert registry.values[WALLPAPER_STYLE_VALUE] == original_style
    assert TILE_WALLPAPER_VALUE not in registry.values
    assert registry.restore_calls == [
        (WALLPAPER_STYLE_VALUE, original_style),
        (TILE_WALLPAPER_VALUE, original_tile),
    ]
    assert registry.read_calls == [WALLPAPER_STYLE_VALUE, TILE_WALLPAPER_VALUE]


def test_first_registry_write_failure_skips_second_write_and_system_set(tmp_path):
    old_image = make_image(tmp_path, "old.jpg").resolve()
    new_image = make_image(tmp_path, "new.jpg").resolve()
    service, system, registry = make_service(str(old_image))
    registry.fail_set_before.add(1)

    result = service.set_wallpaper(new_image, WallpaperStyle.CENTER)
    restore_result = service.restore_previous()

    assert result.code == "registry_write_failed"
    assert restore_result.code == "no_restore_point"
    assert registry.set_calls == [(WALLPAPER_STYLE_VALUE, "0")]
    assert system.set_calls == []
    assert system.get_calls == 1
    assert len(registry.restore_calls) == 2
    assert len(registry.read_calls) == 2


def test_system_failure_rolls_back_and_keeps_primary_error(tmp_path):
    old_image = make_image(tmp_path, "old.jpg").resolve()
    new_image = make_image(tmp_path, "new.jpg").resolve()
    original_style = RegistryValueSnapshot(True, "6", 99)
    original_tile = RegistryValueSnapshot(False)
    service, system, registry = make_service(
        str(old_image),
        wallpaper_style=original_style,
        tile_wallpaper=original_tile,
    )
    system.fail_set_calls.add(1)
    system.mutate_before_failure = True

    result = service.set_wallpaper(new_image, WallpaperStyle.STRETCH)
    restore_result = service.restore_previous()

    assert result.code == "wallpaper_set_failed"
    assert restore_result.code == "no_restore_point"
    assert "set failure 1" in (result.message or "")
    assert system.set_calls == [str(new_image), str(old_image)]
    assert system.get_calls == 1
    assert registry.values[WALLPAPER_STYLE_VALUE] == original_style
    assert TILE_WALLPAPER_VALUE not in registry.values
    assert len(registry.read_calls) == 2
    assert len(registry.restore_calls) == 2


def test_rollback_failure_is_reported_without_replacing_primary_error(tmp_path):
    old_image = make_image(tmp_path, "old.jpg").resolve()
    new_image = make_image(tmp_path, "new.jpg").resolve()
    service, system, registry = make_service(str(old_image))
    system.fail_set_calls.update({1, 2})
    registry.fail_restore_before.add(1)

    result = service.set_wallpaper(new_image, WallpaperStyle.FILL)

    assert result.code == "wallpaper_set_failed"
    assert "Rollback also failed" in (result.message or "")
    assert "registry value WallpaperStyle" in (result.message or "")
    assert "desktop wallpaper" in (result.message or "")

    system.fail_set_calls.clear()
    registry.fail_restore_before.clear()
    restore_result = service.restore_previous()

    assert restore_result.succeeded is True
    assert system.get_calls == 2
    assert len(system.set_calls) == 3
    assert len(registry.read_calls) == 4
    assert len(registry.restore_calls) == 4


def test_failed_later_set_does_not_replace_existing_restore_history(tmp_path):
    image_a = make_image(tmp_path, "a.jpg").resolve()
    image_b = make_image(tmp_path, "b.jpg").resolve()
    image_c = make_image(tmp_path, "c.jpg").resolve()
    service, system, registry = make_service(str(image_a))

    assert service.set_wallpaper(image_b).succeeded
    system.fail_set_calls.update({2, 3})
    failed = service.set_wallpaper(image_c)
    restored = service.restore_previous()

    assert failed.code == "wallpaper_set_failed"
    assert "Rollback also failed" in (failed.message or "")
    assert restored.succeeded is True
    assert system.set_calls == [
        str(image_b),
        str(image_c),
        str(image_b),
        str(image_a),
    ]
    assert system.get_calls == 3
    assert len(registry.read_calls) == 6
    assert len(registry.restore_calls) == 2


def test_restore_failure_retains_history_for_retry(tmp_path):
    old_image = make_image(tmp_path, "old.jpg").resolve()
    new_image = make_image(tmp_path, "new.jpg").resolve()
    service, system, _ = make_service(str(old_image))

    assert service.set_wallpaper(new_image).succeeded
    system.fail_set_calls.add(2)
    failed_restore = service.restore_previous()
    retried_restore = service.restore_previous()

    assert failed_restore.code == "wallpaper_restore_failed"
    assert retried_restore.succeeded is True
    assert system.set_calls == [
        str(new_image),
        str(old_image),
        str(new_image),
        str(old_image),
    ]


def test_registry_restore_failure_retains_history_for_retry(tmp_path):
    old_image = make_image(tmp_path, "old.jpg").resolve()
    new_image = make_image(tmp_path, "new.jpg").resolve()
    service, system, registry = make_service(str(old_image))

    assert service.set_wallpaper(new_image, WallpaperStyle.FILL).succeeded
    registry.fail_restore_after.add(2)
    failed_restore = service.restore_previous()
    retried_restore = service.restore_previous()

    assert failed_restore.code == "registry_restore_failed"
    assert retried_restore.succeeded is True
    assert system.set_calls == [str(new_image), str(old_image)]


def test_empty_wallpaper_path_can_be_restored(tmp_path):
    new_image = make_image(tmp_path, "new.png").resolve()
    service, system, _ = make_service("")

    assert service.set_wallpaper(new_image).succeeded
    assert service.restore_previous().succeeded

    assert system.set_calls == [str(new_image), ""]


def test_read_failures_return_stable_results():
    service, system, registry = make_service("")
    system.fail_get = True

    system_result = service.get_current()

    assert system_result.code == "wallpaper_read_failed"
    assert registry.read_calls == []

    system.fail_get = False
    registry.fail_read = True
    registry_result = service.get_current()
    assert registry_result.code == "registry_read_failed"


class FakeSystemParametersInfo:
    def __init__(self, current_path: str) -> None:
        self.current_path = current_path
        self.calls: list[tuple[int, int, Any, int]] = []
        self.argtypes: list[Any] | None = None
        self.restype: Any = None

    def __call__(self, action: int, ui_param: int, value: Any, flags: int) -> int:
        self.calls.append((action, ui_param, value, flags))
        if action == SPI_GETDESKWALLPAPER:
            source = ctypes.create_unicode_buffer(self.current_path)
            ctypes.memmove(value, source, ctypes.sizeof(source))
        elif action == SPI_SETDESKWALLPAPER:
            self.current_path = ctypes.cast(value, ctypes.c_wchar_p).value or ""
        return 1


class FailingSystemParametersInfo:
    def __init__(self) -> None:
        self.argtypes: list[Any] | None = None
        self.restype: Any = None

    def __call__(self, action: int, ui_param: int, value: Any, flags: int) -> int:
        return 0


def test_ctypes_backend_uses_exact_unicode_get_and_set_arguments():
    api = FakeSystemParametersInfo("C:\\壁纸\\当前 图片.jpg")
    backend = CtypesWallpaperSystemBackend(api)

    assert backend.get_wallpaper() == "C:\\壁纸\\当前 图片.jpg"
    backend.set_wallpaper("C:\\壁纸\\新 图片.png")

    assert api.calls[0][0:2] == (
        SPI_GETDESKWALLPAPER,
        MAX_WALLPAPER_PATH_CHARS,
    )
    assert api.calls[0][3] == 0
    assert api.calls[1][0:2] == (SPI_SETDESKWALLPAPER, 0)
    assert api.calls[1][3] == SET_WALLPAPER_FLAGS
    assert api.current_path == "C:\\壁纸\\新 图片.png"
    assert api.argtypes is not None and len(api.argtypes) == 4
    assert api.restype is not None


def test_ctypes_backend_converts_zero_results_to_os_errors():
    backend = CtypesWallpaperSystemBackend(FailingSystemParametersInfo())

    with pytest.raises(OSError, match="GET failed"):
        backend.get_wallpaper()
    with pytest.raises(OSError, match="SET failed"):
        backend.set_wallpaper("C:\\壁纸\\图片.png")


class FakeWinreg:
    HKEY_CURRENT_USER = object()
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = REG_SZ

    def __init__(self) -> None:
        self.values: dict[str, tuple[Any, int]] = {
            WALLPAPER_STYLE_VALUE: ("17", 99)
        }
        self.open_keys: list[object] = []
        self.closed_keys: list[object] = []

    def OpenKey(self, root, path, reserved, access):  # noqa: N802
        key = object()
        self.open_keys.append(key)
        return key

    def CloseKey(self, key):  # noqa: N802
        self.closed_keys.append(key)

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name]

    def SetValueEx(self, key, name, reserved, value_type, value):  # noqa: N802
        self.values[name] = (value, value_type)

    def DeleteValue(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


def test_registry_backend_preserves_existence_content_type_and_closes_handles():
    winreg = FakeWinreg()
    backend = WinRegistryWallpaperBackend(winreg)

    existing = backend.read_value(WALLPAPER_STYLE_VALUE)
    missing = backend.read_value(TILE_WALLPAPER_VALUE)
    backend.set_string_value(TILE_WALLPAPER_VALUE, "1")
    backend.restore_value(WALLPAPER_STYLE_VALUE, existing)
    backend.restore_value(TILE_WALLPAPER_VALUE, RegistryValueSnapshot(False))

    assert existing == RegistryValueSnapshot(True, "17", 99)
    assert missing == RegistryValueSnapshot(False)
    assert winreg.values == {WALLPAPER_STYLE_VALUE: ("17", 99)}
    assert len(winreg.open_keys) == len(winreg.closed_keys)


def test_platform_import_does_not_eagerly_import_windows_wallpaper_module():
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project_root / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import danbooru_multi_search.platform; "
                "print('danbooru_multi_search.platform.windows_wallpaper' "
                "in sys.modules)"
            ),
        ],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_non_windows_default_factory_is_explicitly_unavailable():
    service = create_default_windows_wallpaper_service(is_windows=False)

    assert service.availability.available is False
    assert service.get_current().code == "unsupported_platform"


class ProbeWallpaperService:
    availability = ServiceAvailability(available=True)

    def __init__(self, result: ValueResult[WallpaperSnapshot]) -> None:
        self.result = result
        self.get_calls = 0
        self.set_calls = 0
        self.restore_calls = 0

    def get_current(self) -> ValueResult[WallpaperSnapshot]:
        self.get_calls += 1
        return self.result

    def set_wallpaper(
        self,
        image_path: Path,
        style: WallpaperStyle | None = None,
    ) -> OperationResult:
        self.set_calls += 1
        raise AssertionError("The read-only probe called set_wallpaper().")

    def restore_previous(self) -> OperationResult:
        self.restore_calls += 1
        raise AssertionError("The read-only probe called restore_previous().")


def test_wallpaper_probe_only_reads_and_returns_stable_success_json(tmp_path):
    snapshot = WallpaperSnapshot(
        path=Path("C:/壁纸/当前 图片.jpg"),
        style=WallpaperStyle.FILL,
        wallpaper_style_value="10",
        tile_wallpaper_value="0",
    )
    service = ProbeWallpaperService(ValueResult(True, snapshot))
    adapter = create_platform_adapter(
        "win32",
        override_root=tmp_path,
        wallpaper_service=service,
    )

    payload, exit_code = wallpaper_probe_result(adapter)

    assert exit_code == 0
    assert payload == {
        "status": "ok",
        "platform": "windows",
        "wallpaper": {
            "path": str(snapshot.path),
            "style": "fill",
            "wallpaper_style_value": "10",
            "tile_wallpaper_value": "0",
        },
    }
    assert service.get_calls == 1
    assert service.set_calls == 0
    assert service.restore_calls == 0


def test_wallpaper_probe_returns_stable_failure_json_without_writes(tmp_path):
    service = ProbeWallpaperService(
        ValueResult.unavailable("wallpaper_read_failed", "read denied")
    )
    adapter = create_platform_adapter(
        "win32",
        override_root=tmp_path,
        wallpaper_service=service,
    )

    payload, exit_code = wallpaper_probe_result(adapter)

    assert exit_code == 2
    assert payload == {
        "status": "error",
        "platform": "windows",
        "code": "wallpaper_read_failed",
        "message": "read denied",
    }
    assert service.get_calls == 1
    assert service.set_calls == 0
    assert service.restore_calls == 0
