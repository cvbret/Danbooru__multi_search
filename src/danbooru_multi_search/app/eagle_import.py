"""Single-post Eagle preflight, exact duplicate checks and confirmed writes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from danbooru_multi_search.app.eagle_import_files import (
    CheckedImportFile, assert_file_unchanged, check_import_file,
)
from danbooru_multi_search.app.eagle_import_metadata import (
    annotation_markers, build_annotation, import_name, import_tags, merged_tags,
)
from danbooru_multi_search.app.eagle_import_types import (
    EaglePostImportError, EaglePostImportOutcome, EaglePostImportRequest,
    normalized_post_url,
)
from danbooru_multi_search.integrations.eagle.contracts import EagleBridge, EagleItemInfo
from danbooru_multi_search.storage.database import DatabaseError
from danbooru_multi_search.storage.eagle_imports import EagleImportRecord, EagleImportRepository


class EagleImportBackend(Protocol):
    bridge: EagleBridge
    post_sent: Callable[[], bool]
    imports: EagleImportRepository | None
    validate_media: Callable[[Path, str], None] | None


@dataclass(frozen=True, slots=True)
class _Candidate:
    item: EagleItemInfo
    reason: str
    verified_hash: bool = False
    known_sha256: str | None = None


class _PostImporter:
    def __init__(
        self, request: EaglePostImportRequest, backend: EagleImportBackend,
        cancelled: Callable[[], bool],
    ) -> None:
        self.request = request
        self.backend = backend
        self.bridge = backend.bridge
        self.repository = backend.imports
        self.cancelled = cancelled
        self.page_url = normalized_post_url(request.post_url, request.post.id)
        self.checked: CheckedImportFile | None = None
        self.token: str | None = None
        self.item_identifier: str | None = None
        self.recovered_paths: list[Path] = []
        self.unresolved_previous = False
        self.pending_file: Path | None = None
        self.pending_sha256: str | None = None

    def check(self) -> None:
        if self.cancelled():
            raise EaglePostImportError("eagle_cancelled", "The Eagle import was cancelled.")

    def take(self, call):
        self.check()
        value = call()
        self.check()
        if not value.succeeded or value.value is None:
            raise EaglePostImportError(value.code or "eagle_error", value.message or "Eagle request failed.")
        return value.value

    def same_library(self) -> None:
        current = self.take(self.bridge.get_current_library)
        if current.identifier != self.request.library_identifier:
            raise EaglePostImportError("eagle_library_changed", "The captured Eagle library is no longer current.")

    def target(self) -> None:
        self.take(self.bridge.test_connection)
        self.same_library()
        folders = self.take(lambda: self.bridge.list_folders(
            maximum_pages=20, maximum_folders=20000, cancelled=self.cancelled,
        ))
        folder = self.request.folder_identifier
        if folder is not None and folder not in {item.identifier for item in folders}:
            raise EaglePostImportError("eagle_folder_not_found", "The captured target folder no longer exists.")
        self.same_library()

    def search(self, **filters) -> tuple[EagleItemInfo, ...]:
        offset = 0
        total = None
        items: list[EagleItemInfo] = []
        seen: set[str] = set()
        for _ in range(20):
            page = self.take(lambda: self.bridge.get_items(**filters, offset=offset, limit=100))
            if page.offset != offset or (total is not None and page.total != total):
                raise EaglePostImportError("eagle_search_incomplete", "The item search changed while being read.")
            total = page.total
            if not page.items and offset < total:
                raise EaglePostImportError("eagle_search_incomplete", "The item search returned an incomplete page.")
            for item in page.items:
                if item.identifier in seen:
                    raise EaglePostImportError("eagle_search_incomplete", "The item search repeated a previous identifier.")
                seen.add(item.identifier)
                items.append(item)
            offset += len(page.items)
            if offset >= total:
                return tuple(items)
        raise EaglePostImportError("eagle_search_incomplete", "The bounded item search did not reach its end.")

    def one(self, identifier: str) -> EagleItemInfo | None:
        items = self.search(item_identifier=identifier)
        if any(item.identifier != identifier for item in items) or len(items) > 1:
            raise EaglePostImportError("eagle_duplicate_conflict", "An item response did not match its requested identifier.")
        return items[0] if items else None

    def markers(self, item: EagleItemInfo):
        return tuple(marker for marker in annotation_markers(item.annotation)
                     if marker.library_id == self.request.library_identifier)

    def known_hash(self, item: EagleItemInfo) -> str | None:
        hashes = {marker.sha256 for marker in self.markers(item) if marker.sha256 is not None}
        if len(hashes) > 1:
            raise EaglePostImportError("eagle_duplicate_conflict", "The item contains conflicting application content hashes.")
        return next(iter(hashes), None)

    def candidates(self, digest: str | None) -> _Candidate | None:
        self.check()
        output: dict[str, _Candidate] = {}
        cache: dict[str, EagleItemInfo | None] = {}

        def add(candidate: _Candidate) -> None:
            previous = output.get(candidate.item.identifier)
            if previous is not None:
                if previous.known_sha256 and candidate.known_sha256 and previous.known_sha256 != candidate.known_sha256:
                    raise EaglePostImportError("eagle_duplicate_conflict", "Local and remote item hashes disagree.")
                candidate = _Candidate(
                    candidate.item, previous.reason,
                    previous.verified_hash or candidate.verified_hash,
                    previous.known_sha256 or candidate.known_sha256,
                )
            output[candidate.item.identifier] = candidate

        record = self.repository.find_by_post(self.request.library_identifier, self.request.post.id)
        records = [record] if record is not None else []
        if digest is not None:
            records.extend(self.repository.find_by_hash(self.request.library_identifier, digest))
        for saved in records:
            self.check()
            if saved.post_id == self.request.post.id and normalized_post_url(saved.page_url, saved.post_id) != self.page_url:
                raise EaglePostImportError("eagle_duplicate_conflict", "The local post belongs to a different source URL.")
            if saved.item_id not in cache:
                cache[saved.item_id] = self.one(saved.item_id)
            item = cache[saved.item_id]
            if item is None:
                if saved is record:
                    self.same_library()
                    if not self.repository.forget(saved):
                        raise EaglePostImportError("eagle_duplicate_conflict", "The stale local association changed.")
                continue
            valid_note = any(
                marker.post_id == saved.post_id
                and marker.page_url == normalized_post_url(saved.page_url, saved.post_id)
                and (saved.sha256 is None or marker.sha256 == saved.sha256)
                for marker in self.markers(item)
            )
            valid_hash_note = saved.sha256 is not None and any(
                marker.sha256 == saved.sha256
                and marker.page_url == normalized_post_url(item.url)
                for marker in self.markers(item)
            )
            if normalized_post_url(item.url) != normalized_post_url(saved.page_url) and not (valid_note or valid_hash_note):
                raise EaglePostImportError("eagle_duplicate_conflict", "The saved item identity is no longer consistent.")
            remote_hash = self.known_hash(item)
            if saved.sha256 and remote_hash and saved.sha256 != remote_hash:
                raise EaglePostImportError("eagle_duplicate_conflict", "The saved and annotated hashes disagree.")
            add(_Candidate(
                item, "local_mapping" if saved.post_id == self.request.post.id else "file_hash",
                digest is not None and saved.sha256 == digest, saved.sha256 or remote_hash,
            ))

        for item in self.search(url=self.page_url):
            if normalized_post_url(item.url) == self.page_url:
                known = self.known_hash(item)
                add(_Candidate(item, "post_url", digest is not None and known == digest, known))
        if digest is not None:
            for item in self.search(annotation=digest):
                markers = self.markers(item)
                known = self.known_hash(item)
                anchored = any(
                    marker.page_url == normalized_post_url(item.url) and marker.sha256 == digest
                    for marker in markers
                )
                if anchored and known == digest:
                    add(_Candidate(item, "file_hash", True, digest))
        self.same_library()
        if len(output) > 1:
            raise EaglePostImportError("eagle_duplicate_conflict", "The duplicate checks returned different Eagle item identifiers.")
        return next(iter(output.values()), None)

    def resolve_attempts(self, digest: str | None) -> None:
        attempts = self.repository.find_attempts(self.request.library_identifier, self.request.post.id, digest)
        for attempt in attempts:
            self.unresolved_previous = True
            self.pending_file = attempt.file_path
            self.pending_sha256 = attempt.sha256
            self.check()
            if not attempt.page_url:
                raise EaglePostImportError("eagle_unresolved_attempt", "A previous write has no captured source URL.")
            found: dict[str, tuple[EagleItemInfo, object]] = {}
            for item in self.search(annotation=attempt.token):
                for marker in self.markers(item):
                    if (
                        marker.attempt_token == attempt.token and marker.post_id == attempt.post_id
                        and marker.page_url == normalized_post_url(attempt.page_url, attempt.post_id)
                        and marker.source_sha256 == attempt.sha256
                    ):
                        found[item.identifier] = item, marker
            self.same_library()
            if len(found) != 1:
                code = "eagle_duplicate_conflict" if len(found) > 1 else "eagle_unresolved_attempt"
                raise EaglePostImportError(code, "The previous Eagle write cannot be uniquely confirmed; no write was repeated.")
            item, marker = next(iter(found.values()))
            self.known_hash(item)
            if normalized_post_url(item.url) != marker.page_url:
                anchored_hash = marker.sha256 is not None and any(
                    other.page_url == normalized_post_url(item.url)
                    and other.sha256 == marker.sha256
                    for other in self.markers(item)
                )
                if not anchored_hash:
                    raise EaglePostImportError(
                        "eagle_duplicate_conflict",
                        "The previous write no longer matches the item's source URL.",
                    )
            self.item_identifier = item.identifier
            record = EagleImportRecord(
                attempt.library_id, attempt.post_id, item.identifier, attempt.page_url,
                attempt.sha256 if marker.sha256 == attempt.sha256 else None,
            )
            self.check()
            self.repository.save(record, attempt_token=attempt.token)
            if attempt.file_path is not None and attempt.file_path not in self.recovered_paths:
                self.recovered_paths.append(attempt.file_path)
            self.unresolved_previous = False
            self.pending_file = None
            self.pending_sha256 = None

    def save_candidate(self, candidate: _Candidate, digest: str | None, *, token: str | None = None) -> None:
        self.check()
        self.repository.save(EagleImportRecord(
            self.request.library_identifier, self.request.post.id, candidate.item.identifier,
            self.page_url, digest if candidate.verified_hash else None,
        ), attempt_token=token)
        if token is not None:
            self.token = None

    def result(self, state, *, candidate: _Candidate | None = None, **changes) -> EaglePostImportOutcome:
        return EaglePostImportOutcome(
            state, self.item_identifier or (candidate.item.identifier if candidate else None),
            changes.pop("reason", candidate.reason if candidate else ""),
            self.pending_sha256 if self.unresolved_previous else (
                self.checked.sha256 if self.checked else (candidate.known_sha256 if candidate else None)
            ),
            self.pending_file if self.unresolved_previous else (
                self.checked.path if self.checked else self.request.file_path
            ),
            recovered_paths=tuple(self.recovered_paths), post_started=self.backend.post_sent(), **changes,
        )

    def run(self, *, precheck: bool) -> EaglePostImportOutcome:
        if self.repository is None:
            raise EaglePostImportError("eagle_import_storage_error", "The import repository was not configured.")
        tags = import_tags(self.request)
        self.target()
        digest = None
        if not precheck:
            if self.request.file_path is None:
                raise EaglePostImportError("eagle_file_invalid", "A checked local original file is required.")
            self.checked = check_import_file(
                self.request.file_path,
                extension=self.request.post.file_ext,
                expected_size=self.request.post.file_size if self.request.post.file_size else None,
                cancelled=self.cancelled, validate_media=self.backend.validate_media,
            )
            digest = self.checked.sha256
        self.resolve_attempts(digest)
        candidate = self.candidates(digest)
        self.item_identifier = None
        if candidate and (not self.request.update_duplicates or candidate.item.is_deleted):
            self.save_candidate(candidate, digest)
            return self.result("skipped", candidate=candidate,
                               reason="item_in_trash" if candidate.item.is_deleted else candidate.reason)
        if precheck:
            return self.result("ready", candidate=candidate)
        self.check()
        self.token = self.repository.begin_attempt(
            self.request.library_identifier, self.request.post.id, digest,
            file_path=self.checked.path, page_url=self.page_url,
        )
        if self.token is None:
            self.unresolved_previous = True
            raise EaglePostImportError("eagle_unresolved_attempt", "Another import owns this post or content; no write was started.")
        # A previous owner may have completed between our empty lookup and claim.
        # Always rebuild both local and remote facts after acquiring our claim.
        candidate = self.candidates(digest)
        if candidate and (not self.request.update_duplicates or candidate.item.is_deleted):
            self.save_candidate(candidate, digest, token=self.token)
            return self.result("skipped", candidate=candidate,
                               reason="item_in_trash" if candidate.item.is_deleted else candidate.reason)
        assert_file_unchanged(self.checked)
        self.target()
        self.check()
        annotation = build_annotation(
            self.request,
            sha256=candidate.known_sha256 if candidate else digest,
            source_sha256=digest, attempt_token=self.token,
            previous=candidate.item.annotation if candidate else "",
        )
        assert_file_unchanged(self.checked)
        if candidate:
            self.item_identifier = candidate.item.identifier
            written = self.bridge.update_item_metadata(
                candidate.item.identifier, tags=merged_tags(candidate.item.tags, tags), annotation=annotation,
            )
        else:
            self.item_identifier = None
            written = self.bridge.add_item_from_file(
                self.checked.path, folder_identifier=self.request.folder_identifier,
                metadata={"name": import_name(self.request), "website": self.page_url,
                          "tags": tags, "annotation": annotation},
            )
        if written.succeeded and written.value is not None:
            self.item_identifier = (
                written.value.identifier if candidate else written.value.item_identifier
            )
        self.check()
        if not written.succeeded or not self.item_identifier:
            raise EaglePostImportError(written.code or "eagle_missing_item_identifier", written.message or "Eagle did not confirm the item identifier.")
        self.same_library()
        assert_file_unchanged(self.checked)
        self.check()
        record = EagleImportRecord(
            self.request.library_identifier, self.request.post.id, self.item_identifier,
            self.page_url, digest if candidate is None or candidate.verified_hash else None,
        )
        self.repository.save(record, attempt_token=self.token)
        self.token = None
        return self.result("updated" if candidate else "imported", candidate=candidate)


def execute_eagle_post_import(
    request: EaglePostImportRequest,
    backend: EagleImportBackend,
    cancelled: Callable[[], bool],
    *,
    precheck: bool,
) -> EaglePostImportOutcome:
    """Never automatically repeat a mutation, including after an uncertain result.

    Library checks detect observed switches; Eagle has no atomic expected-library
    condition. Current response identities and complete markers are rechecked.
    """
    operation = _PostImporter(request, backend, cancelled)
    try:
        return operation.run(precheck=precheck)
    except (EaglePostImportError, DatabaseError) as exc:
        started = backend.post_sent()
        diagnostic = str(exc)
        if operation.token is not None and not started:
            try:
                operation.repository.release_attempt(operation.token)
                operation.token = None
            except DatabaseError as cleanup:
                diagnostic += f" Reservation cleanup failed: {cleanup}"
        code = exc.code if isinstance(exc, EaglePostImportError) else "eagle_import_storage_error"
        uncertain = started or operation.unresolved_previous
        return operation.result(
            "uncertain" if uncertain else "error", error_code=code,
            diagnostic=diagnostic, uncertain=uncertain,
        )
