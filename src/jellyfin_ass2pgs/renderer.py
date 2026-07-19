from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from time import perf_counter
from typing import Callable, Iterable

from PIL import Image

from .frame_time import frame_count_for_window, frame_index_to_pts
from .libass_renderer import LibassRenderer, LibassRenderError, RenderedBitmap
from .metrics import GroupMetrics, current_metrics
from .renderplan import DynamicRenderGroup, StaticRenderGroup, StaticSample


@dataclass(frozen=True)
class RenderResult:
    full_png: Path
    cropped_png: Path
    bbox: tuple[int, int, int, int] | None


FrameChangeCallback = Callable[[int, int, int, Image.Image | None], None]
StaticSampleCallback = Callable[[StaticSample, int, int, Image.Image | None], None]


def render_static_group(
    renderer: LibassRenderer,
    group: StaticRenderGroup,
    *,
    on_sample: StaticSampleCallback,
    group_metrics: GroupMetrics | None = None,
) -> int:
    started = perf_counter()
    used = 0
    try:
        for sample in group.samples:
            frame_started = perf_counter()
            frame = renderer.render(sample.timestamp_ms, expect_content=True)
            _record_frame(group_metrics, frame, first=(used == 0), elapsed=perf_counter() - frame_started)
            on_sample(sample, frame.x, frame.y, frame.image)
            used += 1
        return used
    finally:
        if group_metrics:
            group_metrics.group_wall_time_s = perf_counter() - started
        metrics = current_metrics()
        if metrics:
            metrics.add_time("group_wall_time", perf_counter() - started)


def render_dynamic_group(
    renderer: LibassRenderer,
    group: DynamicRenderGroup,
    *,
    render_fps: float,
    on_change: FrameChangeCallback,
    on_progress: Callable[[int, int], None] | None = None,
    group_metrics: GroupMetrics | None = None,
) -> int:
    started = perf_counter()
    frame_count = frame_count_for_window(group.start_ms, group.end_ms, render_fps)
    previous_key: bytes | object = object()
    changes = 0
    try:
        for frame_index in range(frame_count):
            pts_ms = group.start_ms + frame_index_to_pts(frame_index, render_fps)
            frame_started = perf_counter()
            frame = renderer.render(pts_ms)
            _record_frame(
                group_metrics,
                frame,
                first=(frame_index == 0),
                elapsed=perf_counter() - frame_started,
            )

            hash_started = perf_counter()
            key = previous_key if frame.change == 0 and frame_index > 0 else _bitmap_key(frame)
            hash_s = perf_counter() - hash_started
            metrics = current_metrics()
            if metrics:
                metrics.add_time("hashing", hash_s)
            if group_metrics:
                group_metrics.hashing_time_s += hash_s

            if key != previous_key:
                on_change(pts_ms, frame.x, frame.y, frame.image)
                previous_key = key
                changes += 1
            if on_progress and frame_index and frame_index % 500 == 0:
                on_progress(frame_index, frame_count)
        if on_progress:
            on_progress(frame_count, frame_count)
        return changes
    finally:
        wall_s = perf_counter() - started
        if group_metrics:
            group_metrics.group_wall_time_s = wall_s
        metrics = current_metrics()
        if metrics:
            metrics.add_time("group_wall_time", wall_s)


def render_and_crop(
    ass_path: Path,
    full_png: Path,
    cropped_png: Path,
    *,
    size: tuple[int, int],
    timestamp_ms: int,
    font_paths: Iterable[Path] = (),
    libass_path: Path | str | None = None,
    warning_callback: Callable[[str], None] | None = None,
) -> RenderResult:
    with LibassRenderer(
        ass_path,
        size=size,
        font_paths=font_paths,
        libass_path=libass_path,
        warning_callback=warning_callback,
    ) as renderer:
        frame = renderer.render(timestamp_ms)

    full_png.parent.mkdir(parents=True, exist_ok=True)
    cropped_png.parent.mkdir(parents=True, exist_ok=True)
    full = Image.new("RGBA", size, (0, 0, 0, 0))
    bbox = None
    if frame.image is not None:
        full.alpha_composite(frame.image, (frame.x, frame.y))
        frame.image.save(cropped_png)
        bbox = (
            frame.x,
            frame.y,
            frame.x + frame.image.width,
            frame.y + frame.image.height,
        )
    full.save(full_png)
    return RenderResult(full_png=full_png, cropped_png=cropped_png, bbox=bbox)


def _record_frame(
    group_metrics: GroupMetrics | None,
    frame: RenderedBitmap,
    *,
    first: bool,
    elapsed: float,
) -> None:
    metrics = current_metrics()
    if metrics:
        metrics.add_time("python_processing_time", frame.compose_s)
        if first:
            metrics.add_time("time_to_first_frame", elapsed)
    if group_metrics is None:
        return
    group_metrics.frames_rendered += 1
    group_metrics.frames_used += 1
    group_metrics.libass_render_time_s += frame.render_s
    group_metrics.compose_rgba_time_s += frame.compose_s
    group_metrics.python_processing_time_s += frame.compose_s
    if first:
        group_metrics.time_to_first_frame_s = elapsed


def _bitmap_key(frame: RenderedBitmap) -> bytes:
    if frame.image is None:
        return b"empty"
    digest = hashlib.blake2b(digest_size=16)
    digest.update(frame.x.to_bytes(4, "big", signed=True))
    digest.update(frame.y.to_bytes(4, "big", signed=True))
    digest.update(frame.image.width.to_bytes(4, "big"))
    digest.update(frame.image.height.to_bytes(4, "big"))
    digest.update(frame.image.tobytes())
    return digest.digest()


__all__ = [
    "LibassRenderError",
    "RenderResult",
    "render_and_crop",
    "render_dynamic_group",
    "render_static_group",
]
