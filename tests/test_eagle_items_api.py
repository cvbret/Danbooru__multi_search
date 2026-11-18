"""Independent Item API wire checks against a controlled local Eagle server."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from danbooru_multi_search.integrations.eagle import EagleWebApiBridge
from tests.test_eagle_client import (
    ServerResponse,
    app_info,
    eagle_server,
    jsend,
    library_info,
)


PAGE_URL = "https://danbooru.donmai.us/posts/42"


def item_data(**changes):
    return {
        "id": "ITEM42",
        "name": "用户原名称",
        "url": PAGE_URL,
        "tags": ["user_tag", "画师"],
        "folders": ["FOLDER42"],
        "annotation": "用户注释\n保留空白  ",
        "isDeleted": False,
        "ext": "png",
        "size": 42,
        **changes,
    }


def item_page(items, *, total=None, offset=0, limit=100):
    return {
        "data": items,
        "total": len(items) if total is None else total,
        "offset": offset,
        "limit": limit,
    }


@pytest.mark.parametrize(
    "filter_name, query_name, value",
    [
        ("item_identifier", "id", "ITEM42"),
        ("url", "url", PAGE_URL),
        ("annotation", "annotation", "中文说明 + SHA256:abc"),
    ],
)
def test_get_item_page_uses_one_filter_and_real_nested_envelope_without_fields(
    eagle_server, filter_name, query_name, value,
):
    server = eagle_server(ServerResponse(jsend(item_page([item_data()], total=8, offset=7))))
    result = EagleWebApiBridge(server.base_url).get_items(**{filter_name: value}, offset=7)

    assert result.succeeded and result.value is not None
    page = result.value
    assert (page.total, page.offset, page.limit) == (8, 7, 100)
    (item,) = page.items
    assert (item.identifier, item.name, item.url) == ("ITEM42", "用户原名称", PAGE_URL)
    assert item.tags == ("user_tag", "画师") and item.folders == ("FOLDER42",)
    assert item.annotation == "用户注释\n保留空白  " and item.is_deleted is False
    (request,) = server.requests
    parsed = urlsplit(request.path)
    assert request.method == "GET" and parsed.path == "/api/v2/item/get"
    assert parse_qs(parsed.query) == {query_name: [value], "offset": ["7"], "limit": ["100"]}
    assert request.body == b""


def test_missing_item_is_an_empty_success_page(eagle_server):
    server = eagle_server(ServerResponse(jsend(item_page([]))))
    result = EagleWebApiBridge(server.base_url).get_items(item_identifier="MISSING")
    assert result.succeeded and result.value is not None
    assert result.value.items == () and result.value.total == 0
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    "data",
    [
        [item_data()],
        item_page([item_data()], total=2, offset=1),
        item_page([item_data(), item_data()]),
        item_page([item_data(id="OTHER")]),
    ],
    ids=["missing-page-envelope", "wrong-offset", "duplicate-id", "wrong-requested-id"],
)
def test_inconsistent_item_success_cannot_become_a_usable_match(eagle_server, data):
    server = eagle_server(ServerResponse(jsend(data)))
    result = EagleWebApiBridge(server.base_url).get_items(item_identifier="ITEM42")
    assert not result.succeeded and result.value is None
    assert result.code == "eagle_invalid_items"
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    "response, code",
    [
        (ServerResponse(b"server failure", status=500), "eagle_http_error"),
        (ServerResponse(b'{"status":"error","message":"read failed"}'), "eagle_api_error"),
        (ServerResponse(b"invalid json"), "eagle_invalid_json"),
    ],
    ids=["http-error", "jsend-error", "invalid-json"],
)
def test_item_read_failure_is_not_an_empty_match_or_an_automatic_write(eagle_server, response, code):
    server = eagle_server(response)
    result = EagleWebApiBridge(server.base_url).get_items(url=PAGE_URL)
    assert not result.succeeded and result.value is None and result.code == code
    assert len(server.requests) == 1 and server.requests[0].method == "GET"


def test_metadata_update_sends_only_identity_tags_and_annotation(eagle_server):
    tags = ("user_tag", "画师", "danbooru")
    annotation = "用户注释\n保留空白  \n\n新增说明"
    server = eagle_server(ServerResponse(jsend(item_data(tags=list(tags), annotation=annotation))))

    result = EagleWebApiBridge(server.base_url).update_item_metadata(
        "ITEM42", tags=tags, annotation=annotation,
    )

    assert result.succeeded and result.value is not None
    assert result.value.tags == tags and result.value.annotation == annotation
    assert result.value.name == "用户原名称" and result.value.folders == ("FOLDER42",)
    (request,) = server.requests
    assert request.method == "POST" and request.path == "/api/v2/item/update"
    assert json.loads(request.body) == {"id": "ITEM42", "tags": list(tags), "annotation": annotation}
    assert request.headers["content-type"] == "application/json"


def test_metadata_update_rejects_a_different_returned_item_without_resending(eagle_server):
    server = eagle_server(ServerResponse(jsend(item_data(id="OTHER"))))
    result = EagleWebApiBridge(server.base_url).update_item_metadata(
        "ITEM42", tags=("tag",), annotation="说明",
    )
    assert not result.succeeded and result.value is None and result.code == "eagle_invalid_items"
    assert len(server.requests) == 1


@pytest.mark.parametrize("folder", [None, "FOLDER42"], ids=["root", "folder"])
def test_checked_add_sends_one_local_file_and_requires_returned_identifier(eagle_server, tmp_path, folder):
    source = tmp_path / "作品 原文件.png"
    original = b"checked original bytes"
    source.write_bytes(original)
    metadata = {"name": "42_artist", "website": PAGE_URL, "tags": ["danbooru"], "annotation": "说明"}
    before = json.loads(json.dumps(metadata))
    server = eagle_server(ServerResponse(jsend({"id": "ADDED42"})))

    result = EagleWebApiBridge(server.base_url).add_item_from_file(
        source, folder_identifier=folder, metadata=metadata,
    )

    assert result.succeeded and result.value is not None and result.value.item_identifier == "ADDED42"
    (request,) = server.requests
    assert request.method == "POST" and request.path == "/api/v2/item/add"
    expected = {"path": str(source.resolve()), **metadata}
    if folder is not None:
        expected["folders"] = [folder]
    assert json.loads(request.body) == expected
    assert source.read_bytes() == original and metadata == before


@pytest.mark.parametrize("data", [{}, {"id": "   "}], ids=["missing-id", "blank-id"])
def test_add_success_without_identifier_is_not_confirmed_or_repeated(eagle_server, tmp_path, data):
    source = tmp_path / "original.png"
    source.write_bytes(b"original")
    server = eagle_server(ServerResponse(jsend(data)))
    result = EagleWebApiBridge(server.base_url).add_item_from_file(source)
    assert not result.succeeded and result.value is None and result.code == "eagle_missing_item_identifier"
    assert len(server.requests) == 1 and source.read_bytes() == b"original"


def test_legacy_import_keeps_connection_library_and_folder_checks_before_add(eagle_server, tmp_path):
    source = tmp_path / "original.png"
    source.write_bytes(b"original")
    folders = {"data": [{"id": "FOLDER42", "name": "目标", "children": []}],
               "total": 1, "offset": 0, "limit": 1000}
    server = eagle_server(
        ServerResponse(jsend(app_info())),
        ServerResponse(jsend(library_info())),
        ServerResponse(jsend(folders)),
        ServerResponse(jsend({"id": "LEGACY42"})),
    )
    result = EagleWebApiBridge(server.base_url).import_file(source, folder_identifier="FOLDER42")
    assert result.succeeded and result.value.item_identifier == "LEGACY42"
    assert [(request.method, request.path) for request in server.requests] == [
        ("GET", "/api/v2/app/info"),
        ("GET", "/api/v2/library/info"),
        ("GET", "/api/v2/folder/get?offset=0&limit=1000"),
        ("POST", "/api/v2/item/add"),
    ]
    assert json.loads(server.requests[-1].body) == {"path": str(source.resolve()), "folders": ["FOLDER42"]}
