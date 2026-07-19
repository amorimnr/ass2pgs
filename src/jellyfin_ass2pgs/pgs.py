from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image

from .metrics import current_metrics


@dataclass(frozen=True)
class PgsObject:
    x: int
    y: int
    width: int
    height: int
    palette: list[tuple[int, int, int, int]]
    indices: bytes
    rle: bytes


def image_to_pgs_object(image: Image.Image, *, matrix: str = "bt709") -> PgsObject | None:
    rgba = image.convert("RGBA")
    bbox = rgba.getchannel("A").getbbox()
    if bbox is None:
        return None

    cropped = rgba.crop(bbox)
    return cropped_rgba_to_pgs_object(cropped, x=bbox[0], y=bbox[1], matrix=matrix)


def cropped_rgba_to_pgs_object(
    cropped: Image.Image,
    *,
    x: int,
    y: int,
    matrix: str = "bt709",
) -> PgsObject | None:
    cropped = cropped.convert("RGBA")
    if cropped.getchannel("A").getbbox() is None:
        return None
    metrics = current_metrics()
    with metrics.time("quantization") if metrics else _null_timer():
        quantized = cropped.quantize(colors=255, method=Image.Quantize.FASTOCTREE)
    source_palette = _quantized_palette(quantized)
    alpha_table = _quantized_alpha_table(quantized)

    with metrics.time("palette_remap") if metrics else _null_timer():
        source_indices = np.asarray(quantized, dtype=np.uint8).reshape(-1)
        original_alpha = np.asarray(cropped.getchannel("A"), dtype=np.uint8).reshape(-1)
        visible_positions = np.flatnonzero(original_alpha)
        visible_source = source_indices[visible_positions]

        palette: list[tuple[int, int, int, int]] = [(0, 0, 0, 0)]
        lookup = np.zeros(256, dtype=np.uint8)
        if visible_source.size:
            source_values, first_offsets = np.unique(visible_source, return_index=True)
            order = np.argsort(first_offsets)
            source_values = source_values[order]
            first_positions = visible_positions[first_offsets[order]]
            lookup[source_values] = np.arange(1, len(source_values) + 1, dtype=np.uint8)
            for source_index, first_position in zip(source_values, first_positions, strict=True):
                source_index = int(source_index)
                alpha = int(original_alpha[first_position])
                r, g, b = source_palette[source_index]
                entry_alpha = alpha_table[source_index]
                if entry_alpha == 255 and alpha != 255:
                    entry_alpha = alpha
                palette.append((*rgb_to_ycrcb(r, g, b, matrix=matrix), entry_alpha))

        output_array = lookup[source_indices]
        output_array[original_alpha == 0] = 0
        output_indices = output_array.tobytes()

    with metrics.time("rle") if metrics else _null_timer():
        rle = encode_rle(output_indices, cropped.width, cropped.height)

    return PgsObject(
        x=x,
        y=y,
        width=cropped.width,
        height=cropped.height,
        palette=palette,
        indices=output_indices,
        rle=rle,
    )


