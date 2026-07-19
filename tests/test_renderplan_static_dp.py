from __future__ import annotations

from jellyfin_ass2pgs import ass
from jellyfin_ass2pgs.renderplan import build_render_plan


def test_each_static_state_change_becomes_one_direct_render() -> None:
    intervals = [
        ass.TimelineInterval(0, 100, False, ()),
        ass.TimelineInterval(100, 200, False, ()),
        ass.TimelineInterval(300, 400, False, ()),
    ]

    plan = build_render_plan(intervals, frame_ms=40.0, dynamic_render_fps=24.0)

    assert len(plan.static_groups) == 3
    assert [group.samples[0].timestamp_ms for group in plan.static_groups] == [50, 150, 350]
    assert plan.expected_render_calls == 3


def test_only_contiguous_dynamic_intervals_are_merged() -> None:
    intervals = [
        ass.TimelineInterval(0, 100, True, ()),
        ass.TimelineInterval(100, 200, True, ()),
        ass.TimelineInterval(250, 350, True, ()),
        ass.TimelineInterval(350, 450, False, ()),
    ]

    plan = build_render_plan(intervals, frame_ms=40.0, dynamic_render_fps=10.0)

    assert [(group.start_ms, group.end_ms) for group in plan.dynamic_groups] == [(0, 200), (250, 350)]
    assert len(plan.static_groups) == 1
