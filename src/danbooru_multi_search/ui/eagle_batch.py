"""Capture gallery and target identities for the current session's Eagle batch."""

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QObject, Property, Signal, Slot

from danbooru_multi_search.app.eagle_batch import EagleBatchJob
from danbooru_multi_search.app.eagle_import_types import EaglePostImportRequest
from danbooru_multi_search.app.query_candidates import CandidateCriteria, CandidateSelectionMode, CandidateSource
from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.danbooru.result_processing import PostFileCategory
from danbooru_multi_search.danbooru.tag_translation import TagTranslationStore
from danbooru_multi_search.storage.query_serialization import encode_query_definition
from danbooru_multi_search.ui.details import build_post_page_url
from danbooru_multi_search.ui.eagle import EagleController
from danbooru_multi_search.ui.eagle_import import post_import_result_text
from danbooru_multi_search.ui.gallery_selection import GallerySelectionController
from danbooru_multi_search.ui.pools import PoolBrowserController
from danbooru_multi_search.ui.query_state import M2QueryBridgeController


_STATES = {
    "waiting": "等待处理", "precheck_post": "正在核对作品和目标", "download": "正在下载",
    "import_post": "正在检查文件并处理 Eagle 素材", "imported": "已导入", "updated": "已更新",
    "skipped": "重复跳过", "failed": "失败", "uncertain": "结果待核实", "cancelled": "已取消",
    "not_started": "未开始",
}
_QUALITY = {"original": "原图", "large": "较大图片", "preview": "预览图"}
_PHASES = {
    "preparing": "正在准备候选作品", "running": "正在采集", "cancelling": "正在取消",
    "cancelled": "已取消", "stopped": "已停止", "completed": "采集结束",
}


@dataclass(frozen=True, slots=True)
class _Draft:
    source: CandidateSource
    selected: bool
    selection_revision: int


def _optional_int(value: object, label: str) -> int | None:
    if value in ("", None):
        return None
    if type(value) is int:
        return value
    if isinstance(value, str) and len(value) <= 20 and value.removeprefix("-").isascii() and value.removeprefix("-").isdecimal():
        return int(value)
    raise ValueError(f"{label}必须为整数。")


