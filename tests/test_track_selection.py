from __future__ import annotations

from pathlib import Path

import pytest

from jellyfin_ass2pgs import mkv, pipeline
from jellyfin_ass2pgs.cli import build_parser
from jellyfin_ass2pgs.config import AppConfig


def test_track_filters_support_index_name_language_and_and_semantics() -> None:
    tracks = _tracks()

    assert [track.id for track in mkv.select_ass_tracks(tracks, None)] == [3, 4]
    assert [track.id for track in mkv.select_ass_tracks(
        tracks, mkv.TrackSelector(indexes=frozenset({4}))
    )] == [4]
    assert [track.id for track in mkv.select_ass_tracks(
        tracks, mkv.TrackSelector(name="DIALOGUE")
    )] == [4]
    assert [track.id for track in mkv.select_ass_tracks(
        tracks, mkv.TrackSelector(language="eng")
    )] == [3, 4]
    assert [track.id for track in mkv.select_ass_tracks(
        tracks, mkv.TrackSelector(indexes=frozenset({3, 4}), name="signs", language="en")
    )] == [3]


def test_no_match_lists_available_ass_tracks() -> None:
    with pytest.raises(mkv.TrackSelectionError) as caught:
        mkv.select_ass_tracks(_tracks(), mkv.TrackSelector(name="commentary"))

    message = str(caught.value)
    assert "No ASS track matched" in message
    assert "index=3" in message
    assert "Signs/Songs [Synthetic]" in message
    assert "index=4" in message
    assert "en/eng" in message


def test_pipeline_filters_before_extracting_any_unselected_track(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = _probe_info()
    extracted = []

    monkeypatch.setattr(mkv, "probe", lambda path: info)

    def stop_after_selected_extract(path, track_id, output):
        extracted.append(track_id)
        raise RuntimeError("stop after proving pre-extraction selection")

    monkeypatch.setattr(mkv, "extract_track", stop_after_selected_extract)
    config = AppConfig(work_dir=tmp_path / "work", font_cache=tmp_path / "fonts")

    with pytest.raises(RuntimeError, match="proving pre-extraction"):
        pipeline.convert_mkv(
            tmp_path / "episode.mkv",
            config=config,
            track_selector=mkv.TrackSelector(indexes=frozenset({4})),
        )

    assert extracted == [4]


def test_no_match_fails_before_work_directory_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mkv, "probe", lambda path: _probe_info())
    monkeypatch.setattr(
        pipeline,
        "prepare_work_directory",
        lambda *args, **kwargs: pytest.fail("work directory must not be created for an invalid selector"),
    )

    with pytest.raises(mkv.TrackSelectionError):
        pipeline.convert_mkv(
            tmp_path / "episode.mkv",
            config=AppConfig(work_dir=tmp_path / "work"),
            track_selector=mkv.TrackSelector(indexes=frozenset({99})),
        )


def test_cli_parses_track_lists_all_and_combined_filters() -> None:
    parser = build_parser()
    selected = parser.parse_args([
        "convert", "episode.mkv", "--track-index", "3,4", "--track-name", "dialogue", "--track-lang", "eng"
    ])
    all_tracks = parser.parse_args(["convert", "episode.mkv", "--track-index", "all"])

    assert selected.track_indexes == frozenset({3, 4})
    assert selected.track_name == "dialogue"
    assert selected.track_lang == "eng"
    assert all_tracks.track_indexes is None


def _tracks() -> list[mkv.SubtitleTrack]:
    return [
        mkv.SubtitleTrack(
            3, "S_TEXT/ASS", "Signs/Songs [Synthetic]", "en", True, False,
            language_ietf="en", language_legacy="eng",
        ),
        mkv.SubtitleTrack(
            4, "S_TEXT/ASS", "English Dialogue [Synthetic]", "en", False, False,
            language_ietf="en", language_legacy="eng",
        ),
    ]


def _probe_info() -> dict:
    return {
        "tracks": [
            {
                "id": track.id,
                "type": "subtitles",
                "properties": {
                    "codec_id": track.codec_id,
                    "track_name": track.name,
                    "language_ietf": track.language_ietf,
                    "language": track.language_legacy,
                    "default_track": track.default,
                    "forced_track": track.forced,
                },
            }
            for track in _tracks()
        ]
    }
