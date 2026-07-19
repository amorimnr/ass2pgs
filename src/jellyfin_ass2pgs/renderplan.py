from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from . import ass
from .frame_time import frame_count_for_window


class RenderGroupKind(Enum):
    STATIC = "static"
    DYNAMIC = "dynamic"


@dataclass(frozen=True)
class StaticSample:
    timestamp_ms: int
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class StaticRenderGroup:
    start_ms: int
    end_ms: int
    samples: tuple[StaticSample, ...]
    estimated_frames: int = 1

    @property
    def kind(self) -> RenderGroupKind:
        return RenderGroupKind.STATIC


@dataclass(frozen=True)
class DynamicRenderGroup:
    start_ms: int
    end_ms: int
    intervals: tuple[ass.TimelineInterval, ...]
    estimated_frames: int

    @property
    def kind(self) -> RenderGroupKind:
        return RenderGroupKind.DYNAMIC


RenderGroup = StaticRenderGroup | DynamicRenderGroup


@dataclass(frozen=True)
class RenderPlan:
    intervals: tuple[ass.TimelineInterval, ...]
    groups: tuple[RenderGroup, ...] = field(default_factory=tuple)

    @property
    def static_groups(self) -> tuple[StaticRenderGroup, ...]:
        return tuple(group for group in self.groups if isinstance(group, StaticRenderGroup))

    @property
    def dynamic_groups(self) -> tuple[DynamicRenderGroup, ...]:
        return tuple(group for group in self.groups if isinstance(group, DynamicRenderGroup))

    @property
    def expected_render_calls(self) -> int:
        return sum(group.estimated_frames for group in self.groups)

    @property
    def expected_ffmpeg_processes(self) -> int:
        return 0


def build_render_plan(
    intervals: list[ass.TimelineInterval],
    *,
    frame_ms: float,
    dynamic_render_fps: float | None = None,
) -> RenderPlan:
    video_fps = 1000 / frame_ms
    if dynamic_render_fps is not None and dynamic_render_fps <= 0:
        raise ValueError("dynamic_render_fps must be greater than zero.")
    dynamic_fps = min(video_fps, dynamic_render_fps) if dynamic_render_fps is not None else video_fps

    groups: list[RenderGroup] = []
    index = 0
    while index < len(intervals):
        interval = intervals[index]
        if not interval.dynamic:
            sample = StaticSample(interval.sample_ms, interval.start_ms, interval.end_ms)
            groups.append(
                StaticRenderGroup(
                    start_ms=interval.start_ms,
                    end_ms=interval.end_ms,
                    samples=(sample,),
                )
            )
            index += 1
            continue

        dynamic_run = [interval]
        index += 1
        while (
            index < len(intervals)
            and intervals[index].dynamic
            and intervals[index].start_ms == dynamic_run[-1].end_ms
        ):
            dynamic_run.append(intervals[index])
            index += 1
        start_ms = dynamic_run[0].start_ms
        end_ms = dynamic_run[-1].end_ms
        groups.append(
            DynamicRenderGroup(
                start_ms=start_ms,
                end_ms=end_ms,
                intervals=tuple(dynamic_run),
                estimated_frames=frame_count_for_window(start_ms, end_ms, dynamic_fps),
            )
        )
    return RenderPlan(intervals=tuple(intervals), groups=tuple(groups))


def limit_render_plan(plan: RenderPlan, *, max_groups: int | None) -> RenderPlan:
    if max_groups is None:
        return plan
    return RenderPlan(intervals=plan.intervals, groups=plan.groups[: max(0, max_groups)])
