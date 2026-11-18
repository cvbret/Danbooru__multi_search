"""Present one confirmed detail snapshot and its Eagle import result."""

from PySide6.QtCore import QObject, Property, Signal, Slot

from danbooru_multi_search.app.eagle_import_types import EaglePostImportRequest
from danbooru_multi_search.app.eagle_import_job import EagleImportJob
from danbooru_multi_search.app.eagle_service import EagleOperation
from danbooru_multi_search.storage.query_serialization import encode_query_definition
from danbooru_multi_search.ui.detail_navigation import DetailNavigationController
from danbooru_multi_search.ui.eagle import EagleController
from danbooru_multi_search.ui.eagle_messages import ERROR_TEXT
from danbooru_multi_search.ui.pools import PoolBrowserController
from danbooru_multi_search.ui.query_state import M2QueryBridgeController


_DUPLICATE_REASONS = {
    "local_mapping": "此作品已在当前素材库登记",
    "post_url": "作品页面地址相同",
    "file_hash": "文件内容相同",
    "item_in_trash": "已有素材位于 Eagle 废纸篓",
}
_ERRORS = {
    "eagle_media_download": "原文件下载失败。",
    "eagle_media_staging": "无法准备本地暂存文件。",
    "eagle_missing_original": "此作品缺少原文件地址。",
    "eagle_duplicate_conflict": "重复检查匹配到相互冲突的素材。",
    "eagle_search_incomplete": "素材查询未完成，请检查 Eagle 后重试。",
    "eagle_unresolved_attempt": "先前的导入结果待核实，请在 Eagle 中查看这件作品。",
    "eagle_import_storage_error": "本地导入记录读写失败。",
    "eagle_file_invalid": "原文件检查失败，请重新下载。",
    "eagle_file_changed": "原文件在检查期间发生变化，请重新下载。",
    "eagle_file_hash_failed": "无法读取原文件并计算哈希，请重新下载。",
}


class EagleImportController(QObject):
    changed = Signal()

    def __init__(self, job: EagleImportJob, eagle: EagleController,
                 navigation: DetailNavigationController, query: M2QueryBridgeController,
                 pools: PoolBrowserController, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._job = job
        self._eagle = eagle
        self._navigation = navigation
        self._detail = navigation.detail_controller
        self._query = query
        self._pools = pools
        self._source_revision = 0
        self._last_post = ""
        self._source_error = ""
        self._query_definition = None
        self._detail.openChanged.connect(self._detail_changed)
        job.changed.connect(self.changed)

    def _detail_changed(self) -> None:
        self._source_revision += 1
        self._query_definition = None
        snapshot = self._navigation.gallery_snapshot
        definition = self._query.gallery_definition
        if (self._detail.isOpen and snapshot and snapshot.post_id == self._detail.current_post.id
                and snapshot.revision == snapshot.model.revision and definition):
            self._query_definition = definition
        self.changed.emit()

    @Property(int, notify=changed)
    def sourceRevision(self) -> int:
        return self._source_revision

    @Property(bool, notify=changed)
    def queryAvailable(self) -> bool:
        return self._query_definition is not None

    @Property(bool, notify=changed)
    def busy(self) -> bool:
        return bool(self._job.stage)

    @Property(bool, notify=changed)
    def cancelling(self) -> bool:
        return self._job.cancelling

    @Property(str, notify=changed)
    def lastPostId(self) -> str:
        return self._last_post

    @Property(str, notify=changed)
    def statusText(self) -> str:
        if self._source_error:
            return self._source_error
        if self.cancelling:
            return "正在取消导入…"
        if self.busy:
            return {EagleOperation.PRECHECK_POST: "正在核对素材库、目标文件夹和作品编号…",
                    "download": "正在下载原文件…",
                    EagleOperation.IMPORT_POST: "正在检查文件、核对重复项并处理 Eagle 素材…"}.get(self._job.stage, "")
        result = self._job.result
        if result is None:
            return ""
        outcome = result.import_result
        identifier = (outcome.item_identifier if outcome else None) or result.created_identifier
        suffix = f" · 素材编号 {identifier}" if identifier else ""
        if result.uncertain:
            if result.post_started:
                return "本次写入请求已发送，结果待核实。请在 Eagle 中查看这件作品。" + suffix
            return _ERRORS["eagle_unresolved_attempt"] + suffix
        if result.error_code:
            return _ERRORS.get(result.error_code, ERROR_TEXT.get(result.error_code, "Eagle 导入失败。")) + suffix
        if outcome and identifier:
            if outcome.state == "imported":
                return "已导入 Eagle" + suffix
            if outcome.state == "updated":
                return "已更新已有素材的标签和注释" + suffix
            if outcome.state == "skipped":
                return "已跳过重复作品，" + _DUPLICATE_REASONS.get(outcome.reason, "已有匹配素材") + suffix
        return "未收到有效的导入结果。"

    @Property(str, notify=changed)
    def detailsText(self) -> str:
        if self._source_error:
            return ""
        result = self._job.result
        return "\n".join(filter(None, (result.diagnostic if result else "", self._job.file_notice)))

    @Slot(str, int, int, bool, bool, bool, bool, bool, result=bool)
    def confirm(self, post_key: str, source_revision: int, target_revision: int,
                update_duplicates: bool, include_general: bool, include_meta: bool,
                include_chinese: bool, include_query: bool) -> bool:
        detail = self._detail
        target = self._eagle.selected_target(target_revision)
        post = detail.current_post
        if (self._job.closed or self.busy or self._eagle.busy or target is None
                or not detail.isOpen or post is None or str(post.id) != post_key
                or source_revision != self._source_revision):
            return False
        self._last_post = post_key
        self._source_error = ""
        translations = ()
        if include_chinese:
            translations = tuple((tag.name, tag.chinese_names)
                                 for group in detail.tagsModel.groups for tag in group.entries
                                 if tag.chinese_names)
        pools = self._pools.known_pools_for(post.id)
        try:
            payload = EaglePostImportRequest(
                library_identifier=target[0], folder_identifier=target[1], post=post, post_url=detail.postUrl,
                update_duplicates=update_duplicates, include_general=include_general, include_meta=include_meta,
                include_chinese=include_chinese, translations=translations,
                query_text=encode_query_definition(self._query_definition) if include_query and self._query_definition else "",
                pool_text=("已知：" + "、".join(f"{pool.name} (#{pool.id})" for pool in pools)) if pools else "未取得",
            )
        except ValueError as exc:
            self._source_error = f"无法确认作品的导入信息：{exc}"
            self.changed.emit()
            return False
        return self._job.start(payload)

    @Slot(result=bool)
    def cancel(self) -> bool:
        return self._job.cancel()
