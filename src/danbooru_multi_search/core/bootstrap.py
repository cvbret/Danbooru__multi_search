"""Platform-neutral application initialization."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from danbooru_multi_search.core.settings import (
    AppSettings,
    SettingsStore,
    create_default_settings,
)
from danbooru_multi_search.platform import (
    ApplicationPaths,
    PlatformAdapter,
    create_platform_adapter,
)
from danbooru_multi_search.storage.database import Database


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Services and startup state shared by CLI and GUI entry points."""

    paths: ApplicationPaths
    platform: PlatformAdapter
    settings: AppSettings
    settings_created: bool
    database: Database
    database_version: int


def initialize_runtime(data_root: Path | None = None) -> RuntimeContext:
    """Create required directories, settings, and database schema."""

    platform_adapter = create_platform_adapter(override_root=data_root)
    paths = platform_adapter.services.paths.resolve()
    paths.ensure_directories()

    settings_store = SettingsStore(paths.settings_file)
    settings_created = not settings_store.exists()
    default_settings = create_default_settings(paths.download_dir)
    settings = settings_store.load_or_default(default_settings)
    if settings_created:
        settings_store.save(settings)

    database = Database(paths.database_file)
    database_version = database.initialize()

    return RuntimeContext(
        paths=paths,
        platform=platform_adapter,
        settings=settings,
        settings_created=settings_created,
        database=database,
        database_version=database_version,
    )

