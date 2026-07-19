from __future__ import annotations

from pathlib import Path

from PIL import Image
import pytest

from jellyfin_ass2pgs.libass_renderer import LibassRenderer, LibassRenderError, RenderedBitmap
from jellyfin_ass2pgs import renderer
from jellyfin_ass2pgs.renderplan import DynamicRenderGroup, StaticRenderGroup, StaticSample


class _FakeRenderer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.timestamps = []

    def render(self, timestamp_ms: int, *, expect_content: bool = False) -> RenderedBitmap:
        self.timestamps.append((timestamp_ms, expect_content))
        if self.fail:
            raise LibassRenderError("simulated direct libass failure")
        image = Image.new("RGBA", (2, 2), (255, 255, 255, 255))
        return RenderedBitmap(10, 20, image, 1, 1, 0.001, 0.002, 0.0, 0.0)


def test_uninitialized_renderer_fails_cleanly() -> None:
    direct = LibassRenderer(Path("unused.ass"), size=(1920, 1080))

    with pytest.raises(LibassRenderError, match="not initialized"):
        direct.render(0)


def test_negative_timestamp_is_rejected() -> None:
    direct = LibassRenderer(Path("unused.ass"), size=(1920, 1080))

    with pytest.raises(LibassRenderError, match="negative"):
        direct.render(-1)


def test_direct_failure_aborts_static_group() -> None:
    sample = StaticSample(timestamp_ms=50, start_ms=0, end_ms=100)
    group = StaticRenderGroup(0, 100, (sample,))

    with pytest.raises(LibassRenderError, match="simulated"):
        renderer.render_static_group(
            _FakeRenderer(fail=True),
            group,
            on_sample=lambda *args: None,
        )


def test_dynamic_renderer_uses_configured_render_fps() -> None:
    direct = _FakeRenderer()
    group = DynamicRenderGroup(0, 1_000, (), 12)

    renderer.render_dynamic_group(
        direct,
        group,
        render_fps=12.0,
        on_change=lambda *args: None,
    )

    assert [timestamp for timestamp, _ in direct.timestamps] == [
        0, 83, 167, 250, 333, 417, 500, 583, 667, 750, 833, 917
    ]
    assert not any(expect_content for _, expect_content in direct.timestamps)
