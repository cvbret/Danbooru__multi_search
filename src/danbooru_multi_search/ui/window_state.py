"""Pure logical-pixel window geometry selection for multi-display restore."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


MINIMUM_WINDOW_WIDTH = 820
MINIMUM_WINDOW_HEIGHT = 600
MINIMUM_VISIBLE_WIDTH = 96
MINIMUM_VISIBLE_HEIGHT = 48


@dataclass(frozen=True, slots=True)
class WindowGeometry:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class ScreenGeometry:
    available: WindowGeometry
    primary: bool = False


def _intersection(first: WindowGeometry, second: WindowGeometry) -> tuple[int, int]:
    width = max(
        0,
        min(first.x + first.width, second.x + second.width)
        - max(first.x, second.x),
    )
    height = max(
        0,
        min(first.y + first.height, second.y + second.height)
        - max(first.y, second.y),
    )
    return width, height


def restore_window_geometry(
    saved: WindowGeometry,
    screens: Sequence[ScreenGeometry],
) -> WindowGeometry:
    """Return a visible restore geometry while preserving valid negative positions."""

    if not screens:
        width = max(MINIMUM_WINDOW_WIDTH, saved.width)
        height = max(MINIMUM_WINDOW_HEIGHT, saved.height)
        return WindowGeometry(saved.x, saved.y, width, height)

    primary_index = next(
        (index for index, screen in enumerate(screens) if screen.primary),
        0,
    )
    candidates: list[tuple[int, bool, int]] = []
    for index, screen in enumerate(screens):
        visible_width, visible_height = _intersection(saved, screen.available)
        if (
            visible_width >= MINIMUM_VISIBLE_WIDTH
            and visible_height >= MINIMUM_VISIBLE_HEIGHT
        ):
            candidates.append(
                (
                    visible_width * visible_height,
                    index == primary_index,
                    index,
                )
            )

    if candidates:
        _, _, target_index = max(
            candidates,
            key=lambda candidate: (candidate[0], candidate[1], -candidate[2]),
        )
        target = screens[target_index].available
        keep_position = True
    else:
        target = screens[primary_index].available
        keep_position = False

    width = min(
        max(saved.width, min(MINIMUM_WINDOW_WIDTH, target.width)),
        target.width,
    )
    height = min(
        max(saved.height, min(MINIMUM_WINDOW_HEIGHT, target.height)),
        target.height,
    )
    if keep_position:
        required_width = min(MINIMUM_VISIBLE_WIDTH, width, target.width)
        required_height = min(MINIMUM_VISIBLE_HEIGHT, height, target.height)
        x = min(
            max(saved.x, target.x - width + required_width),
            target.x + target.width - required_width,
        )
        y = min(
            max(saved.y, target.y - height + required_height),
            target.y + target.height - required_height,
        )
        return WindowGeometry(x, y, width, height)
    return WindowGeometry(
        target.x + (target.width - width) // 2,
        target.y + (target.height - height) // 2,
        width,
        height,
    )
