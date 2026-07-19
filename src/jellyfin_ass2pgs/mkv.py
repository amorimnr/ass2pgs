from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .tools import find_tool, run


@dataclass(frozen=True)
class SubtitleTrack:
    id: int
    codec_id: str
    name: str
    language: str
    default: bool
    forced: bool
    language_ietf: str = ""
    language_legacy: str = ""

    @property
    def language_tags(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                tag for tag in (self.language, self.language_ietf, self.language_legacy) if tag
            )
        )


@dataclass(frozen=True)
class TrackSelector:
    indexes: frozenset[int] | None = None
    name: str | None = None
    language: str | None = None

    @property
    def active(self) -> bool:
        return self.indexes is not None or bool(self.name) or bool(self.language)

    def matches(self, track: SubtitleTrack) -> bool:
        if self.indexes is not None and track.id not in self.indexes:
            return False
        if self.name and self.name.casefold() not in track.name.casefold():
            return False
        if self.language and not _language_matches(self.language, track.language_tags):
            return False
        return True


class TrackSelectionError(ValueError):
    def __init__(self, selector: TrackSelector, available: list[SubtitleTrack]) -> None:
        self.selector = selector
        self.available = tuple(available)
        requested = []
        if selector.indexes is not None:
            requested.append("index=" + ",".join(str(index) for index in sorted(selector.indexes)))
        if selector.name:
            requested.append(f"name contains {selector.name!r}")
        if selector.language:
            requested.append(f"language={selector.language!r}")
        lines = [f"No ASS track matched {' AND '.join(requested) or 'the selection'}. Available ASS tracks:"]
        if available:
            lines.extend(f"  {_format_available_track(track)}" for track in available)
        else:
            lines.append("  (none)")
        super().__init__("\n".join(lines))


def subtitle_tracks(info: dict) -> list[SubtitleTrack]:
    tracks = []
    for track in info.get("tracks", []):
        if track.get("type") != "subtitles":
            continue
        props = track.get("properties", {})
        tracks.append(
            SubtitleTrack(
                id=int(track["id"]),
                codec_id=props.get("codec_id", ""),
                name=props.get("track_name", ""),
                language=props.get("language_ietf") or props.get("language", ""),
                default=bool(props.get("default_track")),
                forced=bool(props.get("forced_track")),
                language_ietf=props.get("language_ietf", ""),
                language_legacy=props.get("language", ""),
            )
        )
    return tracks


def probe(path: Path) -> dict:
    mkvmerge = find_tool("mkvmerge")
    result = run([mkvmerge, "-J", path], process_metric="mkvmerge_probe")
    info = json.loads(result.stdout)
    container = info.get("container", {})
    if not container.get("recognized") or not container.get("supported"):
        raise RuntimeError(f"Not a recognized or supported Matroska file: {path}")
    if container.get("type") != "Matroska":
        raise RuntimeError(f"Expected a Matroska container, found {container.get('type', 'unknown')}: {path}")
    return info


def ass_tracks(info: dict) -> list[SubtitleTrack]:
    return [track for track in subtitle_tracks(info) if track.codec_id == "S_TEXT/ASS"]


def select_ass_tracks(
    tracks: list[SubtitleTrack],
    selector: TrackSelector | None,
) -> list[SubtitleTrack]:
    if selector is None or not selector.active:
        return list(tracks)
    selected = [track for track in tracks if selector.matches(track)]
    if not selected:
        raise TrackSelectionError(selector, tracks)
    return selected


def pgs_tracks(info: dict) -> list[SubtitleTrack]:
    return [
        track
        for track in subtitle_tracks(info)
        if track.codec_id in {"S_HDMV/PGS", "S_HDMV/PGS_FORCED"}
    ]


def has_matching_pgs(info: dict, ass_track: SubtitleTrack) -> bool:
    return bool(matching_pgs_track_ids(info, ass_track))


def generated_pgs_name(ass_track: SubtitleTrack) -> str:
    return f"{ass_track.name} (PGS)" if ass_track.name else "(PGS)"


def matching_pgs_track_ids(
    info: dict,
    ass_track: SubtitleTrack,
    *,
    include_legacy_name: bool = False,
) -> list[int]:
    expected_names = {generated_pgs_name(ass_track)}
    if include_legacy_name:
        expected_names.add(ass_track.name)
    matches = []
    for pgs_track in pgs_tracks(info):
        if (
            pgs_track.language == ass_track.language
            and pgs_track.name in expected_names
            and pgs_track.default == ass_track.default
            and pgs_track.forced == ass_track.forced
        ):
            matches.append(pgs_track.id)
    return matches


def video_size(info: dict) -> tuple[int, int]:
    for track in info.get("tracks", []):
        if track.get("type") != "video":
            continue
        props = track.get("properties", {})
        dimensions = props.get("display_dimensions") or props.get("pixel_dimensions")
        if dimensions:
            width, height = dimensions.lower().split("x", 1)
            return int(width), int(height)
    raise RuntimeError("No video track with dimensions was found.")


def video_frame_ms(info: dict) -> float:
    for track in info.get("tracks", []):
        if track.get("type") != "video":
            continue
        duration = track.get("properties", {}).get("default_duration")
        if duration:
            return int(duration) / 1_000_000
    return 1000 / 23.976


def extract_track(mkv_path: Path, track_id: int, output_ass: Path) -> Path:
    output_ass.parent.mkdir(parents=True, exist_ok=True)
    mkvextract = find_tool("mkvextract")
    run([mkvextract, "tracks", mkv_path, f"{track_id}:{output_ass}"])
    return output_ass


def extract_font_attachments(info: dict, mkv_path: Path, fonts_dir: Path) -> list[Path]:
    attachments = []
    fonts_dir.mkdir(parents=True, exist_ok=True)

    for attachment in info.get("attachments", []):
        content_type = attachment.get("content_type", "").lower()
        file_name = attachment.get("file_name", f"attachment-{attachment['id']}")
        if not (content_type.startswith("font/") or Path(file_name).suffix.lower() in {".ttf", ".otf", ".ttc"}):
            continue

        safe_name = _safe_file_name(file_name)
        attachments.append((attachment["id"], fonts_dir / safe_name))

    if not attachments:
        return []

    mkvextract = find_tool("mkvextract")
    args = [mkvextract, "attachments", mkv_path]
    args.extend(f"{attachment_id}:{output_path}" for attachment_id, output_path in attachments)
    run(args)
    return [output_path for _, output_path in attachments]


def _safe_file_name(file_name: str) -> str:
    name = Path(file_name).name
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)


def _language_matches(query: str, tags: tuple[str, ...]) -> bool:
    requested = _normalize_language_tag(query)
    for tag in tags:
        candidate = _normalize_language_tag(tag)
        if requested == candidate:
            return True
        if len(requested) == 2 and candidate.split("-", 1)[0] == requested:
            return True
    return False


def _normalize_language_tag(value: str) -> str:
    return value.strip().replace("_", "-").casefold()


def _format_available_track(track: SubtitleTrack) -> str:
    name = track.name or "(unnamed)"
    tags = "/".join(track.language_tags) or "und"
    return (
        f"index={track.id} name={name!r} language={tags} "
        f"default={track.default} forced={track.forced}"
    )
