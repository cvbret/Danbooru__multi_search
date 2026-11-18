from dataclasses import FrozenInstanceError

import pytest
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest

from danbooru_multi_search.danbooru.http_diagnostics import read_reply_diagnostics


class MetadataOnlyReply:
    def __init__(
        self, status=200, error=QNetworkReply.NetworkError.NoError,
        text="raw network details", retry_after=b" 2 ",
    ):
        self.status = status
        self.network_error = error
        self.text = text
        self.retry_after = retry_after
        self.calls = []

    def attribute(self, attribute):
        assert attribute == QNetworkRequest.Attribute.HttpStatusCodeAttribute
        self.calls.append("attribute")
        return self.status

    def error(self):
        self.calls.append("error")
        return self.network_error

    def errorString(self):
        self.calls.append("errorString")
        return self.text

    def rawHeader(self, name):
        assert name == "Retry-After"
        self.calls.append("rawHeader")
        return self.retry_after

    def __getattr__(self, name):
        raise AssertionError(f"diagnostics must not use reply.{name}")


def test_only_four_metadata_reads_preserve_facts_and_return_a_frozen_value():
    reply = MetadataOnlyReply(status="200")
    result = read_reply_diagnostics(reply)
    assert reply.calls == ["attribute", "error", "errorString", "rawHeader"]
    assert result.http_status == 200
    assert result.network_error is reply.network_error
    assert result.network_error_text == "raw network details"
    assert result.retry_after == "2"
    with pytest.raises(FrozenInstanceError):
        result.http_status = 404


@pytest.mark.parametrize("status,error,expected", [
    (429, QNetworkReply.NetworkError.TimeoutError, "http"),
    (300, QNetworkReply.NetworkError.NoError, "http"),
    (299, QNetworkReply.NetworkError.NoError, None),
    (199, QNetworkReply.NetworkError.NoError, "http"),
    (200, QNetworkReply.NetworkError.TimeoutError, "timeout"),
    (None, QNetworkReply.NetworkError.TimeoutError, "timeout"),
    (200, QNetworkReply.NetworkError.ConnectionRefusedError, "network"),
    (None, QNetworkReply.NetworkError.NoError, None),
])
def test_failure_precedence_and_missing_status_are_preserved(status, error, expected):
    result = read_reply_diagnostics(MetadataOnlyReply(status=status, error=error))
    assert result.failure_kind == expected
    assert result.http_status == status


@pytest.mark.parametrize("raw,expected", [
    (b" \t ", None), (b" 120 ", "120"), (b" \xff ", "\ufffd"),
])
def test_retry_after_keeps_ascii_replacement_and_stripping(raw, expected):
    assert read_reply_diagnostics(MetadataOnlyReply(retry_after=raw)).retry_after == expected


@pytest.mark.parametrize("status,error_type", [("invalid", ValueError), (object(), TypeError)])
def test_non_none_status_conversion_errors_are_not_suppressed(status, error_type):
    reply = MetadataOnlyReply(status=status)
    with pytest.raises(error_type):
        read_reply_diagnostics(reply)
    assert reply.calls == ["attribute"]
