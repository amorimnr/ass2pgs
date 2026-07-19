from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Callable
import warnings

from . import ass, mkv
from .cache import WorkCleanupReport, extract_fonts_cached, prepare_work_directory
from .config import AppConfig
from .frame_time import frame_count_for_window
from .libass_renderer import LibassRenderer, LibassRenderError
from .metrics import GroupMetrics, Metrics, current_metrics, use_metrics
from .mux import mux_sup
from .pgs import PgsObject, cropped_rgba_to_pgs_object
from .renderer import render_dynamic_group, render_static_group
from .renderplan import RenderPlan, build_render_plan, limit_render_plan
from .sup import SupWriter


ProgressCallback = Callable[[str], None]
CancelCallback = Callable[[], bool]


class ConversionCancelledError(RuntimeError):
    pass


class GroupConversionError(RuntimeError):
    def __init__(self, *, track_id: int, group_index: int, attempts: int, cause: LibassRenderError) -> None:
        self.track_id = track_id
        self.group_index = group_index
        self.attempts = attempts
        self.cause = cause
        super().__init__(
            f"Track {track_id}, render group #{group_index} failed after {attempts} attempt(s): {cause}"
        )


@dataclass(frozen=True)
class _PendingPgsCue:
    start_ms: int
    end_ms: int
    obj: PgsObject


@dataclass(frozen=True)
class TrackConversionResult:
    ass_track_id: int
    sup_path: Path | None
    cues_total: int
    cues_written: int
    skipped: bool = False
    reason: str = ""


@dataclass(frozen=True)
class ConversionResult:
    input_path: Path
    output_path: Path
    tracks: list[TrackConversionResult] = field(default_factory=list)
    metrics: Metrics | None = None

    @property
    def changed(self) -> bool:
        return any(not track.skipped for track in self.tracks)


def convert_mkv(
    mkv_path: Path,
    *,
    config: AppConfig,
    output_path: Path | None = None,
    track_id: int | None = None,
    track_selector: mkv.TrackSelector | None = None,
    force: bool = False,
    profile_only: bool = False,
    no_mux: bool = False,
    max_intervals: int | None = None,
    max_groups: int | None = None,
    from_ms: int | None = None,
    to_ms: int | None = None,
    retry_failed_groups: int | None = None,
    progress: ProgressCallback | None = None,
    cancel_requested: CancelCallback | None = None,
) -> ConversionResult:
    metrics = Metrics()
    with use_metrics(metrics), metrics.time("total"):
        result = _convert_mkv(
            mkv_path,
            config=config,
            output_path=output_path,
            track_id=track_id,
            track_selector=track_selector,
            force=force,
            profile_only=profile_only,
            no_mux=no_mux,
            max_intervals=max_intervals,
            max_groups=max_groups,
            from_ms=from_ms,
            to_ms=to_ms,
            retry_failed_groups=retry_failed_groups,
            progress=progress,
            cancel_requested=cancel_requested,
            metrics=metrics,
        )
    return ConversionResult(result.input_path, result.output_path, result.tracks, metrics)


