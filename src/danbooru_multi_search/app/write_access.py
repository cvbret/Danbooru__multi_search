"""One write reservation shared by favorite-related application use cases."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class SharedWriteAccess(QObject):
    """Expose committed reservation facts before notifying synchronous listeners."""

    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._local_write_active = False
        self._external_owner: str | None = None
        self._maintenance_owner: str | None = None
        self._closed = False

    @property
    def local_write_active(self) -> bool:
        return self._local_write_active

    @property
    def external_owner(self) -> str | None:
        return self._external_owner

    @property
    def maintenance_owner(self) -> str | None:
        return self._maintenance_owner

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def busy(self) -> bool:
        return self._closed or self._local_write_active or self._external_owner is not None

    def begin_local_write(self) -> bool:
        if self.busy:
            return False
        self._local_write_active = True
        self.changed.emit()
        if self._closed or not self._local_write_active:
            self.end_local_write()
            return False
        return True

    def end_local_write(self) -> None:
        if self._local_write_active:
            self._local_write_active = False
            if not self._closed:
                self.changed.emit()

    def begin_external_write(self, owner: str, *, maintenance: bool = False) -> bool:
        if not isinstance(owner, str) or not owner or self.busy:
            return False
        self._external_owner = owner
        self._maintenance_owner = owner if maintenance else None
        self.changed.emit()
        if self._closed or self._external_owner != owner:
            # Roll back only this failed acquisition, never a replacement owner.
            self.finish_external_write(owner)
            return False
        return True

    def finish_external_write(self, owner: str) -> None:
        if self._external_owner == owner:
            self._maintenance_owner = None
            self._external_owner = None
            if not self._closed:
                self.changed.emit()

    def close(self) -> None:
        """Prevent new writes without revoking in-flight maintenance permission."""
        self._closed = True
