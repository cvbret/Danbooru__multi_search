from __future__ import annotations

import codecs
import threading
from pathlib import Path

import pytest

from danbooru_multi_search.danbooru import (
    TagCsvImportCancelled,
    TagCsvImportError,
    load_tag_csv,
)


HEADER = "name,cn_name,post_count,category,wiki\n"
ROWS = (
    'blue_sky,"蓝天,青空",12,0,fictional\n'
    "sample_artist,示例画师,0,1,fictional\n"
    "missing_count,无数量,,5,fictional\n"
)


@pytest.mark.parametrize(
    ("encoding", "prefix", "expected_encoding"),
    [
        ("utf-8", b"", "utf-8"),
        ("utf-8", codecs.BOM_UTF8, "utf-8-sig"),
        ("gb18030", b"", "gb18030"),
    ],
)
def test_csv_import_supports_required_encodings(
    tmp_path,
    encoding,
    prefix,
    expected_encoding,
):
    path = tmp_path / f"fictional-{encoding}.csv"
    path.write_bytes(prefix + (HEADER + ROWS).encode(encoding))

    result = load_tag_csv(path)

    assert result.encoding == expected_encoding
    assert result.total_records == 3
    assert result.valid_records == 3
    assert result.skipped_records == 0
    assert result.unique_tags == 3
    assert result.snapshot.records_by_name["blue_sky"].chinese_names == (
        "蓝天",
        "青空",
    )
    assert result.snapshot.records_by_name["sample_artist"].post_count == 0
    assert result.snapshot.records_by_name["missing_count"].post_count is None


def test_csv_header_is_case_insensitive_and_extra_columns_are_ignored(tmp_path):
    path = tmp_path / "headers.csv"
    path.write_text(
        " NAME , CN_NAME , POST_COUNT , CATEGORY ,unused\n"
        "cat,猫,5,4,value\n",
        encoding="utf-8",
    )

    result = load_tag_csv(path)

    assert result.unique_tags == 1
    assert result.snapshot.records[0].name == "cat"
    assert result.snapshot.records[0].category_text == "角色"


@pytest.mark.parametrize(
    "invalid_row",
    [
        'cat,"未闭合,1,0,x\n',
        'cat,裸"引号,1,0,x\n',
        'cat,"已闭合"额外内容,1,0,x\n',
    ],
)
def test_csv_import_rejects_invalid_quote_grammar(tmp_path, invalid_row):
    path = tmp_path / "invalid-quotes.csv"
    path.write_text(HEADER + invalid_row, encoding="utf-8")

    with pytest.raises(TagCsvImportError) as error:
        load_tag_csv(path)

    assert error.value.code == "csv_error"


def test_csv_import_accepts_escaped_quotes_and_newlines_inside_quoted_fields(
    tmp_path,
):
    path = tmp_path / "quoted-fields.csv"
    path.write_text(
        HEADER + 'quoted_tag,"名称""甲,\n名称乙",1,0,x\n',
        encoding="utf-8",
    )

    result = load_tag_csv(path)

    assert result.total_records == 1
    assert result.snapshot.records[0].chinese_names == ("名称\"甲", "名称乙")


@pytest.mark.parametrize(
    "header",
    [
        "name,cn_name,post_count\n",
        "name,name,cn_name,post_count,category\n",
    ],
)
def test_csv_import_rejects_missing_or_duplicate_required_headers(tmp_path, header):
    path = tmp_path / "bad-header.csv"
    path.write_text(header + "cat,猫,1,0\n", encoding="utf-8")

    with pytest.raises(TagCsvImportError) as error:
        load_tag_csv(path)

    assert error.value.code == "invalid_header"


