"""Stable cache cleanup facts and maintenance writes without interface state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from danbooru_multi_search.app.query_checkpoints import QueryCheckpointService
from danbooru_multi_search.app.write_access import SharedWriteAccess
from danbooru_multi_search.danbooru.query_checkpoint import CheckpointError
from danbooru_multi_search.storage.history import HistoryCleanupIdentity, HistoryError, HistoryRepository
from danbooru_multi_search.storage.query_checkpoints import CheckpointCleanupIdentity, QueryCheckpointRepository


CleanupIdentity = HistoryCleanupIdentity | CheckpointCleanupIdentity


@dataclass(frozen=True, slots=True)
class CleanupSnapshot:
    kind: str
    identities: tuple[CleanupIdentity, ...]
    count: int


def delete_history(
    repository: HistoryRepository,
    expected: tuple[HistoryCleanupIdentity, ...] | None = None,
) -> None:
    """Perform the actual deletion inside the session's caller-owned UI boundary."""
    if expected is None:
        repository.clear_history()
    else:
        repository.clear_history(expected=expected)


class CacheCleanupService:
    def __init__(
        self,
        history: HistoryRepository,
        checkpoints: QueryCheckpointRepository,
        progress: QueryCheckpointService,
        access: SharedWriteAccess,
    ) -> None:
        self._history = history
        self._checkpoints = checkpoints
        self._progress = progress
        self._access = access

    def capture(self, kind: str, *, image_count: int = 0) -> CleanupSnapshot:
        """Read stable ownership; image counts come from the Qt cache adapter."""
        if kind == "history":
            identities = self._history.capture_cleanup()
        elif kind == "checkpoints":
            identities = self._checkpoints.capture_cleanup()
        elif kind == "images":
            if type(image_count) is not int or image_count < 0:
                raise ValueError("图片缓存数量无效。")
            return CleanupSnapshot(kind, (), image_count)
        else:
            raise ValueError("请选择有效的清理范围。")
        return CleanupSnapshot(kind, identities, len(identities))

    @staticmethod
    def _owners(snapshot: CleanupSnapshot) -> frozenset[tuple[int, str]]:
        if snapshot.kind == "history":
            return frozenset(
                (item.history_id, item.checkpoint_owner)
                for item in snapshot.identities if item.checkpoint_owner is not None
            )
        return frozenset((item.history_id, item.owner) for item in snapshot.identities)

    def execute(
        self,
        snapshot: CleanupSnapshot,
        *,
        guard: Callable[[], bool],
        clear_history: Callable[[Callable[[], None], Callable[[], None]], None],
        refresh_history: Callable[[], None],
        clear_images: Callable[[], str],
        publish: Callable[[str], None],
        failed: Callable[[Exception], None],
    ) -> bool:
        """Publish the result while maintenance is held; callbacks are synchronous.

        clear_history runs deletion within the session's existing invalidation/
        rollback boundary and invokes retirement before any model notification.
        """
        owner = uuid4().hex
        if not self._access.begin_external_write(owner, maintenance=True):
            return False
        try:
            if not guard():
                return False
            if snapshot.kind in {"history", "checkpoints"}:
                current = self.capture(snapshot.kind)
                if not guard():
                    return False
                if current.identities != snapshot.identities:
                    self._progress.reconcile_ownership()
                    raise ValueError("记录归属已改变，请重新打开清理确认。")
                owners = self._owners(snapshot)
                self._progress.cancel_if_owned(owners)
                if not guard():
                    return False
                if snapshot.kind == "history":
                    clear_history(
                        lambda: delete_history(self._history, snapshot.identities),
                        lambda: self._progress.retire_checkpoints(owners),
                    )
                else:
                    self._checkpoints.clear_checkpoints(expected=snapshot.identities)
                    self._progress.retire_checkpoints(owners)
                    refresh_history()
                notice = f"已清理 {snapshot.count} 条" + (
                    "查询历史及对应检查点。" if snapshot.kind == "history"
                    else "执行检查点；历史条件和当前画廊已保留。"
                )
            elif snapshot.kind == "images":
                notice = clear_images()
            else:
                raise ValueError("请选择有效的清理范围。")
            if guard():
                publish(notice)
            return True
        except (CheckpointError, HistoryError, OSError, RuntimeError, ValueError) as exc:
            failed(exc)
            return False
        finally:
            self._access.finish_external_write(owner)