class EagleBatchController(QObject):
    changed = Signal()

    def __init__(self, job: EagleBatchJob, eagle: EagleController, selection: GallerySelectionController,
                 query: M2QueryBridgeController, translations: TagTranslationStore, pools: PoolBrowserController,
                 base_url: str, single_busy: Callable[[], bool], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.job, self._eagle, self._selection = job, eagle, selection
        self._translations, self._pools, self._base_url = translations, pools, base_url
        self._single_busy = single_busy
        self._draft: _Draft | None = None
        self._error = ""
        job.changed.connect(self.changed)
        selection.changed.connect(self.changed)
        query.galleryChanged.connect(self.changed)
        query.core_controller.executor.executionBound.connect(lambda _: self.changed.emit())

    @Property(bool, notify=changed)
    def busy(self) -> bool:
        return self.job.busy

    @Property(str, notify=changed)
    def errorText(self) -> str:
        return self._error

    def _current(self, draft: _Draft) -> bool:
        if draft.selected and draft.selection_revision != self._selection.revision:
            current = self._selection.selected_posts(self._selection.revision, self._selection.modelRevision)
            if current is None or {str(post.id) for post in current} != set(draft.source.selected_keys):
                return False
        return self.job.candidates.source_current(draft.source)

    @Property("QVariantMap", notify=changed)
    def pending(self) -> dict:
        draft = self._draft
        if not draft:
            return {"token": "", "valid": False, "count": 0, "partial": False, "more": False, "range": ""}
        source = draft.source
        return {"token": source.identifier, "valid": self._current(draft),
                "count": len(source.selected_keys) if draft.selected else len(source.posts),
                "partial": source.partial, "more": source.can_load_more, "range": source.range_description}

    @Slot(bool, int, int, result=str)
    def capture(self, selected: bool, model_revision: int, selection_revision: int) -> str:
        if self.busy or self.job.closed or self._single_busy() or self._selection.modelRevision != model_revision:
            return ""
        self._draft = None
        self._error = ""
        try:
            posts = self._selection.selected_posts(selection_revision, model_revision) if selected else ()
            if selected and (posts is None or not 1 <= len(posts) <= 100):
                raise ValueError("请在当前画廊勾选 1 至 100 件作品。")
            source = self.job.candidates.capture(selected_posts=posts, model_revision=model_revision)
            self._draft = _Draft(source, selected, selection_revision)
        except (TypeError, ValueError, RuntimeError) as exc:
            self._error = str(exc)
        draft = self._draft
        self.changed.emit()
        return draft.source.identifier if draft and self._draft is draft else ""

    @Slot(str, int, "QVariantMap", result=bool)
    def confirm(self, token: str, target_revision: int, options: dict) -> bool:
        draft = self._draft
        target = self._eagle.selected_target(target_revision)
        if (self.busy or self._single_busy() or self._eagle.busy or target is None or not draft
                or draft.source.identifier != token or not self._current(draft)):
            return False
        self._error = ""
        try:
            mode = CandidateSelectionMode(options["mode"])
            if (mode == CandidateSelectionMode.SELECTED) != draft.selected:
                raise ValueError("采集方式与打开窗口时的作品来源不一致。")
            criteria = CandidateCriteria(
                mode, options["count"], options["start"],
                frozenset(PostFileCategory(v) for v in options["types"]) or None,
                frozenset(options["ratings"]) or None,
                _optional_int(options["score"], "最低得分"), _optional_int(options["favorites"], "最低收藏数"),
                options["more"],
            )
            quality, fallback = options["quality"], options["fallback"] or None
            if quality not in _QUALITY or (fallback is not None and (quality != "original" or fallback not in {"large", "preview"})):
                raise ValueError("采集清晰度或回退选项无效。")
            flags = {key: options[key] for key in ("update", "general", "meta", "chinese", "query")}
            if any(type(value) is not bool for value in flags.values()):
                raise ValueError("标签和重复处理选项无效。")
        except (KeyError, TypeError, ValueError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        query_text = encode_query_definition(draft.source.checkpoint.definition) if flags["query"] else ""
        translations = self._translations.snapshot.records_by_name

        def payload(post: DanbooruPost) -> EaglePostImportRequest:
            names = (*post.artist_tags, *post.character_tags, *post.copyright_tags,
                     *(post.general_tags if flags["general"] else ()), *(post.meta_tags if flags["meta"] else ()))
            aliases = tuple((name, translations[name].chinese_names) for name in names if name in translations) if flags["chinese"] else ()
            pools = self._pools.known_pools_for(post.id)
            return EaglePostImportRequest(
                target[0], target[1], post, build_post_page_url(self._base_url, post.id),
                update_duplicates=flags["update"], include_general=flags["general"], include_meta=flags["meta"],
                include_chinese=flags["chinese"], translations=aliases, query_text=query_text,
                pool_text=("已知：" + "、".join(f"{pool.name} (#{pool.id})" for pool in pools)) if pools else "未取得",
                requested_quality=quality, fallback_quality=fallback,
            )

        self._draft = None
        return self.job.start(draft.source, criteria, payload)

    @Property("QVariantMap", notify=changed)
    def progress(self) -> dict:
        job = self.job
        counts = [item.state for item in job.items]
        return {"token": job.token, "phase": job.phase, "label": _PHASES.get(job.phase, ""),
                "planned": job.planned, "count": len(job.items), "processed": job.index,
                "success": sum(counts.count(v) for v in ("imported", "updated")), "skipped": counts.count("skipped"),
                "failed": counts.count("failed") + counts.count("uncertain"), "reason": job.reason, "scope": job.scope,
                "started": job.started, "ended": job.ended,
                "current": str(job.items[job.index].post.id) if job.busy and job.index < len(job.items) else ""}

    @Property("QVariantList", notify=changed)
    def rows(self) -> list[dict]:
        rows = []
        for item in self.job.items:
            result = item.result
            outcome = result.import_result if result else None
            quality = _QUALITY.get(outcome.actual_quality, "") if outcome else ""
            rows.append({"key": str(item.post.id), "state": _STATES.get(item.state, item.state),
                         "quality": quality, "format": (outcome.actual_extension or "") if outcome else "",
                         "message": post_import_result_text(result) if result else "", "detail": item.detail})
        return rows

    @Slot(str, result=bool)
    def cancel(self, token: str) -> bool:
        return self.job.cancel(token)
