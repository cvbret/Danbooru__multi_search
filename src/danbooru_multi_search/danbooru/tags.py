"""Danbooru tag models, strict parsing, and cancellable API requests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Callable

from PySide6.QtCore import QObject, QUrl, QUrlQuery, Slot
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from danbooru_multi_search.danbooru.client import (
    DEFAULT_TIMEOUT_MS,
    USER_AGENT,
    RequestErrorKind,
)
from danbooru_multi_search.danbooru.tag_syntax import (
    QUERY_OPERATOR_NAMES,
    normalize_tag_name,
)
from danbooru_multi_search.tasks import TaskAttemptResult


DEFAULT_TAG_SUGGESTION_LIMIT = 20
MAX_TAG_RESPONSE_BYTES = 1024 * 1024

_CATEGORY_LABELS = {
    0: "普通标签",
    1: "画师",
    3: "作品系列",
    4: "角色",
    5: "元数据",
}


class TagErrorKind:
    INVALID_REQUEST = RequestErrorKind.INVALID_REQUEST
    HTTP = RequestErrorKind.HTTP
    TIMEOUT = RequestErrorKind.TIMEOUT
    NETWORK = RequestErrorKind.NETWORK
    INVALID_RESPONSE = RequestErrorKind.INVALID_RESPONSE
    RESPONSE_TOO_LARGE = "response_too_large"


class TagParseError(ValueError):
    """Raised when a tag response cannot form strict suggestion models."""


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


def normalize_tag_fragment(value: str) -> str:
    """Normalize an incomplete ASCII fragment without damaging punctuation."""

    if not isinstance(value, str):
        raise ValueError("Tag fragment must be text.")
    stripped = value.strip()
    if not stripped:
        raise ValueError("Tag fragment cannot be empty.")
    if not stripped.isascii():
        raise ValueError("Remote tag fragments must contain only ASCII characters.")
    if any(character.isspace() for character in stripped):
        raise ValueError("Tag fragment cannot contain whitespace.")
    if any(ord(character) < 33 or ord(character) > 126 for character in stripped):
        raise ValueError("Tag fragment contains unsupported control characters.")
    if stripped.startswith(("-", "~")) or "*" in stripped:
        raise ValueError("Tag fragment contains unsupported search syntax.")
    if stripped.casefold() in QUERY_OPERATOR_NAMES:
        raise ValueError("Tag fragment contains an unsupported query operator.")
    return stripped.casefold()


def build_tags_request(
    base_url: str,
    fragment: str,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> QNetworkRequest:
    """Build the fixed M1-002 ``tags.json`` prefix query."""

    if timeout_ms <= 0 or isinstance(timeout_ms, bool):
        raise ValueError("timeout_ms must be greater than zero")
    normalized_fragment = normalize_tag_fragment(fragment)

    endpoint = QUrl(f"{base_url.rstrip('/')}/tags.json")
    if (
        not endpoint.isValid()
        or endpoint.scheme() not in {"http", "https"}
        or not endpoint.host()
    ):
        raise ValueError("Danbooru base URL must be a valid HTTP or HTTPS URL.")

    query = QUrlQuery()
    query.addQueryItem("search[name_matches]", f"{normalized_fragment}*")
    query.addQueryItem("search[hide_empty]", "true")
    query.addQueryItem("search[is_deprecated]", "false")
    query.addQueryItem("search[order]", "count")
    query.addQueryItem("page", "1")
    query.addQueryItem("limit", str(DEFAULT_TAG_SUGGESTION_LIMIT))
    endpoint.setQuery(query)

    request = QNetworkRequest(endpoint)
    request.setRawHeader(b"User-Agent", USER_AGENT.encode("ascii"))
    request.setRawHeader(b"Accept", b"application/json")
    request.setAttribute(QNetworkRequest.Attribute.Http2AllowedAttribute, False)
    request.setTransferTimeout(timeout_ms)
    return request


def parse_tags_response(payload: bytes) -> tuple[TagSuggestion, ...]:
    """Strictly parse the fields used from a Danbooru tag response."""

    try:
        raw = json.loads(payload, parse_constant=_reject_json_constant)
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise TagParseError("Danbooru tag response is not valid JSON.") from exc

    if not isinstance(raw, list):
        raise TagParseError("Danbooru tag response root must be a JSON array.")

    suggestions: list[TagSuggestion] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise TagParseError(f"Tag at index {index} must be a JSON object.")
        suggestions.append(_parse_tag(item, index))
    return tuple(suggestions)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant is not allowed: {value}")


def _parse_tag(raw: Mapping[str, Any], index: int) -> TagSuggestion:
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise TagParseError(
            f"Tag at index {index} field 'name' must be a non-empty string."
        )
    try:
        normalized_name = normalize_tag_name(name)
    except ValueError as exc:
        raise TagParseError(
            f"Tag at index {index} field 'name' is invalid: {exc}"
        ) from exc

    post_count = raw.get("post_count")
    if post_count is not None and (
        not isinstance(post_count, int)
        or isinstance(post_count, bool)
        or post_count < 0
    ):
        raise TagParseError(
            f"Tag at index {index} field 'post_count' must be a non-negative "
            "integer or null."
        )

    category = raw.get("category")
    if category is not None and (
        not isinstance(category, int) or isinstance(category, bool)
    ):
        raise TagParseError(
            f"Tag at index {index} field 'category' must be an integer or null."
        )

    return TagSuggestion(
        name=normalized_name,
        post_count=post_count,
        category=category,
    )


class QtTagsRequestAttempt(QObject):
    """Read one bounded tag response and convert it to a queue result."""

    def __init__(
        self,
        manager: QNetworkAccessManager,
        request: QNetworkRequest,
        completion: Callable[[TaskAttemptResult], None],
        parent: QObject,
        *,
        maximum_bytes: int = MAX_TAG_RESPONSE_BYTES,
    ) -> None:
        super().__init__(parent)
        if maximum_bytes <= 0 or isinstance(maximum_bytes, bool):
            raise ValueError("maximum_bytes must be greater than zero")
        self._completion: Callable[[TaskAttemptResult], None] | None = completion
        self._maximum_bytes = maximum_bytes
        self._payload = bytearray()
        self._reply: QNetworkReply | None = manager.get(request)
        self._finishing = False
        self._reply.metaDataChanged.connect(self._metadata_changed)
        self._reply.readyRead.connect(self._ready_read)
        self._reply.finished.connect(self._finished)
        self._metadata_changed()

    def abort(self) -> None:
        self._completion = None
        reply = self._reply
        self._reply = None
        self._payload.clear()
        if reply is not None:
            reply.abort()
            reply.deleteLater()
        self.deleteLater()

    @Slot()
    def _metadata_changed(self) -> None:
        reply = self._reply
        if reply is None or self._completion is None:
            return
        raw_length = bytes(reply.rawHeader("Content-Length")).strip()
        if not raw_length:
            return
        try:
            content_length = int(raw_length)
        except ValueError:
            return
        if content_length > self._maximum_bytes:
            self._force_failure(
                TaskAttemptResult.failure(
                    "Danbooru tag response exceeds the configured size limit.",
                    error_code=TagErrorKind.RESPONSE_TOO_LARGE,
                )
            )

    @Slot()
    def _ready_read(self) -> None:
        reply = self._reply
        if reply is None or self._completion is None:
            return
        chunk = bytes(reply.readAll())
        if not chunk:
            return
        if len(self._payload) + len(chunk) > self._maximum_bytes:
            self._force_failure(
                TaskAttemptResult.failure(
                    "Danbooru tag response exceeds the configured size limit.",
                    error_code=TagErrorKind.RESPONSE_TOO_LARGE,
                )
            )
            return
        self._payload.extend(chunk)

    def _force_failure(self, result: TaskAttemptResult) -> None:
        if self._completion is None:
            return
        reply = self._reply
        self._reply = None
        self._finishing = True
        self._complete(result)
        if reply is not None:
            reply.abort()
            reply.deleteLater()
        self.deleteLater()

    @Slot()
    def _finished(self) -> None:
        if self._finishing:
            return
        reply = self._reply
        if reply is None:
            return
        self._ready_read()
        if self._completion is None or self._reply is None:
            return

        self._finishing = True
        self._reply = None
        status_value = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute
        )
        http_status = int(status_value) if status_value is not None else None
        network_error = reply.error()
        network_error_text = reply.errorString()
        retry_after_bytes = bytes(reply.rawHeader("Retry-After"))
        retry_after = (
            retry_after_bytes.decode("ascii", errors="replace").strip() or None
        )
        reply.deleteLater()

        if http_status is not None and not 200 <= http_status < 300:
            result = TaskAttemptResult.failure(
                f"Danbooru 标签请求返回 HTTP {http_status}。",
                error_code=TagErrorKind.HTTP,
                http_status=http_status,
                retry_after=retry_after,
            )
        elif network_error != QNetworkReply.NetworkError.NoError:
            if network_error == QNetworkReply.NetworkError.TimeoutError:
                error_code = TagErrorKind.TIMEOUT
                message = "Danbooru 标签请求超时。"
            else:
                error_code = TagErrorKind.NETWORK
                message = f"Danbooru 标签网络错误：{network_error_text}"
            result = TaskAttemptResult.failure(
                message,
                error_code=error_code,
                http_status=http_status,
            )
        else:
            try:
                suggestions = parse_tags_response(bytes(self._payload))
            except TagParseError as exc:
                result = TaskAttemptResult.failure(
                    str(exc),
                    error_code=TagErrorKind.INVALID_RESPONSE,
                    http_status=http_status,
                )
            else:
                result = TaskAttemptResult.success(
                    suggestions,
                    http_status=http_status,
                )

        self._complete(result)
        self.deleteLater()

    def _complete(self, result: TaskAttemptResult) -> None:
        completion = self._completion
        self._completion = None
        self._payload.clear()
        if completion is not None:
            completion(result)
