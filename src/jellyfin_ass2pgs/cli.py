from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import tempfile

from . import ass, mkv
from .config import load_config
from .library import convert_library, resume_library
from .metrics import format_report
from .pipeline import GroupConversionError, convert_mkv
from .renderplan import build_render_plan, limit_render_plan
from .renderer import render_and_crop


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jellyfin-ass2pgs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("tracks", help="List ASS subtitle tracks in an MKV.")
    list_parser.add_argument("mkv", type=Path)

    render_parser = subparsers.add_parser("render-one", help="Render one ASS event to PNG.")
    render_parser.add_argument("mkv", type=Path)
    render_parser.add_argument("--track-id", type=int, default=None)
    render_parser.add_argument("--event", type=int, default=None, help="ASS event index. Defaults to first visible event.")
    render_parser.add_argument("--out", type=Path, default=Path("out"))
    render_parser.add_argument("--config", type=Path, default=None)
    render_parser.add_argument(
        "--at-ms",
        type=int,
        default=None,
        help="Timestamp inside the isolated event. Defaults to 50 ms or the event midpoint for very short events.",
    )

    convert_parser = subparsers.add_parser("convert", help="Convert ASS tracks in one MKV to PGS and mux them.")
    convert_parser.add_argument("mkv", type=Path)
    _add_track_filters(convert_parser)
    convert_parser.add_argument("--output", type=Path, default=None)
    convert_parser.add_argument("--force", action="store_true")
    convert_parser.add_argument("--config", type=Path, default=None)
    convert_parser.add_argument("--keep-temp", action="store_true")
    convert_parser.add_argument(
        "--retry-failed-groups",
        type=_nonnegative_int,
        default=None,
        metavar="N",
        help="Retry a failed render group up to N times. Default: config value or 0.",
    )
    convert_parser.add_argument("--profile-only", action="store_true", help="Generate a partial SUP/report and skip muxing.")
    convert_parser.add_argument("--no-mux", action="store_true", help="Generate SUP but do not mux it into the MKV.")
    convert_parser.add_argument("--max-intervals", type=int, default=None, help="Process at most N timeline intervals.")
    convert_parser.add_argument("--max-groups", type=int, default=None, help="Process at most N render groups.")
    convert_parser.add_argument("--from-ms", type=int, default=None, help="Only process intervals after this timestamp.")
    convert_parser.add_argument("--to-ms", type=int, default=None, help="Only process intervals before this timestamp.")

    plan_parser = subparsers.add_parser("render-plan", help="Print the ASS timeline and grouped render plan.")
    plan_parser.add_argument("mkv", type=Path)
    plan_parser.add_argument("--track-id", type=int, default=None)
    plan_parser.add_argument("--config", type=Path, default=None)
    plan_parser.add_argument("--max-intervals", type=int, default=None)
    plan_parser.add_argument("--max-groups", type=int, default=None)
    plan_parser.add_argument("--from-ms", type=int, default=None)
    plan_parser.add_argument("--to-ms", type=int, default=None)
    plan_parser.add_argument("--show-all-groups", action="store_true")
    plan_parser.add_argument("--show-dynamic-reasons", action="store_true")

    library_parser = subparsers.add_parser("convert-library", help="Convert all MKVs under a directory.")
    library_parser.add_argument("root", type=Path)
    _add_track_filters(library_parser)
    _add_library_output_mode(library_parser)
    library_parser.add_argument("--force", action="store_true")
    library_parser.add_argument("--config", type=Path, default=None)
    library_parser.add_argument("--retry-failed-groups", type=_nonnegative_int, default=None, metavar="N")

    resume_parser = subparsers.add_parser("resume", help="Resume a previous library conversion.")
    resume_parser.add_argument("root", type=Path)
    _add_track_filters(resume_parser)
    _add_library_output_mode(resume_parser)
    resume_parser.add_argument("--force", action="store_true")
    resume_parser.add_argument("--config", type=Path, default=None)
    resume_parser.add_argument("--retry-failed-groups", type=_nonnegative_int, default=None, metavar="N")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "tracks":
            command_tracks(args)
        elif args.command == "render-one":
            command_render_one(args)
        elif args.command == "convert":
            command_convert(args)
        elif args.command == "render-plan":
            command_render_plan(args)
        elif args.command == "convert-library":
            command_convert_library(args)
        elif args.command == "resume":
            command_resume(args)
    except KeyboardInterrupt as exc:
        raise SystemExit("Interrupted safely; no partial MKV was published.") from exc


