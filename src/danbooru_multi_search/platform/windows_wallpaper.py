"""Windows desktop wallpaper service using Unicode Win32 APIs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from danbooru_multi_search.platform.contracts import (
    OperationResult,
    ServiceAvailability,
    UNSUPPORTED_PLATFORM,
    ValueResult,
    WallpaperSnapshot,
    WallpaperStyle,
)
from danbooru_multi_search.platform.stubs import UnavailableWallpaperService


SPI_SETDESKWALLPAPER = 0x0014
SPI_GETDESKWALLPAPER = 0x0073
SPIF_UPDATEINIFILE = 0x0001
SPIF_SENDCHANGE = 0x0002
SET_WALLPAPER_FLAGS = SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
MAX_WALLPAPER_PATH_CHARS = 260

WALLPAPER_STYLE_VALUE = "WallpaperStyle"
TILE_WALLPAPER_VALUE = "TileWallpaper"

SUPPORTED_STATIC_EXTENSIONS = frozenset(
    {".bmp", ".dib", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}
)

WINDOWS_STYLE_VALUES: dict[WallpaperStyle, tuple[str, str]] = {
    WallpaperStyle.CENTER: ("0", "0"),
    WallpaperStyle.TILE: ("0", "1"),
    WallpaperStyle.STRETCH: ("2", "0"),
    WallpaperStyle.FIT: ("6", "0"),
    WallpaperStyle.FILL: ("10", "0"),
}


@dataclass(frozen=True, slots=True)
class RegistryValueSnapshot:
    exists: bool
    value: Any = None
    value_type: int | None = None


class WallpaperSystemBackend(Protocol):
    def get_wallpaper(self) -> str: ...

    def set_wallpaper(self, absolute_path: str) -> None: ...


class WallpaperRegistryBackend(Protocol):
    def read_value(self, name: str) -> RegistryValueSnapshot: ...

    def set_string_value(self, name: str, value: str) -> None: ...

    def restore_value(self, name: str, snapshot: RegistryValueSnapshot) -> None: ...


@dataclass(frozen=True, slots=True)
class _CurrentWallpaperState:
    snapshot: WallpaperSnapshot
    wallpaper_style: RegistryValueSnapshot
    tile_wallpaper: RegistryValueSnapshot


class _BackendReadError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def style_to_windows_values(style: WallpaperStyle) -> tuple[str, str]:
    if not isinstance(style, WallpaperStyle):
        raise ValueError(f"Unsupported wallpaper style: {style!r}")
    try:
        return WINDOWS_STYLE_VALUES[style]
    except KeyError as exc:
        raise ValueError(f"Unsupported wallpaper style: {style!r}") from exc


def style_from_windows_values(
    wallpaper_style: str | None,
    tile_wallpaper: str | None,
) -> WallpaperStyle | None:
    pair = (wallpaper_style, tile_wallpaper)
    for style, values in WINDOWS_STYLE_VALUES.items():
        if pair == values:
            return style
    return None


class WindowsWallpaperService:
    availability = ServiceAvailability(available=True)

    def __init__(
        self,
        system_backend: WallpaperSystemBackend,
        registry_backend: WallpaperRegistryBackend,
    ) -> None:
        self._system = system_backend
        self._registry = registry_backend
        self._restore_point: _CurrentWallpaperState | None = None

    def get_current(self) -> ValueResult[WallpaperSnapshot]:
        try:
            current = self._read_current_state()
        except _BackendReadError as exc:
            return ValueResult.unavailable(exc.code, str(exc))
        return ValueResult(succeeded=True, value=current.snapshot)

    def set_wallpaper(
        self,
        image_path: Path,
        style: WallpaperStyle | None = None,
    ) -> OperationResult:
        if style is not None and not isinstance(style, WallpaperStyle):
            return OperationResult(
                False,
                "invalid_wallpaper_style",
                f"Wallpaper style is not supported: {style!r}",
            )
        validation = self._validate_image_path(image_path)
        if isinstance(validation, OperationResult):
            return validation
        absolute_path = validation

        try:
            current = self._read_current_state()
        except _BackendReadError as exc:
            return OperationResult(False, exc.code, str(exc))

        registry_changed = style is not None
        if style is not None:
            wallpaper_style, tile_wallpaper = style_to_windows_values(style)
            try:
                self._registry.set_string_value(
                    WALLPAPER_STYLE_VALUE,
                    wallpaper_style,
                )
                self._registry.set_string_value(
                    TILE_WALLPAPER_VALUE,
                    tile_wallpaper,
                )
            except Exception as exc:
                rollback_errors = self._rollback_registry(current)
                if rollback_errors:
                    self._remember_failed_change(current)
                return self._failure_with_rollback(
                    "registry_write_failed",
                    f"Unable to update wallpaper display style: {exc}",
                    rollback_errors,
                )

        try:
            self._system.set_wallpaper(str(absolute_path))
        except Exception as exc:
            rollback_errors: list[str] = []
            if registry_changed:
                rollback_errors.extend(self._rollback_registry(current))
            rollback_errors.extend(self._rollback_system(current))
            if rollback_errors:
                self._remember_failed_change(current)
            return self._failure_with_rollback(
                "wallpaper_set_failed",
                f"Unable to set desktop wallpaper: {exc}",
                rollback_errors,
            )

        self._restore_point = current
        return OperationResult(
            succeeded=True,
            code="wallpaper_set",
            message="Desktop wallpaper was updated.",
        )

    def restore_previous(self) -> OperationResult:
        target = self._restore_point
        if target is None:
            return OperationResult(
                False,
                "no_restore_point",
                "There is no wallpaper state to restore.",
            )

        try:
            current = self._read_current_state()
        except _BackendReadError as exc:
            return OperationResult(False, exc.code, str(exc))

        try:
            self._restore_registry_values(target)
        except Exception as exc:
            rollback_errors = self._rollback_registry(current)
            return self._failure_with_rollback(
                "registry_restore_failed",
                f"Unable to restore wallpaper display style: {exc}",
                rollback_errors,
            )

        try:
            self._system.set_wallpaper(self._snapshot_path(target.snapshot))
        except Exception as exc:
            rollback_errors = self._rollback_registry(current)
            rollback_errors.extend(self._rollback_system(current))
            return self._failure_with_rollback(
                "wallpaper_restore_failed",
                f"Unable to restore desktop wallpaper: {exc}",
                rollback_errors,
            )

        self._restore_point = None
        return OperationResult(
            succeeded=True,
            code="wallpaper_restored",
            message="The previous desktop wallpaper was restored.",
        )

    @staticmethod
    def _validate_image_path(image_path: Path) -> Path | OperationResult:
        path = Path(image_path).expanduser()
        raw_path = str(path)
        if raw_path.startswith("\\\\"):
            return OperationResult(
                False,
                "network_path_unsupported",
                "Wallpaper images must use a local file path.",
            )

        try:
            if not path.exists():
                return OperationResult(
                    False,
                    "file_not_found",
                    f"Wallpaper image does not exist: {path}",
                )
            if not path.is_file():
                return OperationResult(
                    False,
                    "not_a_file",
                    f"Wallpaper image is not a regular file: {path}",
                )
            absolute_path = path.resolve(strict=True)
        except OSError as exc:
            return OperationResult(
                False,
                "file_validation_failed",
                f"Unable to inspect wallpaper image: {exc}",
            )

        if str(absolute_path).startswith("\\\\"):
            return OperationResult(
                False,
                "network_path_unsupported",
                "Wallpaper images must use a local file path.",
            )

        extension = absolute_path.suffix.lower()
        if extension not in SUPPORTED_STATIC_EXTENSIONS:
            displayed_extension = extension or "<none>"
            return OperationResult(
                False,
                "unsupported_format",
                f"Wallpaper image format is not supported: {displayed_extension}",
            )
        return absolute_path

    def _read_current_state(self) -> _CurrentWallpaperState:
        try:
            wallpaper_path = self._system.get_wallpaper()
        except Exception as exc:
            raise _BackendReadError(
                "wallpaper_read_failed",
                f"Unable to read the current desktop wallpaper: {exc}",
            ) from exc

        try:
            wallpaper_style = self._registry.read_value(WALLPAPER_STYLE_VALUE)
            tile_wallpaper = self._registry.read_value(TILE_WALLPAPER_VALUE)
        except Exception as exc:
            raise _BackendReadError(
                "registry_read_failed",
                f"Unable to read the current wallpaper display style: {exc}",
            ) from exc

        wallpaper_style_text = self._registry_text(wallpaper_style)
        tile_wallpaper_text = self._registry_text(tile_wallpaper)
        snapshot = WallpaperSnapshot(
            path=Path(wallpaper_path) if wallpaper_path else None,
            style=style_from_windows_values(
                wallpaper_style_text,
                tile_wallpaper_text,
            ),
            wallpaper_style_value=wallpaper_style_text,
            tile_wallpaper_value=tile_wallpaper_text,
        )
        return _CurrentWallpaperState(
            snapshot=snapshot,
            wallpaper_style=wallpaper_style,
            tile_wallpaper=tile_wallpaper,
        )

    @staticmethod
    def _registry_text(snapshot: RegistryValueSnapshot) -> str | None:
        if not snapshot.exists or snapshot.value is None:
            return None
        return str(snapshot.value)

    def _restore_registry_values(self, state: _CurrentWallpaperState) -> None:
        self._registry.restore_value(
            WALLPAPER_STYLE_VALUE,
            state.wallpaper_style,
        )
        self._registry.restore_value(
            TILE_WALLPAPER_VALUE,
            state.tile_wallpaper,
        )

    def _rollback_registry(self, state: _CurrentWallpaperState) -> list[str]:
        errors: list[str] = []
        for name, snapshot in (
            (WALLPAPER_STYLE_VALUE, state.wallpaper_style),
            (TILE_WALLPAPER_VALUE, state.tile_wallpaper),
        ):
            try:
                self._registry.restore_value(name, snapshot)
            except Exception as exc:
                errors.append(f"registry value {name}: {exc}")
        return errors

    def _rollback_system(self, state: _CurrentWallpaperState) -> list[str]:
        try:
            self._system.set_wallpaper(self._snapshot_path(state.snapshot))
        except Exception as exc:
            return [f"desktop wallpaper: {exc}"]
        return []

    def _remember_failed_change(self, state: _CurrentWallpaperState) -> None:
        if self._restore_point is None:
            self._restore_point = state

    @staticmethod
    def _snapshot_path(snapshot: WallpaperSnapshot) -> str:
        return str(snapshot.path) if snapshot.path is not None else ""

    @staticmethod
    def _failure_with_rollback(
        code: str,
        primary_message: str,
        rollback_errors: list[str],
    ) -> OperationResult:
        message = primary_message
        if rollback_errors:
            message += " Rollback also failed: " + "; ".join(rollback_errors)
        return OperationResult(False, code, message)


class CtypesWallpaperSystemBackend:
    """Thin, lazily loaded wrapper around ``SystemParametersInfoW``."""

    def __init__(self, system_parameters_info: Any | None = None) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        if system_parameters_info is None:
            win_dll = getattr(ctypes, "WinDLL", None)
            if win_dll is None:
                raise OSError("ctypes.WinDLL is unavailable on this platform.")
            user32 = win_dll("user32.dll", use_last_error=True)
            system_parameters_info = user32.SystemParametersInfoW

        self._call = system_parameters_info
        self._call.argtypes = [
            wintypes.UINT,
            wintypes.UINT,
            wintypes.LPVOID,
            wintypes.UINT,
        ]
        self._call.restype = wintypes.BOOL

    def get_wallpaper(self) -> str:
        buffer = self._ctypes.create_unicode_buffer(MAX_WALLPAPER_PATH_CHARS)
        self._clear_last_error()
        succeeded = self._call(
            SPI_GETDESKWALLPAPER,
            MAX_WALLPAPER_PATH_CHARS,
            self._ctypes.cast(buffer, self._wintypes.LPVOID),
            0,
        )
        if not succeeded:
            self._raise_last_error("SystemParametersInfoW GET failed")
        return buffer.value

    def set_wallpaper(self, absolute_path: str) -> None:
        path_pointer = self._ctypes.c_wchar_p(absolute_path)
        self._clear_last_error()
        succeeded = self._call(
            SPI_SETDESKWALLPAPER,
            0,
            self._ctypes.cast(path_pointer, self._wintypes.LPVOID),
            SET_WALLPAPER_FLAGS,
        )
        if not succeeded:
            self._raise_last_error("SystemParametersInfoW SET failed")

    def _clear_last_error(self) -> None:
        setter = getattr(self._ctypes, "set_last_error", None)
        if setter is not None:
            setter(0)

    def _raise_last_error(self, prefix: str) -> None:
        getter = getattr(self._ctypes, "get_last_error", None)
        error_code = getter() if getter is not None else 0
        formatter = getattr(self._ctypes, "FormatError", None)
        detail = formatter(error_code).strip() if formatter else "Unknown error"
        raise OSError(error_code, f"{prefix}: {detail}")


class WinRegistryWallpaperBackend:
    """Lazily loaded HKCU wallpaper display-style storage."""

    KEY_PATH = r"Control Panel\Desktop"

    def __init__(self, winreg_module: Any | None = None) -> None:
        if winreg_module is None:
            import importlib

            winreg_module = importlib.import_module("winreg")
        self._winreg = winreg_module

    def read_value(self, name: str) -> RegistryValueSnapshot:
        try:
            key = self._winreg.OpenKey(
                self._winreg.HKEY_CURRENT_USER,
                self.KEY_PATH,
                0,
                self._winreg.KEY_QUERY_VALUE,
            )
        except FileNotFoundError:
            return RegistryValueSnapshot(False)

        try:
            try:
                value, value_type = self._winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                return RegistryValueSnapshot(False)
            return RegistryValueSnapshot(True, value, value_type)
        finally:
            self._winreg.CloseKey(key)

    def set_string_value(self, name: str, value: str) -> None:
        key = self._open_write_key()
        try:
            self._winreg.SetValueEx(key, name, 0, self._winreg.REG_SZ, value)
        finally:
            self._winreg.CloseKey(key)

    def restore_value(self, name: str, snapshot: RegistryValueSnapshot) -> None:
        key = self._open_write_key()
        try:
            if snapshot.exists:
                if snapshot.value_type is None:
                    raise ValueError(f"Registry type for {name} is unavailable.")
                self._winreg.SetValueEx(
                    key,
                    name,
                    0,
                    snapshot.value_type,
                    snapshot.value,
                )
            else:
                try:
                    self._winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
        finally:
            self._winreg.CloseKey(key)

    def _open_write_key(self) -> Any:
        return self._winreg.OpenKey(
            self._winreg.HKEY_CURRENT_USER,
            self.KEY_PATH,
            0,
            self._winreg.KEY_SET_VALUE,
        )


def create_default_windows_wallpaper_service(
    *,
    is_windows: bool | None = None,
) -> WindowsWallpaperService | UnavailableWallpaperService:
    """Build the real Windows service without importing Windows APIs elsewhere."""

    running_on_windows = os.name == "nt" if is_windows is None else is_windows
    if not running_on_windows:
        return UnavailableWallpaperService(
            ServiceAvailability.unavailable(
                UNSUPPORTED_PLATFORM,
                "Windows wallpaper APIs are unavailable on this operating system.",
            )
        )
    try:
        system_backend = CtypesWallpaperSystemBackend()
        registry_backend = WinRegistryWallpaperBackend()
    except Exception as exc:
        return UnavailableWallpaperService(
            ServiceAvailability.unavailable(
                "windows_api_unavailable",
                f"Windows wallpaper APIs could not be initialized: {exc}",
            )
        )
    return WindowsWallpaperService(system_backend, registry_backend)
