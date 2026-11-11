"""Qt event-loop implementations of the task scheduling interfaces."""

from __future__ import annotations

import math
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Slot


class QtScheduledCall(QObject):
    def __init__(
        self,
        delay_seconds: float,
        callback: Callable[[], None],
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        self._callback: Callable[[], None] | None = callback
        self._active = True
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._run)
        milliseconds = max(0, math.ceil(delay_seconds * 1000))
        self._timer.start(milliseconds)

    @property
    def active(self) -> bool:
        return self._active and self._timer.isActive()

    def cancel(self) -> None:
        if not self._active:
            return
        self._active = False
        self._callback = None
        self._timer.stop()
        self.deleteLater()

    @Slot()
    def _run(self) -> None:
        if not self._active:
            return
        self._active = False
        callback = self._callback
        self._callback = None
        if callback is not None:
            callback()
        self.deleteLater()


class QtScheduler(QObject):
    def call_later(
        self,
        delay_seconds: float,
        callback: Callable[[], None],
    ) -> QtScheduledCall:
        return QtScheduledCall(delay_seconds, callback, self)