def _convert_mkv(
    mkv_path: Path,
    *,
    config: AppConfig,
    output_path: Path | None,
    track_id: int | None,
    track_selector: mkv.TrackSelector | None,
    force: bool,
    profile_only: bool,
    no_mux: bool,
    max_intervals: int | None,
    max_groups: int | None,
    from_ms: int | None,
    to_ms: int | None,
    retry_failed_groups: int | None,
    progress: ProgressCallback | None,
    cancel_requested: CancelCallback | None,
    metrics: Metrics,
) -> ConversionResult:
    mkv_path = mkv_path.resolve()
    final_output = output_path.resolve() if output_path else mkv_path
    current_path = final_output if output_path and final_output.exists() and not force else mkv_path
    results: list[TrackConversionResult] = []
    force = force or config.overwrite
    retries = config.retry_failed_groups if retry_failed_groups is None else retry_failed_groups
    if retries < 0:
        raise ValueError("retry_failed_groups must be zero or greater.")

    _raise_if_cancelled(cancel_requested)
    with metrics.time("read_mkv"):
        info = mkv.probe(current_path)
    tracks = mkv.ass_tracks(info)
    if track_id is not None:
        if track_selector is not None and track_selector.active:
            raise ValueError("track_id and track_selector cannot be used together.")
        track_selector = mkv.TrackSelector(indexes=frozenset({track_id}))
    tracks = mkv.select_ass_tracks(tracks, track_selector)
    if not tracks:
        return ConversionResult(mkv_path, final_output, [])

    work_parent = config.work_dir.resolve()
    work_lease = prepare_work_directory(
        work_parent,
        prefix=f"{_safe_stem(mkv_path)}-",
        keep_temp=config.keep_temp,
    )
    work_dir = work_lease.path
    _report_work_cleanup(work_lease.cleanup_report, progress)

    try:
        for original_track in tracks:
            _raise_if_cancelled(cancel_requested)
            with metrics.time("read_mkv"):
                info = mkv.probe(current_path)
            matching_tracks = [track for track in mkv.ass_tracks(info) if track.id == original_track.id]
            if not matching_tracks:
                results.append(TrackConversionResult(original_track.id, None, 0, 0, True, "ASS track no longer exists"))
                continue
            ass_track = matching_tracks[0]

            if not force and mkv.has_matching_pgs(info, ass_track):
                results.append(TrackConversionResult(ass_track.id, None, 0, 0, True, "matching PGS already exists"))
                continue

            _progress(progress, f"extract track {ass_track.id}")
            _raise_if_cancelled(cancel_requested)
            with metrics.time("extract_ass"):
                ass_path = mkv.extract_track(current_path, ass_track.id, work_dir / f"track_{ass_track.id}.ass")
            with metrics.time("extract_fonts"):
                font_paths = extract_fonts_cached(info, current_path, config.font_cache)
            video_size = mkv.video_size(info)
            frame_ms = mkv.video_frame_ms(info)
            video_fps = 1000 / frame_ms
            dynamic_render_fps = min(video_fps, config.dynamic_render_fps)

            with metrics.time("analyze_ass"):
                events = ass.visible_events(ass.load(ass_path))
            if not events:
                results.append(TrackConversionResult(ass_track.id, None, 0, 0, True, "no visible ASS events"))
                continue
            with metrics.time("classify_events"):
                kinds = {event.index: ass.classify_event(event) for event in events}
            with metrics.time("build_timeline"):
                intervals = ass.build_timeline(events, kinds)
            intervals = _filter_intervals(intervals, from_ms=from_ms, to_ms=to_ms, max_intervals=max_intervals)
            with metrics.time("build_render_plan"):
                plan = build_render_plan(
                    intervals,
                    frame_ms=frame_ms,
                    dynamic_render_fps=dynamic_render_fps,
                )
                plan = limit_render_plan(plan, max_groups=max_groups)
            sup_path = work_dir / f"track_{ass_track.id}.sup"
            partial_sup_path = sup_path.with_suffix(sup_path.suffix + ".partial")
            failed_sup_path = sup_path.with_suffix(sup_path.suffix + ".failed")
            written = 0
            changes = 0

            static_count = sum(1 for interval in intervals if not interval.dynamic)
            dynamic_count = len(intervals) - static_count
            metrics.add_track_counters(
                ass_track.id,
                {
                    "events": len(events),
                    "intervals": len(intervals),
                    "static_intervals": static_count,
                    "dynamic_intervals": dynamic_count,
                    "static_groups": len(plan.static_groups),
                    "dynamic_groups": len(plan.dynamic_groups),
                    "expected_render_calls": plan.expected_render_calls,
                    "expected_ffmpeg_processes": plan.expected_ffmpeg_processes,
                },
            )
            _progress(
                progress,
                f"render track {ass_track.id}: {len(plan.static_groups)} static groups, {len(plan.dynamic_groups)} dynamic groups",
            )

            try:
                with LibassRenderer(
                    ass_path,
                    size=video_size,
                    font_paths=font_paths,
                    libass_path=config.libass_path,
                    warning_callback=lambda message: _renderer_warning(progress, message),
                ) as direct_renderer:
                    with SupWriter(partial_sup_path, video_size=video_size, matrix=config.pgs_matrix) as writer:
                        def handle_progress(done: int, total: int) -> None:
                            if done and done % 2500 == 0:
                                _progress(progress, f"rendered {done}/{total} frames for track {ass_track.id}")

                        for group_index, group in enumerate(plan.groups, start=1):
                            _raise_if_cancelled(cancel_requested)

                            def render_group() -> tuple[int, int]:
                                if group.kind.value == "dynamic":
                                    return _write_dynamic_group(
                                        writer,
                                        direct_renderer,
                                        group_index=group_index,
                                        track_id=ass_track.id,
                                        render_fps=dynamic_render_fps,
                                        group=group,
                                        matrix=config.pgs_matrix,
                                        progress=handle_progress,
                                    )
                                return _write_static_group(
                                    writer,
                                    direct_renderer,
                                    group_index=group_index,
                                    track_id=ass_track.id,
                                    group=group,
                                    matrix=config.pgs_matrix,
                                )

                            group_written, group_changes = _run_group_with_retries(
                                render_group,
                                track_id=ass_track.id,
                                group_index=group_index,
                                retry_failed_groups=retries,
                                progress=progress,
                                on_retry=direct_renderer.reinitialize,
                            )
                            written += group_written
                            changes += group_changes

                            if group_index % 50 == 0:
                                _progress(progress, f"processed {group_index}/{len(plan.groups)} groups for track {ass_track.id}")
                partial_sup_path.replace(sup_path)
            except BaseException:
                _mark_sup_failed(partial_sup_path, failed_sup_path)
                raise

            metrics.add_track_counters(
                ass_track.id,
                {
                    "executed_pgs_objects": written,
                    "executed_bitmaps_different": changes,
                },
            )

            if written == 0:
                results.append(TrackConversionResult(ass_track.id, sup_path, changes, 0, True, "no visible cues"))
                continue

            if profile_only or no_mux:
                results.append(TrackConversionResult(ass_track.id, sup_path if config.keep_temp else None, changes, written))
                continue
            _raise_if_cancelled(cancel_requested)
            _progress(progress, f"mux track {ass_track.id}")
            destination = final_output if output_path else None
            with metrics.time("mux_mkv"):
                current_path = mux_sup(
                    current_path,
                    sup_path,
                    ass_track,
                    info=info,
                    output_path=destination,
                    force=force,
                )
            results.append(TrackConversionResult(ass_track.id, sup_path if config.keep_temp else None, changes, written))

        return ConversionResult(mkv_path, current_path if output_path else mkv_path, results)
    finally:
        work_lease.close()


