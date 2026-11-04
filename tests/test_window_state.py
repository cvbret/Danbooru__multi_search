from __future__ import annotations

from danbooru_multi_search.ui.window_state import (
    ScreenGeometry,
    WindowGeometry,
    restore_window_geometry,
)


def test_visible_negative_position_is_preserved_on_secondary_screen():
    screens = (
        ScreenGeometry(WindowGeometry(0, 0, 1920, 1040), primary=True),
        ScreenGeometry(WindowGeometry(-1600, 0, 1600, 900)),
    )

    restored = restore_window_geometry(
        WindowGeometry(-1500, 80, 1000, 700),
        screens,
    )

    assert restored == WindowGeometry(-1500, 80, 1000, 700)


def test_largest_intersection_wins_and_primary_breaks_equal_area_tie():
    screens = (
        ScreenGeometry(WindowGeometry(0, 0, 1000, 800), primary=True),
        ScreenGeometry(WindowGeometry(1000, 0, 800, 800)),
    )
    saved = WindowGeometry(900, 40, 200, 700)

    assert restore_window_geometry(saved, screens) == WindowGeometry(
        900,
        40,
        820,
        700,
    )


def test_larger_secondary_intersection_controls_restored_size():
    screens = (
        ScreenGeometry(WindowGeometry(0, 0, 1200, 800), primary=True),
        ScreenGeometry(WindowGeometry(1200, 0, 800, 600)),
    )

    restored = restore_window_geometry(
        WindowGeometry(1000, 0, 1000, 700),
        screens,
    )

    assert restored == WindowGeometry(1000, 0, 800, 600)


def test_removed_display_recenters_and_clamps_to_primary():
    screens = (ScreenGeometry(WindowGeometry(0, 0, 1280, 720), primary=True),)

    restored = restore_window_geometry(
        WindowGeometry(-2500, 100, 2000, 1200),
        screens,
    )

    assert restored == WindowGeometry(0, 0, 1280, 720)


def test_too_small_window_uses_minimum_size_when_screen_allows_it():
    screens = (ScreenGeometry(WindowGeometry(0, 0, 1920, 1080), primary=True),)

    restored = restore_window_geometry(
        WindowGeometry(100, 100, 300, 200),
        screens,
    )

    assert restored == WindowGeometry(100, 100, 820, 600)


def test_screen_smaller_than_minimum_is_used_in_full():
    screens = (ScreenGeometry(WindowGeometry(10, 20, 640, 480), primary=True),)

    restored = restore_window_geometry(
        WindowGeometry(2000, 2000, 300, 200),
        screens,
    )

    assert restored == WindowGeometry(10, 20, 640, 480)


def test_oversized_visible_window_remains_visible_after_resizing():
    screens = (ScreenGeometry(WindowGeometry(0, 0, 1920, 1080), primary=True),)

    restored = restore_window_geometry(
        WindowGeometry(-5000, 0, 7000, 800),
        screens,
    )

    assert restored == WindowGeometry(-1824, 0, 1920, 800)
