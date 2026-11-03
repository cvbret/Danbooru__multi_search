"""Resolve application directories without hard-coded operating-system paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QStandardPaths

from danbooru_multi_search.platform.contracts import PlatformPaths


@dataclass(frozen=True, slots=True)
class ApplicationPaths:
    config_dir: Path
    data_dir: Path
    cache_dir: Path
    log_dir: Path
    temp_dir: Path
    download_dir: Path
    settings_file: Path
    database_file: Path

    def ensure_directories(self) -> None:
        """Create only application-owned directories."""

        for directory in (
            self.config_dir,
            self.data_dir,
            self.cache_dir,
            self.log_dir,
            self.temp_dir,
            self.download_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


def _standard_location(location: QStandardPaths.StandardLocation) -> Path:
    value = QStandardPaths.writableLocation(location)
    if not value:
        raise RuntimeError(f"No writable standard location for {location.name}")
    return Path(value)


@dataclass(frozen=True, slots=True)
class QtPlatformPaths(PlatformPaths):
    """Resolve standard paths through Qt or an isolated development root."""

    override_root: Path | None = None

    def resolve(self) -> ApplicationPaths:
        return _resolve_application_paths(self.override_root)


def _resolve_application_paths(override_root: Path | None = None) -> ApplicationPaths:
    """Build application paths without creating any directories."""

    if override_root is not None:
        root = override_root.expanduser().resolve()
        config_dir = root / "config"
        data_dir = root / "data"
        cache_dir = root / "cache"
        log_dir = root / "logs"
        temp_dir = root / "temp"
        download_dir = root / "downloads"
    else:
        config_dir = _standard_location(
            QStandardPaths.StandardLocation.AppConfigLocation
        )
        data_dir = _standard_location(QStandardPaths.StandardLocation.AppDataLocation)
        cache_dir = _standard_location(QStandardPaths.StandardLocation.CacheLocation)
        log_dir = data_dir / "logs"
        temp_dir = (
            _standard_location(QStandardPaths.StandardLocation.TempLocation)
            / "DanbooruMultiSearch"
        )
        download_dir = _standard_location(
            QStandardPaths.StandardLocation.DownloadLocation
        )

    return ApplicationPaths(
        config_dir=config_dir,
        data_dir=data_dir,
        cache_dir=cache_dir,
        log_dir=log_dir,
        temp_dir=temp_dir,
        download_dir=download_dir,
        settings_file=config_dir / "settings.json",
        database_file=data_dir / "application.sqlite3",
    )


def resolve_application_paths(override_root: Path | None = None) -> ApplicationPaths:
    """Compatibility wrapper around the platform path service."""

    return QtPlatformPaths(override_root=override_root).resolve()

