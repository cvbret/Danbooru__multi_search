"""Eagle Web API V2 bridge for the local Windows Eagle application."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from http.client import HTTPException
import json
import math
import ntpath
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from danbooru_multi_search import __version__
from danbooru_multi_search.integrations.eagle.contracts import (
    EagleConnectionInfo,
    EagleFolderInfo,
    EagleImportResult,
    EagleItemInfo,
    EagleItemPage,
    EagleLibraryInfo,
)
from danbooru_multi_search.integrations.eagle.transport import (
    EagleConnectionError,
    EagleHttpRequest,
    EagleHttpTransport,
    EagleResponseTooLargeError,
    EagleTimeoutError,
    EagleTransportError,
    UrllibEagleHttpTransport,
)
from danbooru_multi_search.core.results import ServiceAvailability, ValueResult


DEFAULT_EAGLE_BASE_URL = "http://localhost:41595/api/v2/"
DEFAULT_EAGLE_TIMEOUT_SECONDS = 3.0
DEFAULT_EAGLE_MAXIMUM_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_EAGLE_USER_AGENT = f"DanbooruMultiSearch/{__version__} EagleBridge"
FOLDER_PAGE_LIMIT = 1000

_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_VERSION_PATTERN = re.compile(
    r"^\s*(?P<major>\d+)(?:\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?)?\s*$"
)
_BUILD_PATTERN = re.compile(
    r"^\s*(?:build\s*)?(?P<number>[0-9]+)\s*$",
    re.IGNORECASE,
)
_STRING_METADATA_FIELDS = frozenset({"name", "website", "annotation"})
_ALLOWED_METADATA_FIELDS = _STRING_METADATA_FIELDS | {"tags"}

@dataclass(frozen=True, slots=True)
class EagleClientConfiguration:
    """Concrete HTTP configuration for application assembly, without resources."""
    base_url: str
    timeout_seconds: float
    maximum_response_bytes: int
    user_agent: str


class _EagleClientError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_windows_library_path(path: str) -> str:
    """Return a stable case-insensitive Windows path representation."""

    if not isinstance(path, str) or not path.strip():
        raise ValueError("The Eagle library path must be a non-empty string.")
    normalized = ntpath.normpath(unicodedata.normalize("NFC", path))
    if not ntpath.isabs(normalized):
        raise ValueError("The Eagle library path must be absolute.")
    return ntpath.normcase(normalized)


def library_identifier_from_windows_path(path: str) -> str:
    normalized = normalize_windows_library_path(path)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class EagleWebApiBridge:
    """Synchronous bridge intended for worker tasks and command-line checks."""

    availability = ServiceAvailability(available=True)
    is_supported = True
    unavailable_reason = None

    def __init__(
        self,
        base_url: str = DEFAULT_EAGLE_BASE_URL,
        *,
        transport: EagleHttpTransport | None = None,
        timeout_seconds: float = DEFAULT_EAGLE_TIMEOUT_SECONDS,
        maximum_response_bytes: int = DEFAULT_EAGLE_MAXIMUM_RESPONSE_BYTES,
        user_agent: str = DEFAULT_EAGLE_USER_AGENT,
    ) -> None:
        self._base_url = self._validate_base_url(base_url)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be greater than zero.")
        if (
            isinstance(maximum_response_bytes, bool)
            or not isinstance(maximum_response_bytes, int)
            or maximum_response_bytes <= 0
        ):
            raise ValueError("maximum_response_bytes must be a positive integer.")
        if not isinstance(user_agent, str) or not user_agent.strip():
            raise ValueError("user_agent must be a non-empty string.")
        if "\r" in user_agent or "\n" in user_agent:
            raise ValueError("user_agent must not contain line breaks.")

        self._transport = transport or UrllibEagleHttpTransport()
        self._timeout_seconds = float(timeout_seconds)
        self._maximum_response_bytes = maximum_response_bytes
        self._user_agent = user_agent

    def test_connection(self) -> ValueResult[EagleConnectionInfo]:
        try:
            data = self._request_data("app/info")
            connection = self._parse_connection_info(data)
        except _EagleClientError as exc:
            return self._failure(exc)
        return ValueResult(succeeded=True, value=connection)

    @property
    def configuration(self) -> EagleClientConfiguration:
        """Return validated immutable values; callers cannot mutate this bridge."""
        return EagleClientConfiguration(
            self._base_url,
            self._timeout_seconds,
            self._maximum_response_bytes,
            self._user_agent,
        )

    def get_current_library(self) -> ValueResult[EagleLibraryInfo]:
        try:
            data = self._request_data("library/info")
        except _EagleClientError as exc:
            if exc.code == "eagle_api_error" and self._looks_like_no_library(str(exc)):
                return ValueResult.unavailable("eagle_no_library", str(exc))
            return self._failure(exc)

        if data is None or data == {}:
            return ValueResult.unavailable(
                "eagle_no_library",
                "Eagle does not currently have an open library.",
            )
        if not isinstance(data, dict):
            return ValueResult.unavailable(
                "eagle_invalid_library",
                "Eagle returned an invalid library information object.",
            )

        name = data.get("name")
        raw_path = data.get("path")
        if not isinstance(name, str) or not name.strip():
            return ValueResult.unavailable(
                "eagle_invalid_library",
                "Eagle library name must be a non-empty string.",
            )
        if not isinstance(raw_path, str) or not raw_path.strip():
            return ValueResult.unavailable(
                "eagle_invalid_library",
                "Eagle library path must be a non-empty string.",
            )
        try:
            identifier = library_identifier_from_windows_path(raw_path)
        except ValueError as exc:
            return ValueResult.unavailable("eagle_invalid_library", str(exc))

        return ValueResult(
            succeeded=True,
            value=EagleLibraryInfo(
                identifier=identifier,
                name=name,
                path=Path(raw_path),
            ),
        )

    def list_folders(
        self, *, maximum_pages: int = 10001, maximum_folders: int | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ValueResult[tuple[EagleFolderInfo, ...]]:
        folders: list[EagleFolderInfo] = []
        seen_identifiers: set[str] = set()
        requested_offset = 0
        page_count = 0

        try:
            if (type(maximum_pages) is not int or maximum_pages < 1 or
                    maximum_folders is not None and (type(maximum_folders) is not int or maximum_folders < 1)):
                raise _EagleClientError("eagle_invalid_folders", "Invalid folder loading limits.")
            while True:
                if cancelled and cancelled():
                    raise _EagleClientError("eagle_cancelled", "Eagle folder loading was cancelled.")
                if page_count >= maximum_pages:
                    raise _EagleClientError("eagle_folder_limit", "Eagle folder page limit was reached.")
                query = urlencode(
                    {"offset": requested_offset, "limit": FOLDER_PAGE_LIMIT}
                )
                data = self._request_data(f"folder/get?{query}")
                entries, total, response_offset = self._parse_folder_page(data)
                if response_offset != requested_offset:
                    raise _EagleClientError(
                        "eagle_invalid_folders",
                        "Eagle returned an unexpected folder pagination offset.",
                    )
                for entry in entries:
                    self._append_folder_tree(
                        entry,
                        parent_identifier=None,
                        output=folders,
                        seen_identifiers=seen_identifiers,
                        maximum_folders=maximum_folders, cancelled=cancelled,
                    )

                next_offset = requested_offset + len(entries)
                if next_offset >= total:
                    break
                if next_offset <= requested_offset:
                    raise _EagleClientError(
                        "eagle_invalid_folders",
                        "Eagle folder pagination did not make progress.",
                    )
                requested_offset = next_offset
                page_count += 1
                if page_count > 10_000:
                    raise _EagleClientError(
                        "eagle_invalid_folders",
                        "Eagle returned too many folder pages.",
                    )
        except _EagleClientError as exc:
            return self._failure(exc)

        return ValueResult(succeeded=True, value=tuple(folders))

    def create_folder(self, name: str, *, parent_identifier: str | None = None) -> ValueResult[EagleFolderInfo]:
        """POST once; callers must not automatically repeat an uncertain creation."""
        try:
            if not isinstance(name, str) or not name.strip():
                raise _EagleClientError("eagle_invalid_folder_name", "Folder name must not be empty.")
            parent = self._validate_folder_identifier(parent_identifier)
            payload = {"name": name}
            if parent is not None:
                payload["parent"] = parent
            data = self._request_data("folder/create", method="POST", payload=payload)
            folders: list[EagleFolderInfo] = []
            self._append_folder_tree(data, parent_identifier=parent, output=folders, seen_identifiers=set())
            return ValueResult(succeeded=True, value=folders[0])
        except _EagleClientError as exc:
            return self._failure(exc)

    def import_file(
        self,
        file_path: Path,
        *,
        folder_identifier: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ValueResult[EagleImportResult]:
        try:
            absolute_path = self._validate_import_file(file_path)
            payload = self._validate_import_metadata(metadata)
            checked_folder_identifier = self._validate_folder_identifier(
                folder_identifier
            )
        except _EagleClientError as exc:
            return self._failure(exc)

        connection = self.test_connection()
        if not connection.succeeded:
            return self._copy_failure(connection)

        library = self.get_current_library()
        if not library.succeeded:
            return self._copy_failure(library)

        if checked_folder_identifier is not None:
            folders = self.list_folders()
            if not folders.succeeded or folders.value is None:
                return self._copy_failure(folders)
            valid_identifiers = {folder.identifier for folder in folders.value}
            if checked_folder_identifier not in valid_identifiers:
                return ValueResult.unavailable(
                    "eagle_folder_not_found",
                    f"Eagle folder was not found: {checked_folder_identifier}",
                )

        return self._add_file(absolute_path, checked_folder_identifier, payload)

    def get_items(
        self,
        *,
        item_identifier: str | None = None,
        url: str | None = None,
        annotation: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> ValueResult[EagleItemPage]:
        """Read a filtered page without the server-dependent fields parameter."""
        try:
            filters = {"id": item_identifier, "url": url, "annotation": annotation}
            if not any(value is not None for value in filters.values()):
                raise _EagleClientError("eagle_invalid_item_query", "An explicit item filter is required.")
            query: dict[str, str | int] = {}
            for name, value in filters.items():
                if value is not None:
                    if not isinstance(value, str) or not value.strip():
                        raise _EagleClientError("eagle_invalid_item_query", "Item filters must be non-empty text.")
                    query[name] = value
            if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
                raise _EagleClientError("eagle_invalid_item_query", "Invalid item pagination.")
            query.update(offset=offset, limit=limit)
            data = self._request_data("item/get?" + urlencode(query))
            page = self._parse_item_page(data, expected_offset=offset, expected_limit=limit)
            if item_identifier is not None and any(item.identifier != item_identifier for item in page.items):
                raise _EagleClientError("eagle_invalid_items", "The item response did not match its requested identifier.")
            return ValueResult(succeeded=True, value=page)
        except _EagleClientError as exc:
            return self._failure(exc)

    def add_item_from_file(
        self,
        file_path: Path,
        *,
        folder_identifier: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ValueResult[EagleImportResult]:
        """Send item/add once; the application has checked the captured target."""
        try:
            absolute_path = self._validate_import_file(file_path)
            payload = self._validate_import_metadata(metadata)
            checked_folder_identifier = self._validate_folder_identifier(folder_identifier)
        except _EagleClientError as exc:
            return self._failure(exc)
        return self._add_file(absolute_path, checked_folder_identifier, payload)

    def update_item_metadata(
        self,
        item_identifier: str,
        *,
        tags: tuple[str, ...],
        annotation: str,
    ) -> ValueResult[EagleItemInfo]:
        """Send only id/tags/annotation; never replace a file or folder assignment."""
        try:
            if not isinstance(item_identifier, str) or not item_identifier.strip():
                raise _EagleClientError("eagle_invalid_item_identifier", "Item identifier must not be empty.")
            payload = self._validate_import_metadata({"tags": tags, "annotation": annotation})
            payload["id"] = item_identifier
            data = self._request_data("item/update", method="POST", payload=payload)
            item = self._parse_item(data)
            if item.identifier != item_identifier:
                raise _EagleClientError("eagle_invalid_items", "Updated item identifier did not match the request.")
            return ValueResult(succeeded=True, value=item)
        except _EagleClientError as exc:
            return self._failure(exc)

    @staticmethod
    def _parse_item(data: object) -> EagleItemInfo:
        if not isinstance(data, dict):
            raise _EagleClientError("eagle_invalid_items", "Eagle item must be an object.")
        identifier = data.get("id")
        if not isinstance(identifier, str) or not identifier.strip():
            raise _EagleClientError("eagle_invalid_items", "Eagle item identifier is missing.")
        for name in ("name", "url", "annotation"):
            if not isinstance(data.get(name), str):
                raise _EagleClientError("eagle_invalid_items", f"Eagle item {name} must be text.")
        for name in ("tags", "folders"):
            values = data.get(name)
            if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
                raise _EagleClientError("eagle_invalid_items", f"Eagle item {name} must contain text values.")
        if type(data.get("isDeleted")) is not bool:
            raise _EagleClientError("eagle_invalid_items", "Eagle item isDeleted must be a boolean.")
        return EagleItemInfo(
            identifier, data["name"], data["url"], tuple(data["tags"]),
            tuple(data["folders"]), data["annotation"], data["isDeleted"],
        )

    @classmethod
    def _parse_item_page(
        cls, data: object, *, expected_offset: int, expected_limit: int,
    ) -> EagleItemPage:
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise _EagleClientError("eagle_invalid_items", "Eagle item page is invalid.")
        total, offset, limit = data.get("total"), data.get("offset"), data.get("limit")
        if type(total) is not int or total < 0 or type(offset) is not int or offset != expected_offset:
            raise _EagleClientError("eagle_invalid_items", "Eagle item page has invalid counts.")
        if type(limit) is not int or limit != expected_limit or len(data["data"]) > limit:
            raise _EagleClientError("eagle_invalid_items", "Eagle item page has an unexpected limit.")
        if offset + len(data["data"]) > total:
            raise _EagleClientError("eagle_invalid_items", "Eagle item page exceeds its total.")
        items = tuple(cls._parse_item(value) for value in data["data"])
        if len({item.identifier for item in items}) != len(items):
            raise _EagleClientError("eagle_invalid_items", "Eagle item page contains repeated identifiers.")
        return EagleItemPage(items, total, offset, limit)

    def _add_file(
        self,
        absolute_path: Path,
        checked_folder_identifier: str | None,
        payload: Mapping[str, object],
    ) -> ValueResult[EagleImportResult]:
        request_payload: dict[str, object] = {"path": str(absolute_path)}
        request_payload.update(payload)
        if checked_folder_identifier is not None:
            request_payload["folders"] = [checked_folder_identifier]

        try:
            data = self._request_data(
                "item/add",
                method="POST",
                payload=request_payload,
            )
        except _EagleClientError as exc:
            return self._failure(exc)

        if not isinstance(data, dict):
            return ValueResult.unavailable(
                "eagle_invalid_import_response",
                "Eagle returned an invalid item import response.",
            )
        item_identifier = data.get("id")
        if not isinstance(item_identifier, str) or not item_identifier.strip():
            return ValueResult.unavailable(
                "eagle_missing_item_identifier",
                "Eagle reported success without a valid item identifier.",
            )
        return ValueResult(
            succeeded=True,
            value=EagleImportResult(item_identifier=item_identifier),
        )

    def _request_data(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        payload: Mapping[str, object] | None = None,
    ) -> object:
        headers = {
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }
        body = None
        if payload is not None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = EagleHttpRequest(
            method=method,
            url=self._base_url + endpoint,
            headers=headers,
            body=body,
        )

        try:
            response = self._transport.send(
                request,
                timeout_seconds=self._timeout_seconds,
                maximum_response_bytes=self._maximum_response_bytes,
            )
        except EagleTimeoutError as exc:
            raise _EagleClientError("eagle_timeout", str(exc)) from exc
        except EagleResponseTooLargeError as exc:
            raise _EagleClientError("eagle_response_too_large", str(exc)) from exc
        except TimeoutError as exc:
            raise _EagleClientError(
                "eagle_timeout",
                "The Eagle request timed out.",
            ) from exc
        except (
            EagleConnectionError,
            EagleTransportError,
            HTTPException,
            OSError,
        ) as exc:
            raise _EagleClientError("eagle_connection_error", str(exc)) from exc

        if not 200 <= response.status_code < 300:
            raise _EagleClientError(
                "eagle_http_error",
                f"Eagle returned HTTP {response.status_code}.",
            )

        try:
            text = response.body.decode("utf-8")
            document = json.loads(text, parse_constant=self._reject_json_constant)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            RecursionError,
        ) as exc:
            raise _EagleClientError(
                "eagle_invalid_json",
                "Eagle returned an invalid JSON response.",
            ) from exc
        if not isinstance(document, dict):
            raise _EagleClientError(
                "eagle_invalid_jsend",
                "Eagle returned a non-object JSend response.",
            )

        status = document.get("status")
        if status != "success":
            if status in {"error", "fail"}:
                raise _EagleClientError(
                    "eagle_api_error",
                    self._jsend_error_message(document),
                )
            raise _EagleClientError(
                "eagle_invalid_jsend",
                "Eagle returned an invalid JSend status.",
            )
        if "data" not in document:
            raise _EagleClientError(
                "eagle_invalid_jsend",
                "Eagle returned a success response without data.",
            )
        return document["data"]

    @staticmethod
    def _parse_connection_info(data: object) -> EagleConnectionInfo:
        if not isinstance(data, dict):
            raise _EagleClientError(
                "eagle_invalid_app_info",
                "Eagle returned an invalid application information object.",
            )

        version = data.get("version")
        prerelease_version = data.get("prereleaseVersion")
        build_version = data.get("buildVersion")
        platform = data.get("platform")
        if not isinstance(version, str) or not version.strip():
            raise _EagleClientError(
                "eagle_invalid_app_info",
                "Eagle version must be a non-empty string.",
            )
        if "prereleaseVersion" not in data or (
            prerelease_version is not None
            and not isinstance(prerelease_version, str)
        ):
            raise _EagleClientError(
                "eagle_invalid_app_info",
                "Eagle prereleaseVersion must be a string or null.",
            )
        if "buildVersion" not in data or (
            build_version is not None and not isinstance(build_version, str)
        ):
            raise _EagleClientError(
                "eagle_invalid_app_info",
                "Eagle buildVersion must be a string or null.",
            )
        if not isinstance(platform, str) or not platform.strip():
            raise _EagleClientError(
                "eagle_invalid_app_info",
                "Eagle platform must be a non-empty string.",
            )

        version_match = _VERSION_PATTERN.fullmatch(version)
        if version_match is None:
            raise _EagleClientError(
                "eagle_invalid_app_info",
                f"Eagle returned an unrecognized version: {version}",
            )
        major = int(version_match.group("major"))
        minor = int(version_match.group("minor") or 0)

        build_number = None
        if build_version is not None:
            build_match = _BUILD_PATTERN.fullmatch(build_version)
            if build_match is None:
                raise _EagleClientError(
                    "eagle_invalid_app_info",
                    f"Eagle returned an unrecognized build version: {build_version}",
                )
            build_number = int(build_match.group("number"))

        if (major, minor) < (4, 0) or (
            (major, minor) == (4, 0)
            and (build_number is None or build_number < 21)
        ):
            raise _EagleClientError(
                "unsupported_eagle_version",
                "Eagle Web API V2 requires Eagle 4.0 Build 21 or later.",
            )

        return EagleConnectionInfo(
            version=version,
            prerelease_version=prerelease_version,
            build_version=build_version,
            platform=platform,
            build_number=build_number,
        )

    @staticmethod
    def _parse_folder_page(
        data: object,
    ) -> tuple[list[object], int, int]:
        if not isinstance(data, dict):
            raise _EagleClientError(
                "eagle_invalid_folders",
                "Eagle returned an invalid folder page.",
            )
        entries = data.get("data")
        total = data.get("total")
        offset = data.get("offset")
        limit = data.get("limit")
        if not isinstance(entries, list):
            raise _EagleClientError(
                "eagle_invalid_folders",
                "Eagle folder page data must be an array.",
            )
        if type(total) is not int or total < 0:
            raise _EagleClientError(
                "eagle_invalid_folders",
                "Eagle folder total must be a non-negative integer.",
            )
        if type(offset) is not int or offset < 0:
            raise _EagleClientError(
                "eagle_invalid_folders",
                "Eagle folder offset must be a non-negative integer.",
            )
        if type(limit) is not int or not 1 <= limit <= FOLDER_PAGE_LIMIT or len(entries) > limit:
            raise _EagleClientError(
                "eagle_invalid_folders",
                "Eagle folder limit must be a positive integer.",
            )
        if offset + len(entries) > total:
            raise _EagleClientError(
                "eagle_invalid_folders",
                "Eagle folder page contains more entries than its total.",
            )
        return entries, total, offset

    @classmethod
    def _append_folder_tree(
        cls,
        raw_folder: object,
        *,
        parent_identifier: str | None,
        output: list[EagleFolderInfo],
        seen_identifiers: set[str],
        maximum_folders: int | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        stack: list[tuple[object, str | None]] = [
            (raw_folder, parent_identifier)
        ]
        while stack:
            if cancelled and cancelled():
                raise _EagleClientError("eagle_cancelled", "Eagle folder loading was cancelled.")
            if maximum_folders is not None and len(output) >= maximum_folders:
                raise _EagleClientError("eagle_folder_limit", "Eagle folder node limit was reached.")
            current, current_parent = stack.pop()
            if not isinstance(current, dict):
                raise _EagleClientError(
                    "eagle_invalid_folders",
                    "Eagle folder entries must be objects.",
                )
            identifier = current.get("id")
            name = current.get("name")
            children = current.get("children")
            if not isinstance(identifier, str) or not identifier.strip():
                raise _EagleClientError(
                    "eagle_invalid_folders",
                    "Eagle folder identifiers must be non-empty strings.",
                )
            if not isinstance(name, str) or not name.strip():
                raise _EagleClientError(
                    "eagle_invalid_folders",
                    "Eagle folder names must be non-empty strings.",
                )
            if not isinstance(children, list):
                raise _EagleClientError(
                    "eagle_invalid_folders",
                    "Eagle folder children must be an array.",
                )
            if identifier in seen_identifiers:
                raise _EagleClientError(
                    "eagle_invalid_folders",
                    f"Eagle returned a duplicate folder identifier: {identifier}",
                )
            seen_identifiers.add(identifier)
            output.append(
                EagleFolderInfo(
                    identifier=identifier,
                    name=name,
                    parent_identifier=current_parent,
                )
            )
            for child in reversed(children):
                stack.append((child, identifier))

    @staticmethod
    def _validate_import_file(file_path: Path) -> Path:
        try:
            path = Path(file_path).expanduser()
        except (TypeError, ValueError) as exc:
            raise _EagleClientError(
                "eagle_invalid_file",
                "The Eagle import path is invalid.",
            ) from exc
        if str(path).startswith("\\\\"):
            raise _EagleClientError(
                "eagle_network_file_unsupported",
                "Eagle imports require a local file path.",
            )
        try:
            if not path.exists():
                raise _EagleClientError(
                    "eagle_file_not_found",
                    f"Eagle import file does not exist: {path}",
                )
            if not path.is_file():
                raise _EagleClientError(
                    "eagle_not_a_file",
                    f"Eagle import path is not a regular file: {path}",
                )
            if path.stat().st_size <= 0:
                raise _EagleClientError(
                    "eagle_empty_file",
                    f"Eagle import file is empty: {path}",
                )
            absolute_path = path.resolve(strict=True)
        except _EagleClientError:
            raise
        except OSError as exc:
            raise _EagleClientError(
                "eagle_file_validation_failed",
                f"Unable to inspect Eagle import file: {exc}",
            ) from exc
        if str(absolute_path).startswith("\\\\"):
            raise _EagleClientError(
                "eagle_network_file_unsupported",
                "Eagle imports require a local file path.",
            )
        return absolute_path

    @staticmethod
    def _validate_import_metadata(
        metadata: Mapping[str, object] | None,
    ) -> dict[str, object]:
        if metadata is None:
            return {}
        if not isinstance(metadata, Mapping):
            raise _EagleClientError(
                "eagle_invalid_metadata",
                "Eagle import metadata must be a mapping.",
            )
        unknown_fields = set(metadata) - _ALLOWED_METADATA_FIELDS
        if unknown_fields:
            names = ", ".join(sorted(str(field) for field in unknown_fields))
            raise _EagleClientError(
                "eagle_invalid_metadata",
                f"Unsupported Eagle import metadata fields: {names}",
            )

        validated: dict[str, object] = {}
        for field in _STRING_METADATA_FIELDS:
            if field not in metadata:
                continue
            value = metadata[field]
            if not isinstance(value, str):
                raise _EagleClientError(
                    "eagle_invalid_metadata",
                    f"Eagle import metadata field '{field}' must be a string.",
                )
            validated[field] = value

        if "tags" in metadata:
            tags = metadata["tags"]
            if isinstance(tags, (str, bytes)) or not isinstance(tags, Sequence):
                raise _EagleClientError(
                    "eagle_invalid_metadata",
                    "Eagle import tags must be an array of strings.",
                )
            validated_tags: list[str] = []
            for tag in tags:
                if not isinstance(tag, str) or not tag.strip():
                    raise _EagleClientError(
                        "eagle_invalid_metadata",
                        "Every Eagle import tag must be a non-empty string.",
                    )
                validated_tags.append(tag)
            validated["tags"] = validated_tags
        return validated

    @staticmethod
    def _validate_folder_identifier(folder_identifier: str | None) -> str | None:
        if folder_identifier is None:
            return None
        if not isinstance(folder_identifier, str) or not folder_identifier.strip():
            raise _EagleClientError(
                "eagle_invalid_folder_identifier",
                "Eagle folder identifier must be a non-empty string.",
            )
        return folder_identifier

    @staticmethod
    def _validate_base_url(base_url: str) -> str:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("Eagle base_url must be a non-empty string.")
        if "\r" in base_url or "\n" in base_url:
            raise ValueError("Eagle base_url must not contain line breaks.")
        parts = urlsplit(base_url)
        try:
            port = parts.port
        except ValueError as exc:
            raise ValueError("Eagle base_url contains an invalid port.") from exc
        if parts.scheme.lower() != "http":
            raise ValueError("Eagle base_url must use HTTP.")
        if parts.username is not None or parts.password is not None:
            raise ValueError("Eagle base_url must not contain credentials.")
        if parts.hostname is None or parts.hostname.casefold() not in _ALLOWED_HOSTS:
            raise ValueError("Eagle base_url must use localhost, 127.0.0.1, or ::1.")
        if parts.query or parts.fragment:
            raise ValueError("Eagle base_url must not contain a query or fragment.")
        if port is None:
            netloc = parts.hostname
            if parts.hostname == "::1":
                netloc = "[::1]"
        else:
            host = parts.hostname
            if host == "::1":
                host = "[::1]"
            netloc = f"{host}:{port}"
        if parts.path not in {"/api/v2", "/api/v2/"}:
            raise ValueError("Eagle base_url path must be exactly /api/v2/.")
        return urlunsplit(("http", netloc, "/api/v2/", "", ""))

    @staticmethod
    def _jsend_error_message(document: Mapping[str, object]) -> str:
        message = document.get("message")
        if isinstance(message, str) and message.strip():
            return message
        data = document.get("data")
        if isinstance(data, Mapping):
            nested_message = data.get("message")
            if isinstance(nested_message, str) and nested_message.strip():
                return nested_message
        return "Eagle returned an API error."

    @staticmethod
    def _looks_like_no_library(message: str) -> bool:
        lowered = message.casefold()
        return (
            "library" in lowered
            and any(word in lowered for word in ("open", "found", "available"))
        ) or "素材库" in message

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError(f"Non-standard JSON constant: {value}")

    @staticmethod
    def _failure(error: _EagleClientError) -> ValueResult[Any]:
        return ValueResult.unavailable(error.code, str(error))

    @staticmethod
    def _copy_failure(result: ValueResult[Any]) -> ValueResult[Any]:
        return ValueResult.unavailable(
            result.code or "eagle_error",
            result.message or "Eagle operation failed.",
        )
