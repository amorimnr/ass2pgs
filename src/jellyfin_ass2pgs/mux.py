from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from .mkv import SubtitleTrack, generated_pgs_name, matching_pgs_track_ids
from .tools import find_tool, run


def mux_sup(
    mkv_path: Path,
    sup_path: Path,
    ass_track: SubtitleTrack,
    *,
    info: dict,
    output_path: Path | None = None,
    force: bool = False,
) -> Path:
    in_place = output_path is None
    destination = output_path or mkv_path
    temp_output = destination.with_name(
        f".{destination.stem}.jellyfin-ass2pgs-{uuid4().hex}.tmp{destination.suffix}"
    )
    temp_output.parent.mkdir(parents=True, exist_ok=True)

    args = [find_tool("mkvmerge"), "-o", temp_output]
    if force:
        matches = matching_pgs_track_ids(info, ass_track, include_legacy_name=True)
        if matches:
            args.extend(["--subtitle-tracks", ",".join(f"!{track_id}" for track_id in matches)])
    args.append(mkv_path)

    language = ass_track.language or "und"
    args.extend(["--language", f"0:{language}"])
    args.extend(["--track-name", f"0:{generated_pgs_name(ass_track)}"])
    args.extend(["--default-track-flag", f"0:{_bool_flag(ass_track.default)}"])
    args.extend(["--forced-display-flag", f"0:{_bool_flag(ass_track.forced)}"])
    args.append(sup_path)

    try:
        run(args, process_metric="mkvmerge_mux")
        os.replace(temp_output, destination)
    except BaseException:
        temp_output.unlink(missing_ok=True)
        raise

    if in_place:
        return mkv_path
    return destination


def _bool_flag(value: bool) -> str:
    return "1" if value else "0"
