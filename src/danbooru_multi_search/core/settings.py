"""Versioned JSON settings with atomic persistence."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Mapping


DEFAULT_IMAGE_CACHE_LIMIT_BYTES = 512 * 1024 * 1024
MIN_IMAGE_CACHE_LIMIT_BYTES = 64 * 1024 * 1024
MAX_IMAGE_CACHE_LIMIT_BYTES = 16 * 1024 * 1024 * 1024


class SettingsError(RuntimeError):
    """Raised when a settings file cannot be read or validated."""


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Version 1 application settings with backward-compatible defaults."""

    schema_version: int = 1
    language: str = "zh-CN"
    theme: str = "system"
    page_size: int = 40
    danbooru_api_url: str = "https://danbooru.donmai.us"
    download_directory: str = ""
    tag_csv_path: str = ""
    image_cache_limit_bytes: int = DEFAULT_IMAGE_CACHE_LIMIT_BYTES

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AppSettings":
        """Build settings while ignoring fields introduced by newer versions."""

        known_names = {field.name for field in fields(cls)}
        values = {name: raw[name] for name in known_names if name in raw}

        try:
            settings = cls(**values)
        except TypeError as exc:
            raise SettingsError(f"Invalid settings fields: {exc}") from exc

        if not isinstance(settings.schema_version, int) or isinstance(
            settings.schema_version, bool
        ):
            raise SettingsError("schema_version must be an integer")
        if settings.schema_version != 1:
            raise SettingsError(
                f"Unsupported settings schema version: {settings.schema_version}"
            )
        if not isinstance(settings.language, str) or not settings.language:
            raise SettingsError("language must be a non-empty string")
        if not isinstance(settings.theme, str):
            raise SettingsError("theme must be a string")
        if settings.theme not in {"system", "light", "dark"}:
            raise SettingsError(f"Unsupported theme: {settings.theme}")
        if not isinstance(settings.page_size, int) or isinstance(settings.page_size, bool):
            raise SettingsError("page_size must be an integer")
        if not 1 <= settings.page_size <= 200:
            raise SettingsError("page_size must be between 1 and 200")
        if not isinstance(settings.danbooru_api_url, str):
            raise SettingsError("danbooru_api_url must be a string")
        if not settings.danbooru_api_url.startswith(("https://", "http://")):
            raise SettingsError("danbooru_api_url must be an HTTP or HTTPS URL")
        if not isinstance(settings.download_directory, str):
            raise SettingsError("download_directory must be a string")
        if not isinstance(settings.tag_csv_path, str):
            raise SettingsError("tag_csv_path must be a string")
        if settings.tag_csv_path and not Path(settings.tag_csv_path).is_absolute():
            raise SettingsError("tag_csv_path must be an absolute path or empty")
        if not isinstance(settings.image_cache_limit_bytes, int) or isinstance(
            settings.image_cache_limit_bytes, bool
        ):
            raise SettingsError("image_cache_limit_bytes must be an integer")
        if not (
            MIN_IMAGE_CACHE_LIMIT_BYTES
            <= settings.image_cache_limit_bytes
            <= MAX_IMAGE_CACHE_LIMIT_BYTES
        ):
            raise SettingsError(
                "image_cache_limit_bytes must be between 64 MiB and 16 GiB"
            )
        return settings

    def to_mapping(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""

        return asdict(self)


def create_default_settings(download_dir: Path) -> AppSettings:
    """Create platform-derived default settings."""

    return AppSettings(download_directory=str(download_dir))


class SettingsStore:
    """Read and atomically replace a JSON settings file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def exists(self) -> bool:
        return self.path.is_file()

    def load_or_default(self, default: AppSettings) -> AppSettings:
        if not self.exists():
            return default

        try:
            with self.path.open("r", encoding="utf-8") as stream:
                raw = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise SettingsError(f"Unable to read settings at {self.path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise SettingsError("Settings root must be a JSON object")
        return AppSettings.from_mapping(raw)

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(
                    settings.to_mapping(),
                    stream,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())

            os.replace(temporary_path, self.path)
        except OSError as exc:
            raise SettingsError(f"Unable to save settings at {self.path}: {exc}") from exc
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)


class SettingsController:
    """Serialize merged settings updates around one in-memory snapshot."""

    def __init__(
        self,
        store: SettingsStore,
        settings: AppSettings,
    ) -> None:
        self._store = store
        self._settings = settings
        self._lock = threading.RLock()

    @property
    def settings(self) -> AppSettings:
        """Return the latest successfully persisted immutable settings."""

        with self._lock:
            return self._settings

    def update(self, **changes: Any) -> AppSettings:
        """Merge, validate, and atomically persist fields as one serialized update."""

        with self._lock:
            merged = self._settings.to_mapping()
            merged.update(changes)
            updated = AppSettings.from_mapping(merged)
            self._store.save(updated)
            self._settings = updated
            return updated

    def set_tag_csv_path(self, path: Path) -> AppSettings:
        """Persist the absolute path of the last successful CSV import."""

        local_path = Path(path)
        if not local_path.is_absolute():
            raise SettingsError("tag CSV path must be absolute")
        return self.update(tag_csv_path=str(local_path))

    def set_image_cache_limit_bytes(self, value: int) -> AppSettings:
        """Persist the image disk-cache capacity after shared validation."""

        return self.update(image_cache_limit_bytes=value)

