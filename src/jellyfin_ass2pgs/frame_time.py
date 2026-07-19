from __future__ import annotations

import math


def time_to_frame_index(time_ms: int | float, fps: float) -> int:
    """Map a time offset to the nearest rendered frame index.

    Dynamic rendering uses a constant-frame-rate timeline. Existing behavior
    sampled subtitles with round-to-nearest frame, so this function
    keeps that convention in one place for both planning and rendering.
    """
    return max(0, int(round(float(time_ms) * fps / 1000)))


def frame_index_to_pts(frame_index: int, fps: float) -> int:
    """Return the rounded presentation timestamp offset for a frame index."""
    return int(round(max(0, frame_index) * 1000 / fps))


def frame_count_for_window(start_ms: int, end_ms: int, fps: float) -> int:
    """Return how many CFR frames are needed to cover [start_ms, end_ms).

    The window is half-open. A zero or negative duration still renders one frame
    because libass needs one timestamp to produce a sample.
    """
    duration_ms = max(1, end_ms - start_ms)
    return max(1, int(math.ceil(duration_ms * fps / 1000)))


def frame_duration_ms(fps: float) -> int:
    """Return the integer millisecond duration used for one-frame windows."""
    return max(1, int(1000 / fps))
