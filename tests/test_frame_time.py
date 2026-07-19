from __future__ import annotations

import pytest

from jellyfin_ass2pgs.frame_time import (
    frame_count_for_window,
    frame_duration_ms,
    frame_index_to_pts,
    time_to_frame_index,
)


@pytest.mark.parametrize(
    ("time_ms", "fps", "expected_index", "expected_pts"),
    [
        (1_000, 25.0, 25, 1_000),
        (1_001, 24_000 / 1_001, 24, 1_001),
        (1_001, 30_000 / 1_001, 30, 1_001),
        (0, 60.0, 0, 0),
        (-500, 25.0, 0, 0),
    ],
)
def test_time_frame_mapping_edges(
    time_ms: int,
    fps: float,
    expected_index: int,
    expected_pts: int,
) -> None:
    index = time_to_frame_index(time_ms, fps)

    assert index == expected_index
    assert frame_index_to_pts(index, fps) == expected_pts


@pytest.mark.parametrize(
    ("start_ms", "end_ms", "fps", "expected"),
    [
        (0, 0, 25.0, 1),
        (100, 99, 25.0, 1),
        (0, 10, 25.0, 1),
        (0, 40, 25.0, 1),
        (0, 41, 25.0, 2),
        (1_000, 2_001, 24_000 / 1_001, 24),
    ],
)
def test_frame_count_for_half_open_window(
    start_ms: int,
    end_ms: int,
    fps: float,
    expected: int,
) -> None:
    assert frame_count_for_window(start_ms, end_ms, fps) == expected


@pytest.mark.parametrize("fps", [24_000 / 1_001, 25.0, 30_000 / 1_001, 60.0])
def test_round_trip_stays_within_half_a_frame(fps: float) -> None:
    tolerance_ms = 500 / fps + 1
    for time_ms in [0, 1, 17, 41, 999, 1_001, 12_345, 60_000]:
        reconstructed = frame_index_to_pts(time_to_frame_index(time_ms, fps), fps)
        assert abs(reconstructed - time_ms) <= tolerance_ms


@pytest.mark.parametrize(
    ("fps", "expected_ms"),
    [(25.0, 40), (24_000 / 1_001, 41), (30_000 / 1_001, 33), (60.0, 16)],
)
def test_integer_frame_duration_convention(fps: float, expected_ms: int) -> None:
    assert frame_duration_ms(fps) == expected_ms
