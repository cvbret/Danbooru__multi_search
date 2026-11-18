"""Immutable single-post Eagle input and result values."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from danbooru_multi_search.danbooru.models import DanbooruPost


class EaglePostImportError(RuntimeError):
    """A checked single-post import failure with its stable diagnostic code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalized_post_url(value: str, post_id: int | None = None) -> str | None:
    """Compare an exact post path, never a substring or a search result label."""
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        port = parsed.port
        host = parsed.hostname.lower()
        if ":" in host:
            host = "[" + host + "]"
        if port is not None and (parsed.scheme, port) not in {("http", 80), ("https", 443)}:
            host += f":{port}"
        path = parsed.path.rstrip("/")
        if post_id is not None and not path.endswith(f"/posts/{post_id}"):
            return None
        return urlunsplit((parsed.scheme, host, path, "", ""))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class EaglePostImportRequest:
    library_identifier: str
    folder_identifier: str | None
    post: DanbooruPost
    post_url: str
    file_path: Path | None = None
    update_duplicates: bool = False
    include_general: bool = False
    include_meta: bool = False
    include_chinese: bool = False
    translations: tuple[tuple[str, tuple[str, ...]], ...] = ()
    query_text: str = ""
    pool_text: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.library_identifier, str) or not self.library_identifier.strip():
            raise ValueError("A captured Eagle library identifier is required.")
        if self.folder_identifier is not None and (not isinstance(self.folder_identifier, str) or not self.folder_identifier.strip()):
            raise ValueError("The captured Eagle folder identifier is invalid.")
        if not isinstance(self.post, DanbooruPost) or type(self.post.id) is not int or not 1 <= self.post.id <= 2**63 - 1:
            raise ValueError("A valid captured Danbooru post is required.")
        if normalized_post_url(self.post_url, self.post.id) is None:
            raise ValueError("An exact Danbooru post URL is required.")
        if self.file_path is not None and not isinstance(self.file_path, Path):
            raise ValueError("The import file path must be a Path.")
        for name in ("update_duplicates", "include_general", "include_meta", "include_chinese"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean.")
        if not isinstance(self.query_text, str) or not isinstance(self.pool_text, str):
            raise ValueError("Query and pool context must be text.")
        if not isinstance(self.translations, tuple):
            raise ValueError("Translations must be an immutable tuple.")
        for pair in self.translations:
            if not isinstance(pair, tuple) or len(pair) != 2 or not isinstance(pair[0], str):
                raise ValueError("Invalid translation entry.")
            if not isinstance(pair[1], tuple) or any(not isinstance(name, str) or not name for name in pair[1]):
                raise ValueError("Translation aliases must be immutable non-empty text.")


@dataclass(frozen=True, slots=True)
class EaglePostImportOutcome:
    state: Literal["ready", "skipped", "imported", "updated", "error", "uncertain"]
    item_identifier: str | None = None
    reason: str = ""
    sha256: str | None = None
    file_path: Path | None = None
    recovered_paths: tuple[Path, ...] = ()
    error_code: str = ""
    diagnostic: str = ""
    post_started: bool = False
    uncertain: bool = False

    def __post_init__(self) -> None:
        if type(self.state) is not str or self.state not in {"ready", "skipped", "imported", "updated", "error", "uncertain"}:
            raise ValueError("Invalid Eagle post import outcome.")
        if self.state in {"skipped", "imported", "updated"} and (not isinstance(self.item_identifier, str) or not self.item_identifier.strip()):
            raise ValueError("A confirmed Eagle item identifier is required.")
