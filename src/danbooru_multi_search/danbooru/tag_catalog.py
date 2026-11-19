"""Browse queryable tags from the local index, gallery, favorites and Danbooru."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable, Iterator, Mapping
from itertools import chain

from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.danbooru.query_language import AndNode, NotNode, OrNode, TagNode, parse_query_expression
from danbooru_multi_search.danbooru.tag_index import TagIndexSnapshot
from danbooru_multi_search.danbooru.tag_syntax import normalize_tag_name
from danbooru_multi_search.danbooru.tag_types import TagSuggestion


CATEGORY_CODES = {"general": 0, "artist": 1, "character": 4, "copyright": 3}
CATEGORY_NAMES = {code: name for name, code in CATEGORY_CODES.items()}

# A small starting selection for an empty installation. Counts belong to the
# user's CSV or a Danbooru response and are never inferred from this list.
_STARTING_TAGS = {
    "general": (
        ("solo", "单人"), ("landscape", "风景"), ("sky", "天空"), ("flower", "花"),
        ("long_hair", "长发"), ("short_hair", "短发"), ("smile", "微笑"), ("blue_eyes", "蓝眼"),
        ("cat", "猫"), ("dog", "狗"), ("water", "水"), ("night", "夜晚"),
        ("cloud", "云"), ("tree", "树"), ("sunset", "日落"), ("snow", "雪"),
        ("scenery", "景色"), ("cityscape", "城市景观"), ("ocean", "海洋"), ("rain", "雨"),
    ),
    "artist": (
        ("kantoku", "监督"), ("wada_arco", ""), ("takeuchi_takashi", "武内崇"),
        ("redjuice", ""), ("wlop", ""), ("toi8", ""), ("kuroboshi_kouhaku", "黑星红白"),
        ("yoshida_akihiko", "吉田明彦"), ("toriyama_akira", "鸟山明"), ("lack", ""),
    ),
    "character": (
        ("hatsune_miku", "初音未来"), ("hakurei_reimu", "博丽灵梦"), ("kirisame_marisa", "雾雨魔理沙"),
        ("remilia_scarlet", "蕾米莉亚"), ("flandre_scarlet", "芙兰朵露"), ("izayoi_sakuya", "十六夜咲夜"),
        ("kagamine_rin", "镜音铃"), ("kagamine_len", "镜音连"), ("megurine_luka", "巡音流歌"),
        ("pikachu", "皮卡丘"), ("link", "林克"), ("princess_zelda", "塞尔达公主"),
    ),
    "copyright": (
        ("touhou", "东方Project"), ("vocaloid", "虚拟歌手"), ("pokemon", "宝可梦"),
        ("genshin_impact", "原神"), ("arknights", "明日方舟"), ("blue_archive", "蔚蓝档案"),
        ("azur_lane", "碧蓝航线"), ("kantai_collection", "舰队收藏"), ("fate/grand_order", "命运冠位指定"),
        ("the_legend_of_zelda", "塞尔达传说"), ("final_fantasy", "最终幻想"), ("splatoon", "斯普拉遁"),
    ),
}
STARTING_TAGS = {
    name: TagSuggestion(name, (chinese,) if chinese else (), category=CATEGORY_CODES[category])
    for category, values in _STARTING_TAGS.items() for name, chinese in values
}


def gallery_tags(posts: tuple[DanbooruPost, ...]) -> dict[str, TagSuggestion]:
    tags = {}
    for post in posts:
        for category, code in CATEGORY_CODES.items():
            for name in getattr(post, category + "_tags"):
                if name not in tags:
                    try:
                        normalized = normalize_tag_name(name)
                    except ValueError:
                        continue
                    tags[normalized] = TagSuggestion(normalized, category=code)
    return tags


def _index_records(index: TagIndexSnapshot, term: str) -> Iterable[TagSuggestion]:
    if not term:
        return index.records
    if not term.isascii():
        # Narrow by the rarest Chinese character. The caller checks the full
        # alias and stops once the requested page is full.
        postings = (index.chinese_character_index.get(char, ()) for char in set(term) if not char.isascii())
        candidates = min(postings, key=len)
        return (index.records[i] for i in chain(index.chinese_exact.get(term, ()), sorted(candidates)))
    start = bisect_left(index.sorted_english_names, term)
    stop = bisect_left(index.sorted_english_names, term + "\uffff")
    return (index.records_by_name[index.sorted_english_names[i]] for i in range(start, stop))


def browse_tags(
    category: str, term: str, index: TagIndexSnapshot, favorite: Mapping[str, TagSuggestion],
    gallery: Mapping[str, TagSuggestion], online: Mapping[str, TagSuggestion],
) -> Iterator[TagSuggestion]:
    """Yield distinct tags in the chosen category, enriching entries by canonical name."""
    term = term.strip().casefold()
    candidates = favorite.values() if category == "favorites" else chain(
        online.values(), favorite.values(), STARTING_TAGS.values(), gallery.values(),
        (record for record in _index_records(index, term) if record.category == CATEGORY_CODES[category]),
    )
    seen: set[str] = set()
    for entry in candidates:
        name = entry.name
        if name in seen:
            continue
        seen.add(name)
        local = index.records_by_name.get(name)
        remote = online.get(name)
        code = next((item.category for item in (remote, local, gallery.get(name), favorite.get(name), entry)
                     if item is not None and item.category is not None), None)
        if category == "favorites":
            code = favorite[name].category
        if code not in CATEGORY_NAMES or (category != "favorites" and code != CATEGORY_CODES[category]):
            continue
        names = tuple(dict.fromkeys(alias for item in (local, remote, STARTING_TAGS.get(name)) if item
                                    for alias in item.chinese_names))
        if term and not name.startswith(term) and not any(term in alias.casefold() for alias in names):
            continue
        count = next((item.post_count for item in (remote, local) if item and item.post_count is not None), None)
        yield TagSuggestion(name, names, count, code)


def selection_expression(groups: Mapping[str, str]) -> str:
    """Turn the three visible groups into one expression for the existing query editor."""
    terms = [name for name, group in groups.items() if group == "all"]
    alternatives = [name for name, group in groups.items() if group == "any"]
    exclusions = ["NOT " + name for name, group in groups.items() if group == "exclude"]
    if alternatives:
        clause = " OR ".join(alternatives)
        terms.append(f"({clause})" if len(alternatives) > 1 and (terms or exclusions) else clause)
    return " AND ".join(terms + exclusions)


def selection_groups(expression: str) -> dict[str, str] | None:
    """Read exactly the AST shapes the three picker groups can express."""
    if not expression.strip():
        return {}
    root = parse_query_expression(expression).root
    result: dict[str, str] = {}
    has_alternatives = False
    for clause in root.operands if isinstance(root, AndNode) else (root,):
        if isinstance(clause, TagNode):
            tags, group = (clause,), "all"
        elif isinstance(clause, NotNode) and isinstance(clause.operand, TagNode):
            tags, group = (clause.operand,), "exclude"
        elif isinstance(clause, OrNode) and not has_alternatives and all(isinstance(node, TagNode) for node in clause.operands):
            tags, group = clause.operands, "any"
            has_alternatives = True
        else:
            return None
        for tag in tags:
            if tag.name in result:
                return None
            result[tag.name] = group
    return result
