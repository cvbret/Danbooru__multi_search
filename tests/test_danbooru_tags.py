from __future__ import annotations

import json
import os
from urllib.parse import parse_qs, urlsplit

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QNetworkReply

from danbooru_multi_search.danbooru import (
    MAX_TAG_RESPONSE_BYTES,
    QtTagsRequestAttempt,
    TagErrorKind,
    TagParseError,
    build_tags_request,
    parse_tags_response,
)


APPLICATION = QGuiApplication.instance() or QGuiApplication([])


def test_tag_request_uses_fixed_path_six_parameters_and_url_encoding():
    request = build_tags_request(
        "https://example.invalid/api",
        "honkai:_star%",
    )
    parsed = urlsplit(request.url().toString())

    assert parsed.path == "/api/tags.json"
    assert parse_qs(parsed.query) == {
        "search[name_matches]": ["honkai:_star%*"],
        "search[hide_empty]": ["true"],
        "search[is_deprecated]": ["false"],
        "search[order]": ["count"],
        "page": ["1"],
        "limit": ["20"],
    }
    assert bytes(request.rawHeader("Accept")) == b"application/json"


@pytest.mark.parametrize(
    "fragment",
    ["", "猫", "cat dog", "cat*", "-cat", "OR"],
)
def test_tag_request_rejects_invalid_remote_fragments(fragment):
    with pytest.raises(ValueError):
        build_tags_request("https://example.invalid", fragment)


def test_parse_tag_response_preserves_categories_missing_and_zero_counts():
    payload = json.dumps(
        [
            {"name": "cat", "post_count": 0, "category": 0, "extra": True},
            {"name": "artist-name_(artist)", "post_count": None, "category": 1},
            {"name": "honkai:_star_rail", "category": 3},
            {"name": "meta_tag", "post_count": 7, "category": 99},
        ]
    ).encode("utf-8")

    suggestions = parse_tags_response(payload)

    assert [item.name for item in suggestions] == [
        "cat",
        "artist-name_(artist)",
        "honkai:_star_rail",
        "meta_tag",
    ]
    assert suggestions[0].post_count == 0
    assert suggestions[0].post_count_text == "0"
    assert suggestions[1].post_count is None
    assert suggestions[1].post_count_text == "暂无数据"
    assert suggestions[0].category_text == "普通标签"
    assert suggestions[1].category_text == "画师"
    assert suggestions[2].category_text == "作品系列"
    assert suggestions[3].category_text == "未知分类"


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"[null]",
        b"[{}]",
        b'[{"name":"cat","post_count":-1}]',
        b'[{"name":"cat","post_count":true}]',
        b'[{"name":"cat","category":false}]',
        b'[{"name":"cat","category":"0"}]',
        json.dumps([{"name": "猫"}]).encode("utf-8"),
        b'[{"name":"rating:g"}]',
    ],
)
def test_parse_tag_response_rejects_invalid_roots_and_fields(payload):
    with pytest.raises(TagParseError):
        parse_tags_response(payload)


def test_parse_tag_response_converts_json_depth_and_large_integer_errors():
    deep = b"[" * 2_000 + b"0" + b"]" * 2_000
    large_integer = b'[{"name":"cat","post_count":' + b"9" * 5_000 + b"}]"

    with pytest.raises(TagParseError, match="not valid JSON"):
        parse_tags_response(deep)
    with pytest.raises(TagParseError, match="not valid JSON"):
        parse_tags_response(large_integer)


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_parse_tag_response_rejects_nonstandard_constants_in_extra_fields(constant):
    payload = b'[{"name":"cat","ignored":' + constant + b"}]"

    with pytest.raises(TagParseError, match="not valid JSON"):
        parse_tags_response(payload)


class FakeReply(QObject):
    metaDataChanged = Signal()
    readyRead = Signal()
    finished = Signal()

    def __init__(self, payload: bytes, declared_length: int) -> None:
        super().__init__()
        self.payload = payload
        self.declared_length = declared_length
        self.abort_count = 0

    def rawHeader(self, name: str) -> bytes:
        if name == "Content-Length":
            return str(self.declared_length).encode("ascii")
        return b""

    def readAll(self) -> bytes:
        payload = self.payload
        self.payload = b""
        return payload

    def abort(self) -> None:
        self.abort_count += 1

    def deleteLater(self) -> None:
        return


class FakeManager:
    def __init__(self, reply: FakeReply) -> None:
        self.reply = reply

    def get(self, request):
        return self.reply


def test_tag_attempt_rejects_declared_and_actual_oversized_responses():
    request = build_tags_request("https://example.invalid", "cat")
    declared_reply = FakeReply(b"[]", MAX_TAG_RESPONSE_BYTES + 1)
    declared_results = []
    QtTagsRequestAttempt(
        FakeManager(declared_reply),
        request,
        declared_results.append,
        APPLICATION,
    )

    assert declared_results[0].error_code == TagErrorKind.RESPONSE_TOO_LARGE
    assert declared_reply.abort_count == 1

    actual_reply = FakeReply(b"x" * (MAX_TAG_RESPONSE_BYTES + 1), 0)
    actual_results = []
    attempt = QtTagsRequestAttempt(
        FakeManager(actual_reply),
        request,
        actual_results.append,
        APPLICATION,
    )
    actual_reply.readyRead.emit()

    assert attempt is not None
    assert actual_results[0].error_code == TagErrorKind.RESPONSE_TOO_LARGE
    assert actual_reply.abort_count == 1
