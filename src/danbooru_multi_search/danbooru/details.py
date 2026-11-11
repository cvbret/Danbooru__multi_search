"""Work metadata, media fallback, and tags independent of the source page."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from enum import IntEnum, StrEnum

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    Property,
    QUrl,
    QUrlQuery,
    Qt,
    Signal,
    Slot,
)

from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.danbooru.tag_translation import TagTranslationStore


_STATIC_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp", "avif"})
_RESTRICTED_EXTENSIONS = frozenset({"webm", "mp4", "zip", "swf"})
_RATING_LABELS = {
    "g": "General",
    "s": "Sensitive",
    "q": "Questionable",
    "e": "Explicit",
}
_QUALITY_LABELS = {
    "preview": "预览图",
    "large": "较大图片",
    "original": "原图",
}
_SOURCE_FIELDS = {
    "preview": "preview_file_url",
    "large": "large_file_url",
    "original": "file_url",
}
_TAG_GROUPS = (
    ("artist", "画师", "artist_tags"),
    ("character", "角色", "character_tags"),
    ("copyright", "作品系列", "copyright_tags"),
    ("general", "普通标签", "general_tags"),
    ("meta", "元数据", "meta_tags"),
)


class MediaQuality(StrEnum):
    PREVIEW = "preview"
    LARGE = "large"
    ORIGINAL = "original"


class DetailMediaState(StrEnum):
    IDLE = "idle"
    LOADING = "loading"
    FALLBACK = "fallback"
    SUCCESS = "success"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class MediaCandidate:
    source_field: str
    quality: MediaQuality
    url: str
    component: str
    same_as_original: bool = False

    @property
    def quality_text(self) -> str:
        text = _QUALITY_LABELS[self.quality.value]
        if self.same_as_original:
            return f"{text}（与原图相同）"
        return text


@dataclass(frozen=True, slots=True)
class DetailTagEntry:
    name: str
    chinese_names: tuple[str, ...]

    @property
    def chinese_text(self) -> str:
        return "、".join(self.chinese_names) if self.chinese_names else "暂无中文名称"


@dataclass(frozen=True, slots=True)
class DetailTagGroup:
    key: str
    label: str
    entries: tuple[DetailTagEntry, ...]



def format_file_size_bytes(value: int | None) -> str:
    return "暂无数据" if value is None else f"{value} 字节"


def format_file_size_readable(value: int | None) -> str:
    if value is None:
        return "暂无数据"
    if value < 1024:
        return f"{value} B"
    if value < 1024**2:
        divisor = 1024
        unit = "KiB"
    elif value < 1024**3:
        divisor = 1024**2
        unit = "MiB"
    else:
        divisor = 1024**3
        unit = "GiB"
    scaled, remainder = divmod(value * 100, divisor)
    if remainder * 2 >= divisor:
        scaled += 1
    whole, fraction = divmod(scaled, 100)
    return f"{whole}.{fraction:02d} {unit}"


def format_local_created_at(
    value: str | None,
    *,
    local_timezone: tzinfo | None = None,
) -> str:
    if value is None:
        return "暂无数据"
    utc_value = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    try:
        local_value = utc_value.astimezone(local_timezone)
    except (OverflowError, OSError):
        local_value = utc_value
    offset = local_value.strftime("%z")
    formatted_offset = f"{offset[:3]}:{offset[3:]}"
    return (
        f"{local_value.year:04d}-{local_value.month:02d}-{local_value.day:02d} "
        f"{local_value.hour:02d}:{local_value.minute:02d}:"
        f"{local_value.second:02d} {formatted_offset}"
    )


def build_post_page_url(base_url: str, post_id: int) -> str:
    if not isinstance(post_id, int) or isinstance(post_id, bool) or post_id < 1:
        raise ValueError("post_id must be a positive integer")
    url = QUrl(base_url)
    if (
        not url.isValid()
        or url.scheme() not in {"http", "https"}
        or not url.host()
    ):
        raise ValueError("Danbooru base URL must be a valid HTTP or HTTPS URL.")
    url.setQuery(QUrlQuery())
    url.setFragment(None)
    base_path = url.path(QUrl.ComponentFormattingOption.FullyEncoded).rstrip("/")
    url.setPath(
        f"{base_path}/posts/{post_id}",
        QUrl.ParsingMode.TolerantMode,
    )
    return url.toString()


def build_media_candidates(
    post: DanbooruPost,
    requested_quality: MediaQuality | str,
) -> tuple[MediaCandidate, ...]:
    try:
        requested = MediaQuality(requested_quality)
    except ValueError as exc:
        raise ValueError("Unsupported media quality.") from exc

    extension = post.file_ext.casefold()
    restricted = extension in _RESTRICTED_EXTENSIONS or (
        extension not in _STATIC_EXTENSIONS and extension != "gif"
    )
    if restricted:
        order = (MediaQuality.PREVIEW,)
    else:
        order = {
            MediaQuality.PREVIEW: (
                MediaQuality.PREVIEW,
                MediaQuality.LARGE,
                MediaQuality.ORIGINAL,
            ),
            MediaQuality.LARGE: (
                MediaQuality.LARGE,
                MediaQuality.ORIGINAL,
                MediaQuality.PREVIEW,
            ),
            MediaQuality.ORIGINAL: (
                MediaQuality.ORIGINAL,
                MediaQuality.LARGE,
                MediaQuality.PREVIEW,
            ),
        }[requested]

    urls = {
        MediaQuality.PREVIEW: post.preview_url,
        MediaQuality.LARGE: post.large_file_url,
        MediaQuality.ORIGINAL: post.file_url,
    }
    seen: set[str] = set()
    candidates: list[MediaCandidate] = []
    for quality in order:
        url = urls[quality]
        if not url or url in seen:
            continue
        seen.add(url)
        component = (
            "animated"
            if extension == "gif" and quality != MediaQuality.PREVIEW
            else "image"
        )
        candidates.append(
            MediaCandidate(
                source_field=_SOURCE_FIELDS[quality.value],
                quality=quality,
                url=url,
                component=component,
                same_as_original=(
                    quality == MediaQuality.LARGE
                    and post.file_url is not None
                    and url == post.file_url
                ),
            )
        )
    return tuple(candidates)


class DetailTagGroupRole(IntEnum):
    GROUP_KEY = int(Qt.ItemDataRole.UserRole) + 1
    GROUP_LABEL = GROUP_KEY + 1
    TAG_ITEMS = GROUP_KEY + 2


class DetailTagGroupListModel(QAbstractListModel):
    countChanged = Signal()
    revisionChanged = Signal()

    _ROLE_NAMES = {
        DetailTagGroupRole.GROUP_KEY: QByteArray(b"groupKey"),
        DetailTagGroupRole.GROUP_LABEL: QByteArray(b"groupLabel"),
        DetailTagGroupRole.TAG_ITEMS: QByteArray(b"tagItems"),
    }

    def __init__(
        self,
        translation_store: TagTranslationStore,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._translation_store = translation_store
        self._post: DanbooruPost | None = None
        self._groups = self._build_groups()
        self._revision = 0
        translation_store.snapshotChanged.connect(self.refreshTranslations)

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._groups)

    @Property(int, notify=revisionChanged)
    def revision(self) -> int:
        return self._revision

    @property
    def groups(self) -> tuple[DetailTagGroup, ...]:
        return self._groups

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._groups)

    def roleNames(self) -> dict[int, QByteArray]:
        return {int(role): name for role, name in self._ROLE_NAMES.items()}

    def data(self, index: QModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)):
        if not index.isValid() or not 0 <= index.row() < len(self._groups):
            return None
        group = self._groups[index.row()]
        try:
            detail_role = DetailTagGroupRole(role)
        except ValueError:
            return None
        values = {
            DetailTagGroupRole.GROUP_KEY: group.key,
            DetailTagGroupRole.GROUP_LABEL: group.label,
            DetailTagGroupRole.TAG_ITEMS: [
                {
                    "tagName": entry.name,
                    "chineseNameText": entry.chinese_text,
                }
                for entry in group.entries
            ],
        }
        return values[detail_role]

    def set_post(self, post: DanbooruPost | None) -> None:
        self._post = post
        self.refreshTranslations()

    @Slot()
    def refreshTranslations(self) -> None:
        self.beginResetModel()
        self._groups = self._build_groups()
        self._revision += 1
        self.endResetModel()
        self.countChanged.emit()
        self.revisionChanged.emit()

    @Slot(int, result="QVariantMap")
    def groupAt(self, row: int) -> dict[str, object]:
        if not 0 <= row < len(self._groups):
            return {}
        group = self._groups[row]
        return {
            "groupKey": group.key,
            "groupLabel": group.label,
            "tagItems": [
                {
                    "tagName": entry.name,
                    "chineseNameText": entry.chinese_text,
                }
                for entry in group.entries
            ],
        }

    def contains(self, tag_name: str) -> bool:
        return any(
            entry.name == tag_name
            for group in self._groups
            for entry in group.entries
        )

    def _build_groups(self) -> tuple[DetailTagGroup, ...]:
        post = self._post
        groups: list[DetailTagGroup] = []
        for key, label, attribute_name in _TAG_GROUPS:
            names = getattr(post, attribute_name, ()) if post is not None else ()
            groups.append(
                DetailTagGroup(
                    key=key,
                    label=label,
                    entries=tuple(
                        DetailTagEntry(
                            name=name,
                            chinese_names=self._translation_store.chinese_names(name),
                        )
                        for name in names
                    ),
                )
            )
        return tuple(groups)


class DanbooruDetailController(QObject):
    openChanged = Signal()
    detailChanged = Signal()
    mediaChanged = Signal()
    tagSearchRequested = Signal(str)

    def __init__(
        self,
        base_url: str,
        translation_store: TagTranslationStore,
        *,
        local_timezone: tzinfo | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        build_post_page_url(base_url, 1)
        self._base_url = base_url
        self._local_timezone = local_timezone
        self._translation_store = translation_store
        self._tags_model = DetailTagGroupListModel(translation_store, self)
        self._current_post: DanbooruPost | None = None
        self._is_open = False
        self._shutdown = False
        self._operation_generation = 0
        self._requested_quality = MediaQuality.LARGE
        self._actual_quality: MediaQuality | None = None
        self._actual_quality_text = "暂无数据"
        self._media_generation = 0
        self._candidate_attempt = 0
        self._candidates: tuple[MediaCandidate, ...] = ()
        self._candidate_index = -1
        self._media_state = DetailMediaState.IDLE
        self._media_message = ""

    @property
    def current_post(self) -> DanbooruPost | None:
        return self._current_post

    @property
    def translation_store(self) -> TagTranslationStore:
        return self._translation_store

    @Property(bool, notify=openChanged)
    def isOpen(self) -> bool:
        return self._is_open

    @Property(QObject, constant=True)
    def tagsModel(self) -> QObject:
        return self._tags_model

    @Property(str, notify=detailChanged)
    def postIdText(self) -> str:
        return str(self._current_post.id) if self._current_post is not None else ""

    @Property(str, notify=detailChanged)
    def createdAtText(self) -> str:
        return format_local_created_at(
            self._current_post.created_at if self._current_post is not None else None,
            local_timezone=self._local_timezone,
        )

    @Property(str, notify=detailChanged)
    def dimensionsText(self) -> str:
        post = self._current_post
        return f"{post.width} × {post.height}" if post is not None else "暂无数据"

    @Property(str, notify=detailChanged)
    def fileSizeBytesText(self) -> str:
        return format_file_size_bytes(
            self._current_post.file_size if self._current_post is not None else None
        )

    @Property(str, notify=detailChanged)
    def fileSizeReadableText(self) -> str:
        return format_file_size_readable(
            self._current_post.file_size if self._current_post is not None else None
        )

    @Property(str, notify=detailChanged)
    def fileExtensionText(self) -> str:
        return self._current_post.file_ext.upper() if self._current_post else ""

    @Property(str, notify=detailChanged)
    def ratingText(self) -> str:
        return _RATING_LABELS.get(self._current_post.rating, "") if self._current_post else ""

    @Property(str, notify=detailChanged)
    def scoreText(self) -> str:
        value = self._current_post.score if self._current_post is not None else None
        return str(value) if value is not None else "暂无数据"

    @Property(str, notify=detailChanged)
    def favoriteText(self) -> str:
        value = self._current_post.fav_count if self._current_post is not None else None
        return str(value) if value is not None else "暂无数据"

    @Property(str, notify=detailChanged)
    def postUrl(self) -> str:
        if self._current_post is None:
            return ""
        return build_post_page_url(self._base_url, self._current_post.id)

    @Property(str, notify=detailChanged)
    def sourceText(self) -> str:
        source = self._current_post.source if self._current_post is not None else None
        return source if source is not None else "暂无数据"

    @Property(str, notify=detailChanged)
    def originalFileUrl(self) -> str:
        if self._current_post is None or self._current_post.file_url is None:
            return "暂无数据"
        return self._current_post.file_url

    @Property(str, notify=mediaChanged)
    def requestedQuality(self) -> str:
        return self._requested_quality.value

    @Property(str, notify=mediaChanged)
    def requestedQualityText(self) -> str:
        return _QUALITY_LABELS[self._requested_quality.value]

    @Property(str, notify=mediaChanged)
    def actualQualityText(self) -> str:
        return self._actual_quality_text

    @Property(int, notify=mediaChanged)
    def mediaGeneration(self) -> int:
        return self._media_generation

    @Property(int, notify=mediaChanged)
    def candidateAttempt(self) -> int:
        return self._candidate_attempt

    @Property(str, notify=mediaChanged)
    def mediaUrl(self) -> str:
        candidate = self._current_candidate
        return candidate.url if candidate is not None else ""

    @Property(str, notify=mediaChanged)
    def mediaComponent(self) -> str:
        candidate = self._current_candidate
        return candidate.component if candidate is not None else "none"

    @Property(str, notify=mediaChanged)
    def mediaState(self) -> str:
        return self._media_state.value

    @Property(str, notify=mediaChanged)
    def mediaMessage(self) -> str:
        return self._media_message

    @Property(bool, notify=mediaChanged)
    def canRetryMedia(self) -> bool:
        return self._is_open and self._media_state == DetailMediaState.ERROR

    @Property(bool, notify=mediaChanged)
    def restrictedFormat(self) -> bool:
        post = self._current_post
        if post is None:
            return False
        extension = post.file_ext.casefold()
        return extension in _RESTRICTED_EXTENSIONS or (
            extension not in _STATIC_EXTENSIONS and extension != "gif"
        )

    @Property(str, notify=detailChanged)
    def formatState(self) -> str:
        post = self._current_post
        if post is None:
            return "none"
        extension = post.file_ext.casefold()
        if extension in _RESTRICTED_EXTENSIONS:
            return "restricted"
        if extension not in _STATIC_EXTENSIONS and extension != "gif":
            return "unknown"
        return "supported"

    @Property(str, notify=detailChanged)
    def formatStatusText(self) -> str:
        if self.formatState == "restricted":
            return f"受限格式：{self.fileExtensionText}"
        if self.formatState == "unknown":
            return f"未知格式：{self.fileExtensionText}"
        return ""

    def open_post(self, post: DanbooruPost) -> bool:
        """Display an already available work without fetching its metadata."""

        if self._shutdown or not isinstance(post, DanbooruPost):
            return False
        self._operation_generation += 1
        operation_generation = self._operation_generation
        self._current_post = post
        self._is_open = True
        self._requested_quality = MediaQuality.LARGE
        self._start_media_generation(emit_signal=False)
        self._tags_model.set_post(post)
        if not self._is_current_open(operation_generation, post):
            return False
        self.openChanged.emit()
        if not self._is_current_open(operation_generation, post):
            return False
        self.detailChanged.emit()
        if not self._is_current_open(operation_generation, post):
            return False
        self.mediaChanged.emit()
        return self._is_current_open(operation_generation, post)

    @Slot(result=bool)
    def closeDetail(self) -> bool:
        if self._shutdown or not self._is_open:
            return False
        self._operation_generation += 1
        operation_generation = self._operation_generation
        post = self._current_post
        self._is_open = False
        self._invalidate_media()
        if not self._is_current_close(operation_generation, post):
            return False
        self.openChanged.emit()
        return self._is_current_close(operation_generation, post)

    @Slot(str, result=bool)
    def requestQuality(self, quality: str) -> bool:
        if self._shutdown or not self._is_open or self._current_post is None:
            return False
        try:
            requested = MediaQuality(quality)
        except ValueError:
            return False
        if requested == self._requested_quality:
            return False
        self._operation_generation += 1
        self._requested_quality = requested
        self._start_media_generation()
        return True

    @Slot(result=bool)
    def retryMedia(self) -> bool:
        if not self.canRetryMedia:
            return False
        self._operation_generation += 1
        self._start_media_generation()
        return True

    @Slot(int, int, str, result=bool)
    def reportMediaSuccess(
        self,
        generation: int,
        candidate_attempt: int,
        url: str,
    ) -> bool:
        candidate = self._matching_candidate(generation, candidate_attempt, url)
        if candidate is None:
            return False
        self._actual_quality = candidate.quality
        self._actual_quality_text = candidate.quality_text
        self._media_state = DetailMediaState.SUCCESS
        self._media_message = f"已加载{candidate.quality_text}。"
        self.mediaChanged.emit()
        return True

    @Slot(int, int, str, result=bool)
    def reportMediaFailure(
        self,
        generation: int,
        candidate_attempt: int,
        url: str,
    ) -> bool:
        candidate = self._matching_candidate(generation, candidate_attempt, url)
        if candidate is None:
            return False
        failed_text = candidate.quality_text
        next_index = self._candidate_index + 1
        if next_index < len(self._candidates):
            self._candidate_index = next_index
            self._candidate_attempt += 1
            next_candidate = self._candidates[next_index]
            self._media_state = DetailMediaState.FALLBACK
            self._media_message = (
                f"{failed_text}加载失败，正在尝试{next_candidate.quality_text}。"
            )
        else:
            self._media_state = DetailMediaState.ERROR
            if self.restrictedFormat:
                self._media_message = (
                    f"{self.formatStatusText}仅支持预览图，但预览图不可用。"
                )
            else:
                self._media_message = "所有图片候选均加载失败。"
        self.mediaChanged.emit()
        return True

    @Slot(int, int, str, result=bool)
    def reportMediaStopped(
        self,
        generation: int,
        candidate_attempt: int,
        url: str,
    ) -> bool:
        """Keep a cleared active image stopped until the user retries it."""

        candidate = self._matching_candidate(generation, candidate_attempt, url)
        if candidate is None:
            return False
        self._media_state = DetailMediaState.ERROR
        self._media_message = "图片缓存已清理，请手动重试这张图片。"
        self.mediaChanged.emit()
        return True

    @Slot(str, result=bool)
    def activateTag(self, tag_name: str) -> bool:
        if (
            self._shutdown
            or not self._is_open
            or not self._tags_model.contains(tag_name)
        ):
            return False
        self._operation_generation += 1
        operation_generation = self._operation_generation
        post = self._current_post
        self._is_open = False
        self._invalidate_media()
        if not self._is_current_tag_activation(operation_generation, post):
            return False
        self.openChanged.emit()
        if not self._is_current_tag_activation(operation_generation, post):
            return False
        self.tagSearchRequested.emit(tag_name)
        return self._is_current_tag_activation(operation_generation, post)

    @Slot()
    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._operation_generation += 1
        self._shutdown = True
        self._is_open = False
        self._invalidate_media()
        self.openChanged.emit()

    @property
    def _current_candidate(self) -> MediaCandidate | None:
        if not 0 <= self._candidate_index < len(self._candidates):
            return None
        return self._candidates[self._candidate_index]

    def _start_media_generation(self, *, emit_signal: bool = True) -> None:
        post = self._current_post
        self._media_generation += 1
        self._candidate_attempt = 0
        self._candidate_index = -1
        self._actual_quality = None
        self._actual_quality_text = "暂无数据"
        self._candidates = (
            build_media_candidates(post, self._requested_quality)
            if post is not None
            else ()
        )
        if self._candidates:
            self._candidate_index = 0
            self._candidate_attempt = 1
            self._media_state = DetailMediaState.LOADING
            if self.restrictedFormat:
                self._media_message = f"{self.formatStatusText}，当前只显示预览图。"
            else:
                self._media_message = f"正在加载{self._candidates[0].quality_text}。"
        else:
            self._media_state = DetailMediaState.ERROR
            if self.restrictedFormat:
                self._media_message = f"{self.formatStatusText}没有可用预览图。"
            else:
                self._media_message = "此作品没有可用图片地址。"
        if emit_signal:
            self.mediaChanged.emit()

    def _invalidate_media(self) -> None:
        self._media_generation += 1
        self._candidate_attempt = 0
        self._candidate_index = -1
        self._candidates = ()
        self._actual_quality = None
        self._actual_quality_text = "暂无数据"
        self._media_state = DetailMediaState.IDLE
        self._media_message = ""
        self.mediaChanged.emit()

    def _matching_candidate(
        self,
        generation: int,
        candidate_attempt: int,
        url: str,
    ) -> MediaCandidate | None:
        if (
            self._shutdown
            or not self._is_open
            or self._media_state
            not in {DetailMediaState.LOADING, DetailMediaState.FALLBACK}
        ):
            return None
        candidate = self._current_candidate
        if (
            candidate is None
            or generation != self._media_generation
            or candidate_attempt != self._candidate_attempt
            or url != candidate.url
        ):
            return None
        return candidate

    def _is_current_open(
        self,
        operation_generation: int,
        post: DanbooruPost,
    ) -> bool:
        return (
            not self._shutdown
            and self._operation_generation == operation_generation
            and self._is_open
            and self._current_post is post
        )

    def _is_current_tag_activation(
        self,
        operation_generation: int,
        post: DanbooruPost | None,
    ) -> bool:
        return (
            not self._shutdown
            and self._operation_generation == operation_generation
            and not self._is_open
            and self._current_post is post
        )

    def _is_current_close(
        self,
        operation_generation: int,
        post: DanbooruPost | None,
    ) -> bool:
        return (
            not self._shutdown
            and self._operation_generation == operation_generation
            and not self._is_open
            and self._current_post is post
        )
