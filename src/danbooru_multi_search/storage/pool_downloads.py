"""Original page manifests and atomically associated ordinary download batches."""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Iterator
from uuid import uuid4

from danbooru_multi_search.core.downloads import DownloadBatchSnapshot, DownloadConflictPolicy, DownloadError
from danbooru_multi_search.core.pool_downloads import (
    POOL_DOWNLOAD_GROUP_SIZE, PoolDownloadPage, PoolDownloadPageState,
    PoolDownloadPlan, PoolDownloadState, PoolDownloadSummary, pool_download_request,
)
from danbooru_multi_search.core.pool_pages import PoolPageBatch, PoolPageState
from danbooru_multi_search.core.pool_progress import normalize_api_source
from danbooru_multi_search.core.pools import DanbooruPool, PoolCategory, PoolPageReference
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.downloads import DownloadRepository


class PoolDownloadRepository:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._downloads = DownloadRepository(database)

    def create(self, pool: DanbooruPool, source: str, directory: Path,
               policy: DownloadConflictPolicy) -> PoolDownloadPlan:
        """Persist every original position before any metadata or file request."""
        if (not isinstance(pool, DanbooruPool) or pool.category is not PoolCategory.SERIES
                or pool.is_deleted or not pool.post_count):
            raise DownloadError("invalid_pool", "请选择包含页面的有效系列作品池。")
        plan = PoolDownloadPlan(uuid4().hex, normalize_api_source(source), pool.id, pool.name,
                                pool.post_count, directory, policy, PoolDownloadState.PREPARING)
        with self._connection(write=True) as connection:
            connection.execute("""INSERT INTO pool_download_plans
                (id,source,pool_id,name,total,directory,policy,state,generation,message,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
                    plan.plan_id, plan.source, plan.pool_id, plan.name, plan.total,
                    str(directory), policy.value, plan.state.value, 0, "",
                    datetime.now(timezone.utc).isoformat(),
                ))
            connection.executemany("""INSERT INTO pool_download_pages
                (plan_id,page_number,post_id,state) VALUES (?,?,?,'waiting')""",
                ((plan.plan_id, number, identifier) for number, identifier in enumerate(pool.post_ids, 1)))
        return plan

    def get(self, key: str) -> PoolDownloadPlan:
        with self._connection() as connection:
            return self._get(connection, key)

    def summaries(self, *, limit: int = 20, offset: int = 0) -> tuple[PoolDownloadSummary, ...]:
        _pagination(limit, offset)
        with self._connection() as connection:
            rows = connection.execute("""SELECT p.*,
                COUNT(d.page_number) AS actual_total,
                SUM(d.state != 'waiting') AS prepared_count,
                SUM(d.state = 'enqueued') AS enqueued_count,
                SUM(d.state NOT IN ('waiting','ready','enqueued')) AS unavailable_count
                FROM pool_download_plans p JOIN pool_download_pages d ON d.plan_id=p.id
                GROUP BY p.id ORDER BY p.created_at DESC,p.id LIMIT ? OFFSET ?""", (limit, offset))
            values = []
            for row in rows:
                plan = _plan(row)
                if row['actual_total'] != plan.total:
                    raise DownloadError("invalid_record", "整池下载页位数量与计划不一致。")
                values.append(PoolDownloadSummary(plan, row['prepared_count'], row['enqueued_count'], row['unavailable_count']))
            return tuple(values)

    def pages(self, key: str, *, limit: int = 40, offset: int = 0) -> tuple[PoolDownloadPage, ...]:
        _pagination(limit, offset)
        with self._connection() as connection:
            plan = self._get(connection, key)
            return tuple(_page(row, plan) for row in connection.execute(
                "SELECT * FROM pool_download_pages WHERE plan_id=? ORDER BY page_number LIMIT ? OFFSET ?",
                (key, limit, offset)))

    def batches(self, key: str, *, limit: int = 40, offset: int = 0,
                expected: PoolDownloadPlan | None = None) -> tuple[str, ...]:
        _pagination(limit, offset)
        with self._connection() as connection:
            self._get(connection, key)
            if expected is not None:
                self._current(connection, expected)
            return tuple(row[0] for row in connection.execute("""SELECT batch_id FROM pool_download_pages
                WHERE plan_id=? AND batch_id IS NOT NULL GROUP BY batch_id ORDER BY MIN(page_number),batch_id
                LIMIT ? OFFSET ?""", (key, limit, offset)))

    def next_work(self, expected: PoolDownloadPlan) -> tuple[PoolDownloadPage, ...]:
        """Return only the first unfinished 1000-position group, never the whole pool."""
        with self._connection() as connection:
            self._current(connection, expected)
            row = connection.execute("""SELECT MIN(page_number) FROM pool_download_pages
                WHERE plan_id=? AND state IN ('waiting','ready')""", (expected.plan_id,)).fetchone()
            if row[0] is None:
                return ()
            first = (row[0] - 1) // POOL_DOWNLOAD_GROUP_SIZE * POOL_DOWNLOAD_GROUP_SIZE + 1
            return tuple(_page(row, expected) for row in connection.execute("""SELECT * FROM pool_download_pages
                WHERE plan_id=? AND page_number BETWEEN ? AND ? AND state IN ('waiting','ready')
                ORDER BY page_number""", (expected.plan_id, first, first + POOL_DOWNLOAD_GROUP_SIZE - 1)))

    def record_metadata(self, expected: PoolDownloadPlan, batch: PoolPageBatch) -> None:
        if not isinstance(batch, PoolPageBatch):
            raise DownloadError("invalid_request", "整池页面响应无效。")
        with self._connection(write=True) as connection:
            self._current(connection, expected)
            for slot in batch.slots:
                row = connection.execute("SELECT * FROM pool_download_pages WHERE plan_id=? AND page_number=?",
                                         (expected.plan_id, slot.reference.page_number)).fetchone()
                if row is None or _page(row, expected).reference != slot.reference or row['state'] != 'waiting':
                    raise DownloadError("stale_download", "整池页面请求位置已改变。")
                metadata = slot.metadata
                url = extension = None
                message = metadata.message
                if metadata.state is PoolPageState.AVAILABLE:
                    try:
                        post = metadata.post
                        if not post.download_url:
                            raise DownloadError("missing_original", "没有原文件地址，不能使用预览图代替。")
                        request = pool_download_request(slot.reference, post.download_url, post.file_ext)
                        state, url, extension = 'ready', request.source_url, request.file_ext
                    except DownloadError as exc:
                        state, message = 'unavailable', str(exc)
                elif metadata.state.value in {'deleted', 'unavailable', 'not_returned', 'failed'}:
                    state = metadata.state.value
                else:
                    raise DownloadError("invalid_request", "页面响应尚未完成。")
                connection.execute("""UPDATE pool_download_pages SET state=?,source_url=?,file_ext=?,message=?
                    WHERE plan_id=? AND page_number=?""", (
                        state, url, extension, message, expected.plan_id, slot.reference.page_number,
                    ))

    def commit_group(self, expected: PoolDownloadPlan, first_page: int) -> DownloadBatchSnapshot | None:
        """Batch insertion and every original-position association share this transaction."""
        if type(first_page) is not int or first_page < 1 or (first_page - 1) % POOL_DOWNLOAD_GROUP_SIZE:
            raise DownloadError("invalid_request", "整池下载分组页码无效。")
        with self._connection(write=True) as connection:
            self._current(connection, expected)
            rows = tuple(connection.execute("""SELECT * FROM pool_download_pages
                WHERE plan_id=? AND page_number BETWEEN ? AND ? ORDER BY page_number""",
                (expected.plan_id, first_page, first_page + POOL_DOWNLOAD_GROUP_SIZE - 1)))
            pages = tuple(_page(row, expected) for row in rows)
            if any(page.state is PoolDownloadPageState.WAITING for page in pages):
                raise DownloadError("invalid_request", "本组仍有尚未读取的页位。")
            ready = tuple(page for page in pages if page.state is PoolDownloadPageState.READY)
            if not ready:
                return None
            batch = self._downloads.create_batch_in_transaction(
                connection, tuple(page.request for page in ready), expected.directory, expected.policy,
            )
            for page, item in zip(ready, batch.items, strict=True):
                connection.execute("""UPDATE pool_download_pages SET state='enqueued',item_id=?,batch_id=?
                    WHERE plan_id=? AND page_number=?""", (
                        item.item_id, batch.batch_id, expected.plan_id, page.reference.page_number,
                    ))
            return batch

    def stop(self, expected: PoolDownloadPlan, message: str = "已停止准备；已加入的文件下载继续。") -> PoolDownloadPlan:
        return self._transition(expected, PoolDownloadState.PAUSED, message)

    def complete(self, expected: PoolDownloadPlan) -> PoolDownloadPlan:
        with self._connection(write=True) as connection:
            self._current(connection, expected)
            if connection.execute("SELECT 1 FROM pool_download_pages WHERE plan_id=? AND state IN ('waiting','ready')",
                                  (expected.plan_id,)).fetchone():
                raise DownloadError("invalid_request", "整池仍有尚未准备的页位。")
            return self._set_state(connection, expected, PoolDownloadState.READY,
                                   "准备完成；文件下载状态请查看关联批次。")

    def resume(self, expected: PoolDownloadPlan, source: str, *, retry_missing: bool = False) -> PoolDownloadPlan:
        with self._connection(write=True) as connection:
            self._current(connection, expected, preparing=False)
            if expected.source != normalize_api_source(source):
                raise DownloadError("source_changed", "API 来源与原计划不同，不能继续此整池任务。")
            if retry_missing:
                connection.execute("""UPDATE pool_download_pages SET state='waiting',message=''
                    WHERE plan_id=? AND state NOT IN ('ready','enqueued','waiting')""", (expected.plan_id,))
            return self._set_state(connection, expected, PoolDownloadState.PREPARING, "")

    def recover_unfinished(self) -> None:
        """Pause interrupted preparation; never read files or start network requests."""
        with self._connection(write=True) as connection:
            connection.execute("""UPDATE pool_download_plans SET state='paused',generation=generation+1,
                message='上次准备已中断，等待手动继续；已加入的下载可在原批次恢复。'
                WHERE state='preparing'""")

    def _transition(self, expected: PoolDownloadPlan, state: PoolDownloadState, message: str) -> PoolDownloadPlan:
        with self._connection(write=True) as connection:
            self._current(connection, expected)
            return self._set_state(connection, expected, state, message)

    def _set_state(self, connection: sqlite3.Connection, expected: PoolDownloadPlan,
                   state: PoolDownloadState, message: str) -> PoolDownloadPlan:
        if expected.generation >= 2**63 - 2:
            raise DownloadError("invalid_record", "整池执行代次已达到上限。")
        connection.execute("UPDATE pool_download_plans SET state=?,generation=generation+1,message=? WHERE id=?",
                           (state.value, message, expected.plan_id))
        return self._get(connection, expected.plan_id)

    def _get(self, connection: sqlite3.Connection, key: str) -> PoolDownloadPlan:
        row = connection.execute("SELECT * FROM pool_download_plans WHERE id=?", (key,)).fetchone()
        if row is None:
            raise DownloadError("unknown_plan", "找不到整池下载记录。")
        return _plan(row)

    def _current(self, connection: sqlite3.Connection, expected: PoolDownloadPlan, *, preparing: bool = True) -> None:
        if (not isinstance(expected, PoolDownloadPlan) or self._get(connection, expected.plan_id) != expected
                or preparing and expected.state is not PoolDownloadState.PREPARING):
            raise DownloadError("stale_download", "整池准备状态已改变，请重新选择操作。")

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = self._database.connect(busy_timeout_ms=0)
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield connection
            if write:
                connection.commit()
        except DownloadError:
            raise
        except (sqlite3.Error, ValueError, TypeError, OSError, OverflowError) as exc:
            raise DownloadError("storage_error", f"整池下载记录读取或保存失败：{exc}") from exc
        finally:
            if connection is not None:
                connection.close()


def _pagination(limit: int, offset: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
        raise DownloadError("invalid_pagination", "整池下载记录分页范围无效。")


def _plan(row: sqlite3.Row) -> PoolDownloadPlan:
    try:
        return PoolDownloadPlan(row['id'], row['source'], row['pool_id'], row['name'], row['total'],
                                Path(row['directory']), DownloadConflictPolicy(row['policy']),
                                PoolDownloadState(row['state']), row['generation'], row['message'])
    except (ValueError, TypeError, AttributeError) as exc:
        raise DownloadError("invalid_record", f"整池下载计划无效：{exc}") from exc


def _page(row: sqlite3.Row, plan: PoolDownloadPlan) -> PoolDownloadPage:
    try:
        reference = PoolPageReference(plan.pool_id, row['page_number'], row['post_id'])
        if reference.page_number > plan.total:
            raise ValueError('page outside manifest')
        request = pool_download_request(reference, row['source_url'], row['file_ext']) if row['source_url'] is not None else None
        return PoolDownloadPage(reference, PoolDownloadPageState(row['state']), request,
                                row['item_id'], row['batch_id'], row['message'])
    except (ValueError, TypeError, AttributeError) as exc:
        raise DownloadError("invalid_record", f"整池下载页位无效：{exc}") from exc
