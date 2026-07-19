from __future__ import annotations

from jellyfin_ass2pgs import ass
from jellyfin_ass2pgs.config import load_config
from jellyfin_ass2pgs.renderplan import build_render_plan


def test_dynamic_plan_uses_configured_fps_without_affecting_static_calls() -> None:
    intervals = [
        ass.TimelineInterval(0, 1_000, True, ()),
        ass.TimelineInterval(1_000, 2_000, False, ()),
    ]

    plan = build_render_plan(intervals, frame_ms=40.0, dynamic_render_fps=12.0)

    assert plan.dynamic_groups[0].estimated_frames == 12
    assert plan.static_groups[0].estimated_frames == 1
    assert plan.expected_render_calls == 13
    assert plan.expected_ffmpeg_processes == 0


def test_config_loads_dynamic_fps_retry_count_and_libass_path(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'dynamic_render_fps = 15\nretry_failed_groups = 2\nlibass_path = "custom/libass.so"\n',
        encoding="ascii",
    )

    config = load_config(config_path)

    assert config.dynamic_render_fps == 15.0
    assert config.retry_failed_groups == 2
    assert config.libass_path.as_posix() == "custom/libass.so"