def command_tracks(args: argparse.Namespace) -> None:
    info = mkv.probe(args.mkv)
    for track in mkv.subtitle_tracks(info):
        languages = "/".join(track.language_tags) or "-"
        label = f"track_index={track.id} codec={track.codec_id} language={languages}"
        if track.name:
            label += f" name={track.name!r}"
        label += f" default={track.default} forced={track.forced}"
        print(label)


def command_convert(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if args.keep_temp:
        config = replace(config, keep_temp=True)
    if args.retry_failed_groups is not None:
        config = replace(config, retry_failed_groups=args.retry_failed_groups)
    try:
        result = convert_mkv(
            args.mkv,
            config=config,
            output_path=args.output,
            track_selector=_selector_from_args(args),
            force=args.force,
            profile_only=args.profile_only,
            no_mux=args.no_mux,
            max_intervals=args.max_intervals,
            max_groups=args.max_groups,
            from_ms=args.from_ms,
            to_ms=args.to_ms,
            progress=lambda message: print(message),
        )
    except (GroupConversionError, mkv.TrackSelectionError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Output: {result.output_path}")
    for track in result.tracks:
        status = "skipped" if track.skipped else "converted"
        detail = f" ({track.reason})" if track.reason else ""
        print(
            f"track_index={track.ass_track_id}: {status}, "
            f"cues={track.cues_written}/{track.cues_total}{detail}"
        )
    if result.metrics:
        print(format_report(result.metrics))


def command_render_plan(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    info = mkv.probe(args.mkv)
    tracks = mkv.ass_tracks(info)
    if not tracks:
        raise SystemExit("No ASS subtitle tracks found.")
    track = _select_track(tracks, args.track_id)

    with tempfile.TemporaryDirectory(prefix="jellyfin-ass2pgs-plan-") as temp_dir:
        ass_path = mkv.extract_track(args.mkv, track.id, Path(temp_dir) / f"track_{track.id}.ass")
        events = ass.visible_events(ass.load(ass_path))
        classifications = {event.index: ass.classify_event_detail(event) for event in events}
        kinds = {event_index: classification.kind for event_index, classification in classifications.items()}
        intervals = ass.build_timeline(events, kinds)
        intervals = _filter_intervals_for_cli(
            intervals,
            from_ms=args.from_ms,
            to_ms=args.to_ms,
            max_intervals=args.max_intervals,
        )
        frame_ms = mkv.video_frame_ms(info)
        dynamic_render_fps = min(1000 / frame_ms, config.dynamic_render_fps)
        plan = build_render_plan(
            intervals,
            frame_ms=frame_ms,
            dynamic_render_fps=dynamic_render_fps,
        )
        limited_plan = limit_render_plan(plan, max_groups=args.max_groups)

    static_intervals = sum(1 for interval in intervals if not interval.dynamic)
    dynamic_intervals = len(intervals) - static_intervals
    print("Timeline:")
    print(f"{len(intervals)} intervals")
    print(f"{static_intervals} STATIC")
    print(f"{dynamic_intervals} DYNAMIC")
    if args.show_dynamic_reasons:
        reason_counts: dict[str, int] = {}
        for classification in classifications.values():
            if classification.kind is ass.EventKind.DYNAMIC:
                reason_counts[classification.reason] = reason_counts.get(classification.reason, 0) + 1
        print("Dynamic reasons:")
        if not reason_counts:
            print("none")
        for reason, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0])):
            print(f"{count} {reason}")
    print()
    print("RenderPlan:")
    print(f"dynamic render fps {dynamic_render_fps:.3f}")
    print(f"{len(plan.static_groups)} static groups")
    print(f"{len(plan.dynamic_groups)} dynamic groups")
    print(f"{plan.expected_render_calls} direct libass calls")
    if args.max_groups is not None:
        print(f"{len(limited_plan.static_groups)} static groups after --max-groups")
        print(f"{len(limited_plan.dynamic_groups)} dynamic groups after --max-groups")
        print(f"{limited_plan.expected_render_calls} direct libass calls after --max-groups")
    print()
    print("Expected renderer work:")
    selected_plan = limited_plan if args.max_groups is not None else plan
    print(f"{selected_plan.expected_render_calls} direct libass calls")
    print("0 ffmpeg processes")
    print()
    print("Groups:")
    groups_to_show = limited_plan.groups if args.max_groups is not None else plan.groups
    if not args.show_all_groups:
        groups_to_show = groups_to_show[:30]
    for index, group in enumerate(groups_to_show, start=1):
        samples = len(group.samples) if hasattr(group, "samples") else group.estimated_frames
        print(
            f"#{index} {group.kind.value} "
            f"window={(group.end_ms - group.start_ms) / 1000:.3f}s "
            f"samples={samples} "
            f"frames={group.estimated_frames} "
            f"frames/sample={(group.estimated_frames / max(1, samples)):.2f}"
        )
    total_groups = len(limited_plan.groups if args.max_groups is not None else plan.groups)
    if not args.show_all_groups and total_groups > len(groups_to_show):
        print(f"... {total_groups - len(groups_to_show)} more groups hidden; use --show-all-groups")