def _filter_intervals(
    intervals: list[ass.TimelineInterval],
    *,
    from_ms: int | None,
    to_ms: int | None,
    max_intervals: int | None,
) -> list[ass.TimelineInterval]:
    filtered = []
    for interval in intervals:
        if from_ms is not None and interval.end_ms <= from_ms:
            continue
        if to_ms is not None and interval.start_ms >= to_ms:
            continue
        start_ms = max(interval.start_ms, from_ms) if from_ms is not None else interval.start_ms
        end_ms = min(interval.end_ms, to_ms) if to_ms is not None else interval.end_ms
        if end_ms <= start_ms:
            continue
        filtered.append(
            ass.TimelineInterval(
                start_ms=start_ms,
                end_ms=end_ms,
                dynamic=interval.dynamic,
                active_events=interval.active_events,
            )
        )
    if max_intervals is not None:
        filtered = filtered[:max(0, max_intervals)]
    return filtered


def _safe_stem(path: Path) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in path.stem)


def _progress(callback: ProgressCallback | None, message: str) -> None:
    if callback:
        callback(message)


def _raise_if_cancelled(callback: CancelCallback | None) -> None:
    if callback and callback():
        raise ConversionCancelledError("Conversion cancelled before publishing the output MKV.")


def _renderer_warning(callback: ProgressCallback | None, message: str) -> None:
    if callback:
        callback(f"warning: {message}")
    else:
        warnings.warn(message, RuntimeWarning, stacklevel=2)


def _report_work_cleanup(report: WorkCleanupReport, callback: ProgressCallback | None) -> None:
    for path in report.removed:
        _progress(callback, f"removed orphaned work directory: {path}")
    if not report.warnings:
        return
    message = f"work directory cleanup left {len(report.warnings)} item(s) untouched; first: {report.warnings[0]}"
    if callback:
        callback(message)
    else:
        warnings.warn(message, RuntimeWarning, stacklevel=2)


