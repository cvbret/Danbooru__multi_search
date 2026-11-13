"""Immutable named logical queries without storage or Qt dependencies."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from danbooru_multi_search.danbooru.query_definition import GalleryQueryDefinition


class SavedQueryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def saved_query_name(value: str) -> tuple[str, str]:
    """Use the same NFC, whitespace and case rules as other named local objects."""
    if not isinstance(value, str):
        raise SavedQueryError("invalid_name", "常用查询名称必须是文本。")
    display = unicodedata.normalize("NFC", value.strip())
    if not display or any(unicodedata.category(char).startswith("C") for char in display):
        raise SavedQueryError("invalid_name", "常用查询名称不能为空或包含控制字符。")
    return display, display.casefold()


@dataclass(frozen=True, slots=True)
class SavedQueryDraft:
    name: str
    definition: GalleryQueryDefinition

    def __post_init__(self) -> None:
        name, _key = saved_query_name(self.name)
        if not isinstance(self.definition, GalleryQueryDefinition):
            raise SavedQueryError("invalid_definition", "常用查询缺少有效的完整查询条件。")
        object.__setattr__(self, "name", name)


@dataclass(frozen=True, slots=True)
class SavedQuery:
    query_id: str
    revision: int
    name: str
    definition: GalleryQueryDefinition

    def __post_init__(self) -> None:
        if not isinstance(self.query_id, str) or re.fullmatch(r"[a-f0-9]{32}", self.query_id) is None:
            raise SavedQueryError("invalid_identifier", "常用查询编号无效。")
        if type(self.revision) is not int or not 0 <= self.revision <= 2**63 - 1:
            raise SavedQueryError("invalid_revision", "常用查询修订号无效。")
        draft = SavedQueryDraft(self.name, self.definition)
        object.__setattr__(self, "name", draft.name)