def encode_rle(indices: bytes, width: int, height: int) -> bytes:
    if width <= 0 or height <= 0 or len(indices) != width * height:
        raise ValueError("PGS index buffer dimensions are inconsistent.")
    pixels = np.frombuffer(indices, dtype=np.uint8).reshape(height, width)
    if width > 0x3FFF:
        return _encode_rle_wide(pixels, width)

    changes = np.empty((height, width), dtype=bool)
    changes[:, 0] = True
    changes[:, 1:] = pixels[:, 1:] != pixels[:, :-1]
    run_y, run_x = np.nonzero(changes)
    run_ends_x = np.empty_like(run_x)
    run_ends_x[:-1] = np.where(run_y[:-1] == run_y[1:], run_x[1:], width)
    run_ends_x[-1] = width
    run_lengths = run_ends_x - run_x
    values = pixels[run_y, run_x]

    direct = (values != 0) & (run_lengths <= 2)
    zero_short = (values == 0) & (run_lengths <= 0x3F)
    zero_long = (values == 0) & ~zero_short
    color_short = (values != 0) & ~direct & (run_lengths <= 0x3F)
    color_long = (values != 0) & ~direct & ~color_short

    encoded_lengths = np.empty(len(run_x), dtype=np.int64)
    encoded_lengths[direct] = run_lengths[direct]
    encoded_lengths[zero_short] = 2
    encoded_lengths[zero_long] = 3
    encoded_lengths[color_short] = 3
    encoded_lengths[color_long] = 4
    run_end_offsets = np.cumsum(encoded_lengths)
    output_positions = run_end_offsets - encoded_lengths + 2 * run_y
    encoded = np.empty(int(run_end_offsets[-1] + 2 * height), dtype=np.uint8)

    positions = output_positions[direct]
    direct_values = values[direct]
    direct_lengths = run_lengths[direct]
    encoded[positions] = direct_values
    two_pixel_positions = positions[direct_lengths == 2]
    encoded[two_pixel_positions + 1] = direct_values[direct_lengths == 2]

    positions = output_positions[zero_short]
    encoded[positions] = 0
    encoded[positions + 1] = run_lengths[zero_short]

    positions = output_positions[zero_long]
    lengths = run_lengths[zero_long]
    encoded[positions] = 0
    encoded[positions + 1] = 0x40 | (lengths >> 8)
    encoded[positions + 2] = lengths & 0xFF

    positions = output_positions[color_short]
    lengths = run_lengths[color_short]
    encoded[positions] = 0
    encoded[positions + 1] = 0x80 | lengths
    encoded[positions + 2] = values[color_short]

    positions = output_positions[color_long]
    lengths = run_lengths[color_long]
    encoded[positions] = 0
    encoded[positions + 1] = 0xC0 | (lengths >> 8)
    encoded[positions + 2] = lengths & 0xFF
    encoded[positions + 3] = values[color_long]

    row_counts = np.bincount(run_y, minlength=height)
    last_run_per_row = np.cumsum(row_counts) - 1
    row_end_positions = run_end_offsets[last_run_per_row] + 2 * np.arange(height)
    encoded[row_end_positions] = 0
    encoded[row_end_positions + 1] = 0
    return encoded.tobytes()


def _encode_rle_wide(pixels: np.ndarray, width: int) -> bytes:
    encoded = bytearray()
    for row in pixels:
        boundaries = np.flatnonzero(row[1:] != row[:-1]) + 1
        starts = np.concatenate(([0], boundaries))
        ends = np.concatenate((boundaries, [width]))
        for start, end in zip(starts, ends, strict=True):
            _write_run(encoded, int(row[start]), int(end - start))
        encoded.extend(b"\x00\x00")
    return bytes(encoded)


def rgb_to_ycrcb(r: int, g: int, b: int, *, matrix: str = "bt709") -> tuple[int, int, int]:
    if matrix.lower() == "bt601":
        y = 16 + (65.481 * r + 128.553 * g + 24.966 * b) / 255
        cb = 128 + (-37.797 * r - 74.203 * g + 112.0 * b) / 255
        cr = 128 + (112.0 * r - 93.786 * g - 18.214 * b) / 255
    else:
        y = 16 + (46.742 * r + 157.243 * g + 15.874 * b) / 255
        cb = 128 + (-25.765 * r - 86.674 * g + 112.439 * b) / 255
        cr = 128 + (112.439 * r - 102.129 * g - 10.31 * b) / 255
    return _clamp(round(y)), _clamp(round(cr)), _clamp(round(cb))


def _write_run(encoded: bytearray, value: int, run: int) -> None:
    if value != 0 and run <= 2:
        encoded.extend([value] * run)
        return

    while run > 0:
        chunk = min(run, 0x3FFF)
        if value == 0:
            if chunk <= 0x3F:
                encoded.extend((0x00, chunk))
            else:
                encoded.extend((0x00, 0x40 | (chunk >> 8), chunk & 0xFF))
        else:
            if chunk <= 0x3F:
                encoded.extend((0x00, 0x80 | chunk, value))
            else:
                encoded.extend((0x00, 0xC0 | (chunk >> 8), chunk & 0xFF, value))
        run -= chunk


def _quantized_palette(image: Image.Image) -> list[tuple[int, int, int]]:
    raw = image.getpalette() or []
    return [
        (raw[index], raw[index + 1], raw[index + 2])
        for index in range(0, min(len(raw), 256 * 3), 3)
    ]


def _quantized_alpha_table(image: Image.Image) -> list[int]:
    transparency = image.info.get("transparency")
    if isinstance(transparency, bytes):
        return list(transparency) + [255] * (256 - len(transparency))
    if isinstance(transparency, list):
        return transparency + [255] * (256 - len(transparency))
    if isinstance(transparency, int):
        table = [255] * 256
        table[transparency] = 0
        return table
    return [255] * 256


def _clamp(value: int) -> int:
    return max(0, min(255, value))


class _null_timer:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False
