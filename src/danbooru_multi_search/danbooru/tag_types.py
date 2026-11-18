"""Immutable tag suggestion values shared by parsing, indexes and views."""

from __future__ import annotations

from dataclasses import dataclass, replace


DEFAULT_TAG_SUGGESTION_LIMIT = 20
MAX_TAG_RESPONSE_BYTES = 1024 * 1024

_CATEGORY_LABELS = {
    0: "普通标签",
    1: "画师",
    3: "作品系列",
    4: "角色",
    5: "元数据",
}


@dataclass(frozen=True, slots=True)
class TagSuggestion:
    name: str
    chinese_names: tuple[str, ...] = ()
    post_count: int | None = None
    category: int | None = None

    @property
    def chinese_name(self) -> str | None:
        return self.chinese_names[0] if self.chinese_names else None

    @property
    def chinese_name_text(self) -> str:
        return "、".join(self.chinese_names) if self.chinese_names else "暂无中文名称"

    @property
    def category_text(self) -> str:
        return _CATEGORY_LABELS.get(self.category, "未知分类")

    @property
    def post_count_text(self) -> str:
        return str(self.post_count) if self.post_count is not None else "暂无数据"

    @property
    def has_known_category(self) -> bool:
        return self.category in _CATEGORY_LABELS

    def prefer_chinese_name(self, name: str | None) -> TagSuggestion:
        if not name or name not in self.chinese_names:
            return self
        return replace(
            self,
            chinese_names=(name,) + tuple(
                current for current in self.chinese_names if current != name
            ),
        )
