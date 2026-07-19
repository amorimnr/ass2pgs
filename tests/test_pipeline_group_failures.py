from __future__ import annotations

from pathlib import Path

from PIL import Image
import pytest

from jellyfin_ass2pgs import pipeline
from jellyfin_ass2pgs.libass_renderer import LibassRenderError
from jellyfin_ass2pgs.renderplan import DynamicRenderGroup


class _RecordingWriter:
    def __init__(self) -> None:
        self.calls = []

    def write_object(self, start_ms, end_ms, obj) -> bool:
        self.calls.append((start_ms, end_ms, obj))
        return True


def test_failed_group_aborts_without_retry() -> None:
    failure = _failure()

    with pytest.raises(pipeline.GroupConversionError) as caught:
        pipeline._run_group_with_retries(
            lambda: _raise(failure),
            track_id=3,
            group_index=7,
            retry_failed_groups=0,
            progress=None,
        )

    assert caught.value.track_id == 3
    assert caught.value.group_index == 7
    assert caught.value.attempts == 1


def test_failed_group_is_retried_and_committed_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    writer = _RecordingWriter()
    image = Image.new("RGBA", (2, 2), (255, 255, 255, 255))
    group = DynamicRenderGroup(0, 100, (), 2)

    def fake_render(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        kwargs["on_change"](0, 10, 20, image)
        if attempts == 1:
            raise _failure()
        return 1

    monkeypatch.setattr(pipeline, "render_dynamic_group", fake_render)

    def action() -> tuple[int, int]:
        return pipeline._write_dynamic_group(
            writer,
            object(),
            group_index=1,
            track_id=3,
            render_fps=12.0,
            group=group,
            matrix="bt709",
            progress=None,
        )

    written, changes = pipeline._run_group_with_retries(
        action,
        track_id=3,
        group_index=1,
        retry_failed_groups=1,
        progress=None,
    )

    assert attempts == 2
    assert written == 1
    assert changes == 1
    assert [(start, end) for start, end, _ in writer.calls] == [(0, 100)]


def test_partial_sup_is_renamed_to_failed(tmp_path: Path) -> None:
    partial = tmp_path / "track_3.sup.partial"
    failed = tmp_path / "track_3.sup.failed"
    final = tmp_path / "track_3.sup"
    partial.write_bytes(b"partial")

    pipeline._mark_sup_failed(partial, failed)

    assert not partial.exists()
    assert not final.exists()
    assert failed.read_bytes() == b"partial"


def _failure() -> LibassRenderError:
    return LibassRenderError("simulated direct libass failure")


def _raise(error: BaseException):
    raise error
