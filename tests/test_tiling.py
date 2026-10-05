"""Unit tests for the window-tiling grid math (no browser needed)."""

from __future__ import annotations

from engine import tile_bounds


def test_single_window_fills_the_screen():
    assert tile_bounds(0, 1, 1920, 1080) == {
        "left": 0, "top": 0, "width": 1920, "height": 1080
    }


def test_two_windows_split_left_and_right():
    # 2 -> 2 cols x 1 row.
    assert tile_bounds(0, 2, 1920, 1080) == {"left": 0, "top": 0, "width": 960, "height": 1080}
    assert tile_bounds(1, 2, 1920, 1080) == {"left": 960, "top": 0, "width": 960, "height": 1080}


def test_four_windows_form_a_2x2_grid():
    bounds = [tile_bounds(i, 4, 1920, 1080) for i in range(4)]
    assert bounds == [
        {"left": 0, "top": 0, "width": 960, "height": 540},
        {"left": 960, "top": 0, "width": 960, "height": 540},
        {"left": 0, "top": 540, "width": 960, "height": 540},
        {"left": 960, "top": 540, "width": 960, "height": 540},
    ]


def test_three_windows_use_a_2x2_grid_leaving_one_cell_empty():
    # ceil(sqrt(3)) = 2 cols, ceil(3/2) = 2 rows.
    assert tile_bounds(0, 3, 1920, 1080) == {"left": 0, "top": 0, "width": 960, "height": 540}
    assert tile_bounds(2, 3, 1920, 1080) == {"left": 0, "top": 540, "width": 960, "height": 540}


def test_index_wraps_within_count():
    # worker index >= count just reuses a cell rather than going off-screen.
    assert tile_bounds(4, 4, 1920, 1080) == tile_bounds(0, 4, 1920, 1080)


def test_count_zero_is_treated_as_one():
    assert tile_bounds(0, 0, 1920, 1080) == {
        "left": 0, "top": 0, "width": 1920, "height": 1080
    }
