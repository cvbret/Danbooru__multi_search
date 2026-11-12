from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from danbooru_multi_search.core.downloads import DownloadError, DownloadRequest, filename_key, sanitize_filename_component


@pytest.mark.parametrize("source,expected", [
    ("  中文 name...  ", "中文 name"), ("a<b>:c/d\\e|f?g*", "a_b__c_d_e_f_g_"),
    ("NUL", "_NUL"), ("con.txt", "_con.txt"), ("COM¹", "_COM¹"), ("lpt9", "_lpt9"),
    ("...", "download"), ("a\x00b", "a_b"),
])
def test_filename_rules_are_portable_and_keep_readable_labels(source, expected):
    assert sanitize_filename_component(source) == expected
    assert DownloadRequest(321, "https://example.invalid/a", "JPG", source).filename == expected + "_321.jpg"
    assert filename_key("Cafe\u0301.JPG") == filename_key("CAFÉ.jpg")


@pytest.mark.parametrize("identifier,url,extension", [
    (True, "https://example.invalid/a", "jpg"), (0, "https://example.invalid/a", "jpg"),
    (2**63, "https://example.invalid/a", "jpg"), (1, "file:///tmp/image", "jpg"),
    (1, "https://", "jpg"), (1, "https://example.invalid/a b", "jpg"),
    (1, "https://example.invalid/a", "../jpg"), (1, "https://example.invalid/a", ""),
])
def test_invalid_request_values_are_rejected_before_network_or_storage(identifier, url, extension):
    with pytest.raises(DownloadError):
        DownloadRequest(identifier, url, extension)


def test_original_extension_and_identifier_are_preserved_in_immutable_request():
    value = DownloadRequest(2**60 + 1, "https://example.invalid/original.webm", "WEBM")
    assert value.filename == f"{2**60 + 1}.webm"
    with pytest.raises(FrozenInstanceError):
        value.post_id = 2


@pytest.mark.parametrize("label", ["中文" * 200, "😀" * 200])
def test_long_unicode_labels_leave_portable_space_for_identifier_and_extension(label):
    request = DownloadRequest(2**63 - 1, "https://example.invalid/original.webm", "webm", label)
    assert len(request.filename.encode("utf-8")) < 200
    assert len(request.filename.encode("utf-16-le")) // 2 < 200
    assert request.filename.endswith(f"_{2**63 - 1}.webm")
