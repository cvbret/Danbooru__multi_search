"""Versioned JSON settings with atomic persistence."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Mapping


class SettingsError(RuntimeError):
    """Raised when a settings file cannot be read or validated."""


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Settings required by the M0 application shell."""

    schema_version: int = 1
    language: str = "zh-CN"
    theme: str = "system"
    page_size: int = 20
    danbooru_api_url: str = "https://danbooru.donmai.us"
    download_directory: str = ""

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
        if not 1 <= settings.page_size <= 100:
            raise SettingsError("page_size must be between 1 and 100")
        if not isinstance(settings.danbooru_api_url, str):
            raise SettingsError("danbooru_api_url must be a string")
        if not settings.danbooru_api_url.startswith(("https://", "http://")):
            raise SettingsError("danbooru_api_url must be an HTTP or HTTPS URL")
        if not isinstance(settings.download_directory, str):
            raise SettingsError("download_directory must be a string")
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

