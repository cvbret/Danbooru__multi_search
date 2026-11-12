"""Immutable saved artist combinations, independent of interface and storage."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from danbooru_multi_search.danbooru.tag_syntax import TagSyntaxError, normalize_tag_name


class ArtistCombinationMode(StrEnum):
    ALL = "all"
    ANY = "any"
    EXCLUDE = "exclude"


class ArtistCombinationError(RuntimeError):
    """One stable validation or persistence failure shown by the interface."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def combination_name(name: str) -> tuple[str, str]:
    """Return a display name and its case-insensitive Unicode identity."""

    if not isinstance(name, str):
        raise ArtistCombinationError("invalid_name", "画师组合名称必须是文本。")
    display = unicodedata.normalize("NFC", name.strip())
    if not display or any(unicodedata.category(char).startswith("C") for char in display):
        raise ArtistCombinationError("invalid_name", "画师组合名称不能为空或包含控制字符。")
    return display, display.casefold()


@dataclass(frozen=True, slots=True)
class ArtistCombinationDraft:
    name: str
    artists: tuple[str, ...]
    mode: ArtistCombinationMode

    def __post_init__(self) -> None:
        display, _key = combination_name(self.name)
        if not isinstance(self.mode, ArtistCombinationMode):
            raise ArtistCombinationError("invalid_mode", "画师组合模式无效。")
        if not isinstance(self.artists, (tuple, list)):
            raise ArtistCombinationError("invalid_artists", "画师组合必须包含画师标签列表。")
        try:
            artists = tuple(dict.fromkeys(normalize_tag_name(tag) for tag in self.artists))
        except (TagSyntaxError, TypeError, ValueError) as exc:
            raise ArtistCombinationError("invalid_artists", f"画师标签无效：{exc}") from exc
        if not artists:
            raise ArtistCombinationError("empty_artists", "画师组合至少需要一个画师标签。")
        object.__setattr__(self, "name", display)
        object.__setattr__(self, "artists", artists)


@dataclass(frozen=True, slots=True)
class ArtistCombination:
    combination_id: int
    name: str
    artists: tuple[str, ...]
    mode: ArtistCombinationMode

    def __post_init__(self) -> None:
        if type(self.combination_id) is not int or not 1 <= self.combination_id <= 2**63 - 1:
            raise ArtistCombinationError("invalid_identifier", "画师组合编号无效。")
        draft = ArtistCombinationDraft(self.name, self.artists, self.mode)
        object.__setattr__(self, "name", draft.name)
        object.__setattr__(self, "artists", draft.artists)