def command_convert_library(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if args.retry_failed_groups is not None:
        config = replace(config, retry_failed_groups=args.retry_failed_groups)
    result = convert_library(
        args.root,
        config=config,
        force=args.force,
        track_selector=_selector_from_args(args),
        in_place=args.in_place,
        output_dir=args.output_dir,
    )
    print(f"MKVs found: {result.files_total}")
    print(f"Converted: {result.files_converted}")
    print(f"Skipped: {result.files_skipped}")
    print(f"Failed: {result.files_failed}")
    for path, error in result.failures:
        print(f"{path}: {error}")
    if result.files_failed:
        raise SystemExit(1)


def command_resume(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if args.retry_failed_groups is not None:
        config = replace(config, retry_failed_groups=args.retry_failed_groups)
    result = resume_library(
        args.root,
        config=config,
        force=args.force,
        track_selector=_selector_from_args(args),
        in_place=args.in_place,
        output_dir=args.output_dir,
    )
    print(f"MKVs found: {result.files_total}")
    print(f"Converted: {result.files_converted}")
    print(f"Skipped: {result.files_skipped}")
    print(f"Failed: {result.files_failed}")
    for path, error in result.failures:
        print(f"{path}: {error}")
    if result.files_failed:
        raise SystemExit(1)


def command_render_one(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    output_dir = args.out
    extracted_dir = output_dir / "extracted"
    fonts_dir = output_dir / "fonts"
    events_dir = output_dir / "events"
    png_dir = output_dir / "png"

    info = mkv.probe(args.mkv)
    tracks = mkv.ass_tracks(info)
    if not tracks:
        raise SystemExit("No ASS subtitle tracks found.")

    track = _select_track(tracks, args.track_id)
    size = mkv.video_size(info)
    extracted_ass = mkv.extract_track(args.mkv, track.id, extracted_dir / f"track_{track.id}.ass")
    fonts = mkv.extract_font_attachments(info, args.mkv, fonts_dir)

    subs = ass.load(extracted_ass)
    visible = ass.visible_events(subs)
    if not visible:
        raise SystemExit("No visible ASS events found.")

    selected = None
    original_start = None
    at_ms = None
    result = None
    first_non_empty = None
    candidates = [_select_event(visible, args.event)] if args.event is not None else visible

    for candidate in candidates:
        isolated_ass, candidate_original_start = ass.isolate_event(
            extracted_ass,
            candidate.index,
            events_dir / f"track_{track.id}_event_{candidate.index:04d}.ass",
        )
        candidate_at_ms = (
            args.at_ms
            if args.at_ms is not None
            else min(50, max(1, candidate.duration_ms // 2))
        )
        candidate_result = render_and_crop(
            isolated_ass,
            png_dir / f"event_{candidate.index:04d}_full.png",
            png_dir / f"event_{candidate.index:04d}.png",
            size=size,
            timestamp_ms=candidate_at_ms,
            font_paths=fonts,
            libass_path=config.libass_path,
            warning_callback=lambda message: print(f"warning: {message}"),
        )
        current = (candidate, candidate_original_start, candidate_at_ms, candidate_result)
        if candidate_result.bbox is not None and first_non_empty is None:
            first_non_empty = current
        if args.event is not None or _bbox_looks_useful(candidate_result.bbox):
            selected, original_start, at_ms, result = current
            break

    if selected is None and first_non_empty is not None:
        selected, original_start, at_ms, result = first_non_empty
    if selected is None or original_start is None or at_ms is None or result is None:
        raise SystemExit("No ASS event could be rendered.")
    if result.bbox is None:
        raise SystemExit(f"ASS event index {selected.index} rendered no visible pixels.")

    print(f"MKV: {args.mkv}")
    print(f"ASS track: {track.id} {track.name!r}")
    print(f"Video size: {size[0]}x{size[1]}")
    print(f"Extracted ASS: {extracted_ass}")
    print(f"Extracted fonts: {len(fonts)}")
    print(f"Event index: {selected.index}")
    print(f"Original event time: {ass.ms_to_ass_time(original_start)} -> {ass.ms_to_ass_time(selected.end_ms)}")
    print(f"Rendered at event-local timestamp: {ass.ms_to_seconds(at_ms)}s")
    print(f"Full transparent PNG: {result.full_png}")
    print(f"Cropped PNG: {result.cropped_png}")
    print(f"Alpha bbox: {result.bbox}")


def _select_track(tracks: list[mkv.SubtitleTrack], track_id: int | None) -> mkv.SubtitleTrack:
    if track_id is None:
        return tracks[0]
    for track in tracks:
        if track.id == track_id:
            return track
    raise SystemExit(f"ASS track id {track_id} was not found.")


def _select_event(events: list[ass.AssEvent], event_index: int | None) -> ass.AssEvent:
    if event_index is None:
        return events[0]
    for event in events:
        if event.index == event_index:
            return event
    raise SystemExit(f"Visible ASS event index {event_index} was not found.")


def _bbox_looks_useful(bbox: tuple[int, int, int, int] | None) -> bool:
    if bbox is None:
        return False
    left, top, right, bottom = bbox
    width = right - left
    height = bottom - top
    return width >= 24 and height >= 16 and width * height >= 1000


def _filter_intervals_for_cli(
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
        return filtered[: max(0, max_intervals)]
    return filtered


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _add_track_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--track-index",
        "--track-id",
        dest="track_indexes",
        type=_track_indexes,
        default=None,
        metavar="INDEX[,INDEX]|all",
        help=(
            "Select Matroska ASS track IDs shown by 'tracks'. Comma-separated values are allowed; "
            "'all' or no track filters selects every ASS track. --track-id is a legacy alias."
        ),
    )
    parser.add_argument(
        "--track-name",
        type=_nonempty_text,
        default=None,
        metavar="TEXT",
        help="Select ASS tracks whose name contains TEXT (case-insensitive).",
    )
    parser.add_argument(
        "--track-lang",
        type=_nonempty_text,
        default=None,
        metavar="LANG",
        help="Select ASS tracks by IETF or Matroska language tag, for example en or eng.",
    )


def _add_library_output_mode(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--in-place",
        action="store_true",
        help="Atomically replace each original MKV after a successful conversion.",
    )
    group.add_argument(
        "--output-dir",
        type=Path,
        metavar="DIR",
        help="Write converted MKVs under DIR while preserving paths relative to the library root.",
    )


def _selector_from_args(args: argparse.Namespace) -> mkv.TrackSelector:
    return mkv.TrackSelector(
        indexes=args.track_indexes,
        name=args.track_name,
        language=args.track_lang,
    )


def _track_indexes(value: str) -> frozenset[int] | None:
    value = value.strip()
    if value.casefold() == "all":
        return None
    parts = [part.strip() for part in value.split(",")]
    if not parts or any(not part for part in parts):
        raise argparse.ArgumentTypeError("must be 'all' or a comma-separated list of track IDs")
    try:
        indexes = frozenset(int(part) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("track IDs must be integers") from exc
    if any(index < 0 for index in indexes):
        raise argparse.ArgumentTypeError("track IDs must be zero or greater")
    return indexes


def _nonempty_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("must not be empty")
    return value


if __name__ == "__main__":
    main()
