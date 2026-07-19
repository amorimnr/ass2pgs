from __future__ import annotations

from pathlib import Path

import pytest

from jellyfin_ass2pgs import library, mkv, mux, pipeline
from jellyfin_ass2pgs.cli import build_parser
from jellyfin_ass2pgs.config import AppConfig


def test_generated_pgs_name_is_the_incremental_marker() -> None:
    ass_track = _ass_track()
    info = _pgs_info("English (PGS)")

    assert mkv.generated_pgs_name(ass_track) == "English (PGS)"
    assert mkv.has_matching_pgs(info, ass_track)
    assert not mkv.has_matching_pgs(_pgs_info("English"), ass_track)
    assert mkv.matching_pgs_track_ids(
        _pgs_info("English"), ass_track, include_legacy_name=True
    ) == [5]


def test_probe_rejects_an_unrecognized_container(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        stdout = '{"container":{"recognized":false,"supported":false}}'

    monkeypatch.setattr(mkv, "find_tool", lambda name: name)
    monkeypatch.setattr(mkv, "run", lambda *args, **kwargs: Result())

    with pytest.raises(RuntimeError, match="Not a recognized"):
        mkv.probe(Path("broken.mkv"))


def test_probe_accepts_localized_matroska_container_name(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        stdout = (
            '{"container":{"recognized":true,"supported":true,'
            '"type":"Ficheiro MKV","properties":{"container_type":17}}}'
        )

    monkeypatch.setattr(mkv, "find_tool", lambda name: name)
    monkeypatch.setattr(mkv, "run", lambda *args, **kwargs: Result())

    info = mkv.probe(Path("episode.mkv"))

    assert info["container"]["type"] == "Ficheiro MKV"


def test_probe_rejects_non_matroska_by_stable_container_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        stdout = (
            '{"container":{"recognized":true,"supported":true,'
            '"type":"Ficheiro MP4","properties":{"container_type":25}}}'
        )

    monkeypatch.setattr(mkv, "find_tool", lambda name: name)
    monkeypatch.setattr(mkv, "run", lambda *args, **kwargs: Result())

    with pytest.raises(RuntimeError, match="Expected a Matroska"):
        mkv.probe(Path("renamed.mkv"))


def test_mux_appends_pgs_and_atomically_replaces_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mkv"
    destination = tmp_path / "output.mkv"
    sup = tmp_path / "track.sup"
    source.write_bytes(b"source")
    destination.write_bytes(b"old")
    sup.write_bytes(b"sup")
    commands: list[list[str]] = []

    monkeypatch.setattr(mux, "find_tool", lambda name: name)

    def fake_run(args, *, process_metric):
        command = [str(value) for value in args]
        commands.append(command)
        Path(command[2]).write_bytes(b"new")

    monkeypatch.setattr(mux, "run", fake_run)

    result = mux.mux_sup(
        source,
        sup,
        _ass_track(),
        info=_pgs_info("unrelated"),
        output_path=destination,
    )

    assert result == destination
    assert destination.read_bytes() == b"new"
    assert "0:English (PGS)" in commands[0]
    assert not list(tmp_path.glob("*.jellyfin-ass2pgs-*.tmp.mkv"))


def test_library_preserves_relative_paths_skips_and_continues_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "library"
    output = root / "generated"
    (root / "season").mkdir(parents=True)
    converted_source = root / "season" / "converted.mkv"
    skipped_source = root / "season" / "existing.MKV"
    broken_source = root / "broken.mkv"
    for path in (converted_source, skipped_source, broken_source):
        path.write_bytes(path.name.encode("ascii"))

    calls: list[Path] = []

    def fake_convert(path: Path, *, output_path: Path, **kwargs) -> pipeline.ConversionResult:
        calls.append(path)
        if path == broken_source:
            raise RuntimeError("invalid Matroska data")
        if path == skipped_source:
            return pipeline.ConversionResult(
                path,
                output_path,
                [pipeline.TrackConversionResult(3, None, 0, 0, True, "matching PGS already exists")],
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"converted")
        return pipeline.ConversionResult(
            path,
            output_path,
            [pipeline.TrackConversionResult(3, None, 1, 1)],
        )

    monkeypatch.setattr(library, "convert_mkv", fake_convert)
    config = AppConfig(
        workers=2,
        state_file=tmp_path / "state.json",
        work_dir=tmp_path / "work",
        font_cache=tmp_path / "fonts",
    )

    result = library.convert_library(root, config=config, output_dir=output)

    assert result.files_total == 3
    assert result.files_converted == 1
    assert result.files_skipped == 1
    assert result.files_failed == 1
    assert output.joinpath("season", "converted.mkv").read_bytes() == b"converted"
    assert set(calls) == {converted_source, skipped_source, broken_source}
    assert result.failures == [(broken_source, "invalid Matroska data")]
    saved_state = library._load_state(config.state_file)
    assert library._is_completed(
        saved_state,
        skipped_source,
        output_path=output / "season" / "existing.MKV",
        track_selector=None,
    )

    calls.clear()
    resumed = library.resume_library(root, config=config, output_dir=output)

    assert resumed.files_total == 3
    assert resumed.files_converted == 0
    assert resumed.files_skipped == 2
    assert resumed.files_failed == 1
    assert calls == [broken_source]


def test_library_requires_an_explicit_output_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        library.convert_library(tmp_path, config=AppConfig())


def test_cli_requires_and_parses_library_output_mode() -> None:
    parser = build_parser()
    in_place = parser.parse_args(["convert-library", "library", "--in-place"])
    separate = parser.parse_args(["resume", "library", "--output-dir", "converted"])

    assert in_place.in_place
    assert in_place.output_dir is None
    assert not separate.in_place
    assert separate.output_dir == Path("converted")
    with pytest.raises(SystemExit):
        parser.parse_args(["convert-library", "library"])


def test_pipeline_honors_cancellation_before_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mkv, "probe", lambda path: pytest.fail("probe must not run"))

    with pytest.raises(pipeline.ConversionCancelledError):
        pipeline.convert_mkv(
            tmp_path / "episode.mkv",
            config=AppConfig(),
            cancel_requested=lambda: True,
        )


def _ass_track() -> mkv.SubtitleTrack:
    return mkv.SubtitleTrack(
        3,
        "S_TEXT/ASS",
        "English",
        "en",
        False,
        False,
        language_ietf="en",
        language_legacy="eng",
    )


def _pgs_info(name: str) -> dict:
    return {
        "tracks": [
            {
                "id": 5,
                "type": "subtitles",
                "properties": {
                    "codec_id": "S_HDMV/PGS",
                    "track_name": name,
                    "language_ietf": "en",
                    "language": "eng",
                    "default_track": False,
                    "forced_track": False,
                },
            }
        ]
    }
