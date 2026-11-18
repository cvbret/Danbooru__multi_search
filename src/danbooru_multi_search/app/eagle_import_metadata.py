"""Build Eagle metadata and recognize complete application-owned note blocks."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from datetime import datetime, timezone
import json
import re

from danbooru_multi_search.app.eagle_import_types import (
    EaglePostImportError,
    EaglePostImportRequest,
    normalized_post_url,
)
from danbooru_multi_search.danbooru.post_validation import validate_post_values
from danbooru_multi_search.danbooru.models import MediaQuality


_BEGIN = "[DanbooruMultiSearch:v1]"
_END = "[/DanbooruMultiSearch:v1]"
_BLOCK = re.compile(r"^\[DanbooruMultiSearch:v1\]\r?\n(.*?)^\[/DanbooruMultiSearch:v1\](?=\r?$)", re.MULTILINE | re.DOTALL)
_HASH = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class EagleAnnotationMarker:
    library_id: str
    post_id: int
    page_url: str
    sha256: str | None
    attempt_token: str
    start: int
    end: int
    source_sha256: str | None = None


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("Duplicate annotation key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(value)


def annotation_markers(value: str) -> tuple[EagleAnnotationMarker, ...]:
    """Ignore arbitrary substrings and malformed or mismatched marker bodies."""
    result = []
    for match in _BLOCK.finditer(value):
        if len(match.group(1)) > 128 * 1024:
            continue
        try:
            data = json.loads(
                match.group(1), object_pairs_hook=_pairs,
                parse_constant=_reject_constant,
            )
            if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != 1:
                continue
            library = data.get("library_id")
            post_id = data.get("post_id")
            token = data.get("attempt_token")
            digest = data.get("sha256")
            source_digest = data.get("source_sha256", digest)
            if not isinstance(library, str) or not library:
                continue
            if not isinstance(post_id, str) or not post_id.isascii() or not post_id.isdecimal() or len(post_id) > 19:
                continue
            number = int(post_id)
            if not 1 <= number <= 2**63 - 1:
                continue
            page_url = normalized_post_url(data.get("post_url"), number)
            if page_url is None or not isinstance(token, str) or not token:
                continue
            if digest is not None and (not isinstance(digest, str) or _HASH.fullmatch(digest) is None):
                continue
            if source_digest is not None and (not isinstance(source_digest, str) or _HASH.fullmatch(source_digest) is None):
                continue
            result.append(EagleAnnotationMarker(
                library, number, page_url, digest, token, match.start(), match.end(), source_digest,
            ))
        except (ValueError, TypeError, RecursionError):
            continue
    return tuple(result)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    output = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise EaglePostImportError("eagle_invalid_metadata", "A tag is not non-empty text.")
        if value not in seen:
            seen.add(value)
            output.append(value)
    return tuple(output)


def import_tags(request: EaglePostImportRequest) -> tuple[str, ...]:
    post = request.post
    try:
        validate_post_values(post)
    except (ValueError, TypeError, OverflowError) as exc:
        raise EaglePostImportError("eagle_invalid_metadata", str(exc)) from exc
    values = ["danbooru", *post.artist_tags, *post.character_tags, *post.copyright_tags]
    if request.include_general:
        values.extend(post.general_tags)
    if request.include_meta:
        values.extend(post.meta_tags)
    english = _unique(values)
    if not request.include_chinese:
        return english
    translations = dict(request.translations)
    return _unique((*english, *(alias for tag in english for alias in translations.get(tag, ()))))


def merged_tags(existing: tuple[str, ...], incoming: tuple[str, ...]) -> tuple[str, ...]:
    """Keep the user's values and append only missing application tags."""
    return _unique((*existing, *incoming))


def build_annotation(
    request: EaglePostImportRequest,
    *,
    sha256: str | None,
    attempt_token: str,
    source_sha256: str | None = None,
    previous: str = "",
    quality: MediaQuality | None = None,
    extension: str | None = None,
    creating: bool = False,
) -> str:
    """Replace this post's complete block, preserving user text and other posts."""
    post = request.post
    page_url = normalized_post_url(request.post_url, post.id)
    data = {
        "schema": 1,
        "library_id": request.library_identifier,
        "post_id": str(post.id),
        "post_url": page_url,
        "sha256": sha256,
        "source_sha256": source_sha256 if source_sha256 is not None else sha256,
        "attempt_token": attempt_token,
        "details": {
            "Danbooru作品编号": str(post.id),
            "原始来源地址": post.source or "未取得",
            "上传时间": post.created_at or "未取得",
            "内容分级": post.rating,
            "score": post.score,
            "fav_count": post.fav_count,
            "图片尺寸": f"{post.width} × {post.height}",
            "文件格式": post.file_ext,
            "所属作品池": request.pool_text or "未取得",
            "查询条件": request.query_text or "未取得",
            "导入时间": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    }
    if quality is not None and extension is not None:
        data["details"]["本次采集清晰度"] = quality.label
        data["details"]["本次采集文件格式"] = extension
        if creating:
            data["details"]["首次导入清晰度"] = quality.label
            data["details"]["首次导入文件格式"] = extension
    matching = [
        marker for marker in annotation_markers(previous)
        if marker.library_id == request.library_identifier
        and marker.post_id == post.id and marker.page_url == page_url
    ]
    if len(matching) > 1:
        raise EaglePostImportError("eagle_duplicate_conflict", "Multiple note blocks claim the same imported post.")
    if matching:
        marker = matching[0]
        matched = _BLOCK.match(previous, marker.start)
        old_data = json.loads(matched.group(1))
        old_details = old_data.get("details")
        if isinstance(old_details, dict):
            for key, supplied in (
                ("导入时间", False),
                ("首次导入清晰度", False),
                ("首次导入文件格式", False),
                ("查询条件", bool(request.query_text and request.query_text != "未取得")),
                ("所属作品池", bool(request.pool_text and request.pool_text != "未取得")),
            ):
                old_value = old_details.get(key)
                if not supplied and isinstance(old_value, str) and old_value and old_value != "未取得":
                    data["details"][key] = old_value
        block = _BEGIN + "\n" + json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n" + _END
        return previous[:marker.start] + block + previous[marker.end:]
    block = _BEGIN + "\n" + json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n" + _END
    return previous + ("\n\n" if previous else "") + block


def import_name(request: EaglePostImportRequest) -> str:
    artists = request.post.artist_tags
    return f"{request.post.id}_{artists[0]}" if artists else str(request.post.id)