def test_csv_skips_invalid_rows_and_merges_duplicate_tags(tmp_path):
    path = tmp_path / "merge.csv"
    path.write_text(
        HEADER
        + "hero,英雄,2,4,x\n"
        + "HERO,勇者,9,4,x\n"
        + "hero,主角,4,3,x\n"
        + "bad count,坏标签,3,0,x\n"
        + "negative,负数,-1,0,x\n"
        + "unknown,未知,,88,x\n",
        encoding="utf-8",
    )

    result = load_tag_csv(path)
    hero = result.snapshot.records_by_name["hero"]

    assert result.total_records == 6
    assert result.valid_records == 4
    assert result.skipped_records == 2
    assert result.duplicate_records == 2
    assert result.unique_tags == 2
    assert hero.chinese_names == ("英雄", "勇者", "主角")
    assert hero.post_count == 9
    assert hero.category is None
    assert result.snapshot.records_by_name["unknown"].category_text == "未知分类"


def test_excessively_long_decimal_count_is_skipped_as_an_invalid_row(tmp_path):
    path = tmp_path / "long-count.csv"
    path.write_text(
        HEADER
        + f"invalid_count,,{'9' * 5000},0,x\n"
        + "valid_tag,,1,0,x\n",
        encoding="utf-8",
    )

    result = load_tag_csv(path)

    assert result.total_records == 2
    assert result.valid_records == 1
    assert result.skipped_records == 1
    assert result.snapshot.records[0].name == "valid_tag"


def test_non_ascii_names_are_rejected_before_case_conversion(tmp_path):
    path = tmp_path / "unicode-names.csv"
    path.write_text(
        HEADER
        + "Kat,,1,0,x\n"
        + "ſomething,,1,0,x\n"
        + "Straße,,1,0,x\n"
        + "ASCII_TAG,,1,0,x\n",
        encoding="utf-8",
    )

    result = load_tag_csv(path)

    assert result.total_records == 4
    assert result.valid_records == 1
    assert result.skipped_records == 3
    assert result.snapshot.records[0].name == "ascii_tag"


def test_local_indexes_sort_exact_prefix_and_chinese_matches_deterministically(
    tmp_path,
):
    path = tmp_path / "search.csv"
    path.write_text(
        HEADER
        + "cat,猫,0,0,x\n"
        + "cat_ears,猫耳,100,0,x\n"
        + "cat_tail,猫尾,50,0,x\n"
        + "cat_unknown,猫科,,0,x\n",
        encoding="utf-8",
    )
    snapshot = load_tag_csv(path).snapshot

    assert [item.name for item in snapshot.search_english("cat")] == [
        "cat",
        "cat_ears",
        "cat_tail",
        "cat_unknown",
    ]
    assert [item.name for item in snapshot.search_chinese("猫")] == [
        "cat",
        "cat_ears",
        "cat_tail",
        "cat_unknown",
    ]
    assert snapshot.search_chinese("猫耳")[0].chinese_name == "猫耳"


def test_csv_limits_fail_the_whole_import(tmp_path):
    path = tmp_path / "limits.csv"
    path.write_text(HEADER + "cat,猫,1,0,x\ndog,狗,2,0,x\n", encoding="utf-8")

    with pytest.raises(TagCsvImportError) as size_error:
        load_tag_csv(path, maximum_bytes=8)
    assert size_error.value.code == "file_too_large"

    with pytest.raises(TagCsvImportError) as record_error:
        load_tag_csv(path, maximum_records=1)
    assert record_error.value.code == "too_many_records"

    with pytest.raises(TagCsvImportError) as field_error:
        load_tag_csv(path, maximum_field_characters=2)
    assert field_error.value.code == "field_too_large"


def test_csv_import_rejects_no_valid_records_and_honors_cancellation(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text(HEADER + "bad tag,中文,-1,0,x\n", encoding="utf-8")

    with pytest.raises(TagCsvImportError) as empty_error:
        load_tag_csv(path)
    assert empty_error.value.code == "no_valid_records"

    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(TagCsvImportCancelled):
        load_tag_csv(path, cancel_event=cancelled)


def test_csv_import_handles_paths_with_chinese_spaces_and_percent(tmp_path):
    path = Path(tmp_path) / "标签 库 100%.csv"
    path.write_text(HEADER + "love_live!,虚构系列,4,3,x\n", encoding="utf-8")

    result = load_tag_csv(path)

    assert result.snapshot.records[0].name == "love_live!"
