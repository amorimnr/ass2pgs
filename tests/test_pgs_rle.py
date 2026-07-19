from __future__ import annotations

import numpy as np
import pytest

from jellyfin_ass2pgs.pgs import _write_run, encode_rle


@pytest.mark.parametrize(
    ("width", "height", "seed"),
    [
        (1, 1, 1),
        (63, 7, 2),
        (64, 7, 3),
        (1920, 32, 4),
        (20_000, 2, 5),
    ],
)
def test_vectorized_rle_matches_reference(width: int, height: int, seed: int) -> None:
    random = np.random.default_rng(seed)
    pixels = random.integers(0, 12, size=(height, width), dtype=np.uint8)
    if width > 0x3FFF:
        pixels.fill(0)
    pixels[:, : min(width, 50)] = 0
    indices = pixels.tobytes()

    assert encode_rle(indices, width, height) == _reference_rle(indices, width, height)


def _reference_rle(indices: bytes, width: int, height: int) -> bytes:
    encoded = bytearray()
    for y in range(height):
        row = indices[y * width : (y + 1) * width]
        x = 0
        while x < width:
            value = row[x]
            run = 1
            while x + run < width and row[x + run] == value:
                run += 1
            _write_run(encoded, value, run)
            x += run
        encoded.extend(b"\x00\x00")
    return bytes(encoded)
