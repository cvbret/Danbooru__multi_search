"""Bounded CSV import and immutable in-memory tag indexes."""

from __future__ import annotations

import codecs
import csv
import io
import re
import threading
from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from danbooru_multi_search.danbooru.tag_types import (
    DEFAULT_TAG_SUGGESTION_LIMIT,
    TagSuggestion,
)
from danbooru_multi_search.danbooru.tag_syntax import (
    normalize_tag_fragment,
    normalize_tag_name,
)


MAX_TAG_CSV_BYTES = 32 * 1024 * 1024
MAX_TAG_CSV_RECORDS = 200_000
MAX_TAG_CSV_FIELD_CHARACTERS = 64 * 1024
_READ_CHUNK_BYTES = 1024 * 1024
_REQUIRED_COLUMNS = ("name", "cn_name", "post_count", "category")
_KNOWN_CATEGORIES = frozenset({0, 1, 3, 4, 5})
_NON_NEGATIVE_DECIMAL = re.compile(r"[0-9]+")


class TagCsvImportError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TagCsvImportCancelled(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TagIndexSnapshot:
    records: tuple[TagSuggestion, ...]
    sorted_english_names: tuple[str, ...]
    records_by_name: Mapping[str, TagSuggestion]
    chinese_exact: Mapping[str, tuple[int, ...]]
    chinese_character_index: Mapping[str, frozenset[int]]

    @classmethod
    def empty(cls) -> TagIndexSnapshot:
        return cls(
            records=(),
            sorted_english_names=(),
            records_by_name=MappingProxyType({}),
            chinese_exact=MappingProxyType({}),
            chinese_character_index=MappingProxyType({}),
        )

    @classmethod
    def build(
        cls,
        records: tuple[TagSuggestion, ...],
        *,
        cancel_event: threading.Event | None = None,
    ) -> TagIndexSnapshot:
        ordered = tuple(sorted(records, key=lambda item: item.name))
        by_name = {record.name: record for record in ordered}
        exact: dict[str, list[int]] = {}
        character_index: dict[str, set[int]] = {}
        for index, record in enumerate(ordered):
            _raise_if_cancelled(cancel_event)
            for chinese_name in record.chinese_names:
                exact.setdefault(chinese_name, []).append(index)
                for character in set(chinese_name):
                    character_index.setdefault(character, set()).add(index)
        return cls(
            records=ordered,
            sorted_english_names=tuple(record.name for record in ordered),
            records_by_name=MappingProxyType(by_name),
            chinese_exact=MappingProxyType(
                {name: tuple(indices) for name, indices in exact.items()}
            ),
            chinese_character_index=MappingProxyType(
                {
                    character: frozenset(indices)
                    for character, indices in character_index.items()
                }
            ),
        )

    def search_english(
        self,
        fragment: str,
        *,
        limit: int = DEFAULT_TAG_SUGGESTION_LIMIT,
    ) -> tuple[TagSuggestion, ...]:
        try:
            normalized = normalize_tag_fragment(fragment)
        except ValueError:
            return ()
        start = bisect_left(self.sorted_english_names, normalized)
        matches: list[TagSuggestion] = []
        for name in self.sorted_english_names[start:]:
            if not name.startswith(normalized):
                break
            matches.append(self.records_by_name[name])
        matches.sort(key=lambda item: _suggestion_sort_key(item, normalized, False))
        return tuple(matches[:limit])

    def search_chinese(
        self,
        fragment: str,
        *,
        limit: int = DEFAULT_TAG_SUGGESTION_LIMIT,
    ) -> tuple[TagSuggestion, ...]:
        normalized = fragment.strip()
        if not normalized:
            return ()
        candidate_indices: set[int] | None = None
        for character in dict.fromkeys(normalized):
            indices = self.chinese_character_index.get(character)
            if not indices:
                return ()
            if candidate_indices is None:
                candidate_indices = set(indices)
            else:
                candidate_indices.intersection_update(indices)
            if not candidate_indices:
                return ()

        matches: list[TagSuggestion] = []
        for index in candidate_indices or ():
            record = self.records[index]
            exact_names = [
                name for name in record.chinese_names if name == normalized
            ]
            containing_names = [
                name
                for name in record.chinese_names
                if normalized in name and name != normalized
            ]
            matched_name = (
                exact_names[0]
                if exact_names
                else containing_names[0]
                if containing_names
                else None
            )
            if matched_name is not None:
                matches.append(record.prefer_chinese_name(matched_name))
        matches.sort(key=lambda item: _suggestion_sort_key(item, normalized, True))
        return tuple(matches[:limit])


@dataclass(frozen=True, slots=True)
class TagCsvImportResult:
    snapshot: TagIndexSnapshot
    total_records: int
    valid_records: int
    skipped_records: int
    duplicate_records: int
    unique_tags: int
    encoding: str


@dataclass(slots=True)
class _TagAccumulator:
    name: str
    chinese_names: list[str] = field(default_factory=list)
    post_counts: list[int] = field(default_factory=list)
    known_categories: set[int] = field(default_factory=set)

    def add(
        self,
        chinese_names: tuple[str, ...],
        post_count: int | None,
        category: int | None,
    ) -> None:
        for chinese_name in chinese_names:
            if chinese_name not in self.chinese_names:
                self.chinese_names.append(chinese_name)
        if post_count is not None:
            self.post_counts.append(post_count)
        if category in _KNOWN_CATEGORIES:
            self.known_categories.add(category)

    def freeze(self) -> TagSuggestion:
        category = (
            next(iter(self.known_categories))
            if len(self.known_categories) == 1
            else None
        )
        return TagSuggestion(
            name=self.name,
            chinese_names=tuple(self.chinese_names),
            post_count=max(self.post_counts) if self.post_counts else None,
            category=category,
        )


def load_tag_csv(
    path: Path,
    *,
    cancel_event: threading.Event | None = None,
    maximum_bytes: int = MAX_TAG_CSV_BYTES,
    maximum_records: int = MAX_TAG_CSV_RECORDS,
    maximum_field_characters: int = MAX_TAG_CSV_FIELD_CHARACTERS,
) -> TagCsvImportResult:
    """Load a user-selected CSV without mutating an existing snapshot."""

    _validate_limits(maximum_bytes, maximum_records, maximum_field_characters)
    _raise_if_cancelled(cancel_event)
    file_path = Path(path)
    try:
        if not file_path.is_file():
            raise TagCsvImportError(
                "file_not_found",
                "选择的标签 CSV 文件不存在或不是普通文件。",
            )
        declared_size = file_path.stat().st_size
    except TagCsvImportError:
        raise
    except OSError as exc:
        raise TagCsvImportError(
            "file_read_error",
            f"无法读取标签 CSV 文件：{exc}",
        ) from exc
    if declared_size > maximum_bytes:
        raise TagCsvImportError(
            "file_too_large",
            "标签 CSV 文件超过 32 MiB 限制。",
        )

    payload = _read_bounded_file(
        file_path,
        maximum_bytes=maximum_bytes,
        cancel_event=cancel_event,
    )
    text, encoding = _decode_csv(payload)
    _raise_if_cancelled(cancel_event)
    return _parse_csv_text(
        text,
        encoding=encoding,
        maximum_records=maximum_records,
        maximum_field_characters=maximum_field_characters,
        cancel_event=cancel_event,
    )


def _read_bounded_file(
    path: Path,
    *,
    maximum_bytes: int,
    cancel_event: threading.Event | None,
) -> bytes:
    payload = bytearray()
    try:
        with path.open("rb") as stream:
            while True:
                _raise_if_cancelled(cancel_event)
                chunk = stream.read(
                    min(_READ_CHUNK_BYTES, maximum_bytes + 1 - len(payload))
                )
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > maximum_bytes:
                    raise TagCsvImportError(
                        "file_too_large",
                        "标签 CSV 文件超过 32 MiB 限制。",
                    )
    except TagCsvImportError:
        raise
    except OSError as exc:
        raise TagCsvImportError(
            "file_read_error",
            f"无法读取标签 CSV 文件：{exc}",
        ) from exc
    return bytes(payload)


def _decode_csv(payload: bytes) -> tuple[str, str]:
    if payload.startswith(codecs.BOM_UTF8):
        try:
            return payload.decode("utf-8-sig", errors="strict"), "utf-8-sig"
        except UnicodeDecodeError as exc:
            raise TagCsvImportError(
                "decode_error",
                "带 UTF-8 BOM 的标签 CSV 无法按 UTF-8 解码。",
            ) from exc

    for encoding in ("utf-8", "gb18030"):
        try:
            return payload.decode(encoding, errors="strict"), encoding
        except UnicodeDecodeError:
            continue
    raise TagCsvImportError(
        "decode_error",
        "标签 CSV 无法按 UTF-8 或 GB18030 解码。",
    )


def _parse_csv_text(
    text: str,
    *,
    encoding: str,
    maximum_records: int,
    maximum_field_characters: int,
    cancel_event: threading.Event | None,
) -> TagCsvImportResult:
    _validate_csv_quoting(text, cancel_event=cancel_event)
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(reader)
    except StopIteration as exc:
        raise TagCsvImportError(
            "invalid_header",
            "标签 CSV 缺少表头。",
        ) from exc
    except csv.Error as exc:
        raise TagCsvImportError(
            "csv_error",
            f"标签 CSV 格式无效：{exc}",
        ) from exc

    _validate_field_lengths(header, maximum_field_characters)
    normalized_header = [field.strip().lower() for field in header]
    positions: dict[str, int] = {}
    for required in _REQUIRED_COLUMNS:
        occurrences = [
            index
            for index, field_name in enumerate(normalized_header)
            if field_name == required
        ]
        if len(occurrences) != 1:
            raise TagCsvImportError(
                "invalid_header",
                f"标签 CSV 表头必须且只能包含一个 '{required}' 列。",
            )
        positions[required] = occurrences[0]

    accumulators: dict[str, _TagAccumulator] = {}
    total_records = 0
    valid_records = 0
    skipped_records = 0
    duplicate_records = 0
    try:
        for row in reader:
            _raise_if_cancelled(cancel_event)
            total_records += 1
            if total_records > maximum_records:
                raise TagCsvImportError(
                    "too_many_records",
                    "标签 CSV 超过 200,000 条记录限制。",
                )
            _validate_field_lengths(row, maximum_field_characters)
            parsed = _parse_record(row, positions)
            if parsed is None:
                skipped_records += 1
                continue
            name, chinese_names, post_count, category = parsed
            valid_records += 1
            accumulator = accumulators.get(name)
            if accumulator is None:
                accumulator = _TagAccumulator(name)
                accumulators[name] = accumulator
            else:
                duplicate_records += 1
            accumulator.add(chinese_names, post_count, category)
    except csv.Error as exc:
        raise TagCsvImportError(
            "csv_error",
            f"标签 CSV 格式无效：{exc}",
        ) from exc

    if valid_records == 0:
        raise TagCsvImportError(
            "no_valid_records",
            "标签 CSV 中没有有效记录。",
        )
    _raise_if_cancelled(cancel_event)
    records = tuple(accumulator.freeze() for accumulator in accumulators.values())
    snapshot = TagIndexSnapshot.build(records, cancel_event=cancel_event)
    return TagCsvImportResult(
        snapshot=snapshot,
        total_records=total_records,
        valid_records=valid_records,
        skipped_records=skipped_records,
        duplicate_records=duplicate_records,
        unique_tags=len(snapshot.records),
        encoding=encoding,
    )


def _validate_csv_quoting(
    text: str,
    *,
    cancel_event: threading.Event | None,
) -> None:
    """Reject quotes that do not follow the standard CSV field grammar."""

    index = 0
    at_field_start = True
    in_quotes = False
    after_closing_quote = False
    while index < len(text):
        if index % _READ_CHUNK_BYTES == 0:
            _raise_if_cancelled(cancel_event)
        character = text[index]

        if in_quotes:
            if character == '"':
                if index + 1 < len(text) and text[index + 1] == '"':
                    index += 2
                    continue
                in_quotes = False
                after_closing_quote = True
            index += 1
            continue

        if after_closing_quote:
            if character == ",":
                at_field_start = True
                after_closing_quote = False
            elif character in {"\r", "\n"}:
                at_field_start = True
                after_closing_quote = False
            else:
                raise TagCsvImportError(
                    "csv_error",
                    "标签 CSV 包含无效的引号格式。",
                )
            index += 1
            continue

        if character == '"':
            if not at_field_start:
                raise TagCsvImportError(
                    "csv_error",
                    "标签 CSV 包含无效的引号格式。",
                )
            in_quotes = True
            at_field_start = False
        elif character == ",":
            at_field_start = True
        elif character in {"\r", "\n"}:
            at_field_start = True
        else:
            at_field_start = False
        index += 1

    if in_quotes:
        raise TagCsvImportError(
            "csv_error",
            "标签 CSV 包含未闭合的引号字段。",
        )


def _parse_record(
    row: list[str],
    positions: Mapping[str, int],
) -> tuple[str, tuple[str, ...], int | None, int | None] | None:
    if any(position >= len(row) for position in positions.values()):
        return None
    raw_name = row[positions["name"]].strip()
    if not raw_name.isascii():
        return None
    try:
        name = normalize_tag_name(raw_name)
    except ValueError:
        return None

    chinese_names = tuple(
        dict.fromkeys(
            part.strip()
            for part in row[positions["cn_name"]].split(",")
            if part.strip()
        )
    )
    count_text = row[positions["post_count"]].strip()
    if count_text:
        if _NON_NEGATIVE_DECIMAL.fullmatch(count_text) is None:
            return None
        try:
            post_count = int(count_text)
        except ValueError:
            return None
    else:
        post_count = None

    category_text = row[positions["category"]].strip()
    category = int(category_text) if category_text in {"0", "1", "3", "4", "5"} else None
    return name, chinese_names, post_count, category


def _suggestion_sort_key(
    suggestion: TagSuggestion,
    fragment: str,
    chinese: bool,
) -> tuple[int, bool, int, str]:
    if chinese:
        exact = fragment in suggestion.chinese_names
    else:
        exact = suggestion.name == fragment
    return (
        0 if exact else 1,
        suggestion.post_count is None,
        -(suggestion.post_count or 0),
        suggestion.name,
    )


def _validate_field_lengths(row: list[str], maximum: int) -> None:
    if any(len(field) > maximum for field in row):
        raise TagCsvImportError(
            "field_too_large",
            "标签 CSV 包含超过 64 KiB 限制的字段。",
        )


def _validate_limits(
    maximum_bytes: int,
    maximum_records: int,
    maximum_field_characters: int,
) -> None:
    for value, name in (
        (maximum_bytes, "maximum_bytes"),
        (maximum_records, "maximum_records"),
        (maximum_field_characters, "maximum_field_characters"),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise TagCsvImportCancelled("Tag CSV import was cancelled.")
