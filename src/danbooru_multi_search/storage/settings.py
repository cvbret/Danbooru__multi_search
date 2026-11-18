"""Read and atomically replace versioned application settings."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from danbooru_multi_search.core.settings import AppSettings, SettingsError


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