def _write_static_group(
    writer: SupWriter,
    renderer: LibassRenderer,
    *,
    group_index: int,
    track_id: int,
    group,
    matrix: str,
) -> tuple[int, int]:
    metrics = current_metrics()
    group_metrics = GroupMetrics(
        index=group_index,
        kind="static",
        start_ms=group.start_ms,
        end_ms=group.end_ms,
        samples=len(group.samples),
        track_id=track_id,
        estimated_frames=group.estimated_frames,
    )
    if metrics:
        metrics.inc("static_renders", len(group.samples))
    pending: list[_PendingPgsCue] = []

    def handle_sample(sample, x: int, y: int, image) -> None:
        if image is None:
            return
        obj = cropped_rgba_to_pgs_object(image, x=x, y=y, matrix=matrix)
        if obj is None:
            return
        pending.append(_PendingPgsCue(sample.start_ms, sample.end_ms, obj))

    render_static_group(
        renderer,
        group,
        on_sample=handle_sample,
        group_metrics=group_metrics,
    )
    if metrics:
        metrics.add_group(group_metrics)
        metrics.inc("bitmaps_different", len(pending))
    written = sum(writer.write_object(cue.start_ms, cue.end_ms, cue.obj) for cue in pending)
    return written, len(pending)


def _write_dynamic_group(
    writer: SupWriter,
    renderer: LibassRenderer,
    *,
    group_index: int,
    track_id: int,
    render_fps: float,
    group,
    matrix: str,
    progress,
) -> tuple[int, int]:
    metrics = current_metrics()
    estimated_frames = frame_count_for_window(group.start_ms, group.end_ms, render_fps)
    group_metrics = GroupMetrics(
        index=group_index,
        kind="dynamic",
        start_ms=group.start_ms,
        end_ms=group.end_ms,
        samples=estimated_frames,
        track_id=track_id,
        estimated_frames=group.estimated_frames,
    )
    if metrics:
        metrics.inc("dynamic_streams")
    changes = 0
    nonempty_changes = 0
    pending: list[_PendingPgsCue] = []
    current_start: int | None = None
    current_object: PgsObject | None = None

    def handle_change(pts_ms: int, x: int, y: int, image) -> None:
        nonlocal current_start, current_object, changes, nonempty_changes
        changes += 1
        if current_object is not None and current_start is not None:
            pending.append(_PendingPgsCue(current_start, pts_ms, current_object))

        if image is None:
            current_start = None
            current_object = None
            return

        obj = cropped_rgba_to_pgs_object(image, x=x, y=y, matrix=matrix)
        if obj is None:
            current_start = None
            current_object = None
            return
        nonempty_changes += 1
        current_start = pts_ms
        current_object = obj

    render_dynamic_group(
        renderer,
        group,
        render_fps=render_fps,
        on_change=handle_change,
        on_progress=progress,
        group_metrics=group_metrics,
    )
    if current_object is not None and current_start is not None:
        pending.append(_PendingPgsCue(current_start, group.end_ms, current_object))
    if metrics:
        metrics.add_group(group_metrics)
        metrics.inc("bitmaps_different", nonempty_changes)
    written = sum(writer.write_object(cue.start_ms, cue.end_ms, cue.obj) for cue in pending)
    return written, changes


def _run_group_with_retries(
    action: Callable[[], tuple[int, int]],
    *,
    track_id: int,
    group_index: int,
    retry_failed_groups: int,
    progress: ProgressCallback | None,
    on_retry: Callable[[], None] | None = None,
) -> tuple[int, int]:
    attempts = retry_failed_groups + 1
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except LibassRenderError as exc:
            metrics = current_metrics()
            if metrics:
                metrics.inc("failed_group_attempts")
            if attempt >= attempts:
                raise GroupConversionError(
                    track_id=track_id,
                    group_index=group_index,
                    attempts=attempt,
                    cause=exc,
                ) from exc
            if metrics:
                metrics.inc("group_retries")
            _progress(
                progress,
                f"retry track {track_id}, group #{group_index} after failed attempt {attempt}: {exc}",
            )
            if on_retry:
                on_retry()
    raise AssertionError("unreachable")


def _mark_sup_failed(partial_path: Path, failed_path: Path) -> None:
    if not partial_path.exists():
        return
    if failed_path.exists():
        failed_path.unlink()
    partial_path.replace(failed_path)


class _null_timer:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False
