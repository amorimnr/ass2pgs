from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct

from PIL import Image

from .metrics import current_metrics
from .pgs import PgsObject, image_to_pgs_object


SEGMENT_PDS = 0x14
SEGMENT_ODS = 0x15
SEGMENT_PCS = 0x16
SEGMENT_WDS = 0x17
SEGMENT_END = 0x80


@dataclass(frozen=True)
class SupCue:
    start_ms: int
    end_ms: int
    image: Image.Image


class SupWriter:
    def __init__(self, path: Path, *, video_size: tuple[int, int], matrix: str = "bt709") -> None:
        self.path = path
        self.video_width, self.video_height = video_size
        self.matrix = matrix
        self.composition_number = 0
        self.palette_version = 0
        self.object_version = 0
        self.file = None

    def __enter__(self) -> "SupWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("wb")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.file:
            self.file.close()

    def write_cue(self, cue: SupCue) -> bool:
        obj = image_to_pgs_object(cue.image, matrix=self.matrix)
        if obj is None:
            return False
        return self.write_object(cue.start_ms, cue.end_ms, obj)

    def write_object(self, start_ms: int, end_ms: int, obj: PgsObject) -> bool:
        if end_ms <= start_ms:
            return False
        metrics = current_metrics()
        with metrics.time("pgs_object_total") if metrics else _null_timer():
            self._write_display(start_ms, obj)
            self._write_clear(end_ms)
        if metrics:
            metrics.inc("pgs_objects")
        return True

    def _write_display(self, pts_ms: int, obj: PgsObject) -> None:
        object_id = 0
        window_id = 0
        palette_id = 0
        self.composition_number = (self.composition_number + 1) & 0xFFFF
        self.palette_version = (self.palette_version + 1) & 0xFF
        self.object_version = (self.object_version + 1) & 0xFF

        self._write_segment(pts_ms, SEGMENT_PCS, _pcs(
            self.video_width,
            self.video_height,
            self.composition_number,
            composition_state=0x80,
            palette_id=palette_id,
            objects=[(object_id, window_id, obj.x, obj.y)],
        ))
        self._write_segment(pts_ms, SEGMENT_WDS, _wds(window_id, obj.x, obj.y, obj.width, obj.height))
        self._write_segment(pts_ms, SEGMENT_PDS, _pds(palette_id, self.palette_version, obj.palette))
        for payload in _ods_chunks(object_id, self.object_version, obj.width, obj.height, obj.rle):
            self._write_segment(pts_ms, SEGMENT_ODS, payload)
        self._write_segment(pts_ms, SEGMENT_END, b"")

    def _write_clear(self, pts_ms: int) -> None:
        self.composition_number = (self.composition_number + 1) & 0xFFFF
        self._write_segment(pts_ms, SEGMENT_PCS, _pcs(
            self.video_width,
            self.video_height,
            self.composition_number,
            composition_state=0x00,
            palette_id=0,
            objects=[],
        ))
        self._write_segment(pts_ms, SEGMENT_END, b"")

    def _write_segment(self, pts_ms: int, segment_type: int, payload: bytes) -> None:
        if self.file is None:
            raise RuntimeError("SupWriter must be used as a context manager.")
        if len(payload) > 0xFFFF:
            raise ValueError(f"PGS segment too large: {len(payload)} bytes")
        timestamp = int(round(pts_ms * 90))
        header = b"PG" + struct.pack(">IIBH", timestamp, timestamp, segment_type, len(payload))
        metrics = current_metrics()
        with metrics.time("sup_write") if metrics else _null_timer():
            self.file.write(header)
            self.file.write(payload)


def write_sup(path: Path, cues: list[SupCue], *, video_size: tuple[int, int], matrix: str = "bt709") -> int:
    written = 0
    with SupWriter(path, video_size=video_size, matrix=matrix) as writer:
        for cue in cues:
            if cue.end_ms <= cue.start_ms:
                continue
            if writer.write_cue(cue):
                written += 1
    return written


def _pcs(
    width: int,
    height: int,
    composition_number: int,
    *,
    composition_state: int,
    palette_id: int,
    objects: list[tuple[int, int, int, int]],
) -> bytes:
    payload = bytearray()
    payload.extend(struct.pack(">HHB", width, height, 0x10))
    payload.extend(struct.pack(">HBBB", composition_number, composition_state, 0x00, palette_id))
    payload.append(len(objects))
    for object_id, window_id, x, y in objects:
        payload.extend(struct.pack(">HBBHH", object_id, window_id, 0x00, x, y))
    return bytes(payload)


def _wds(window_id: int, x: int, y: int, width: int, height: int) -> bytes:
    return struct.pack(">BBHHHH", 1, window_id, x, y, width, height)


def _pds(palette_id: int, palette_version: int, palette: list[tuple[int, int, int, int]]) -> bytes:
    payload = bytearray((palette_id, palette_version))
    for index, (y, cr, cb, alpha) in enumerate(palette):
        payload.extend((index, y, cr, cb, alpha))
    return bytes(payload)


def _ods_chunks(object_id: int, object_version: int, width: int, height: int, rle: bytes) -> list[bytes]:
    object_data = struct.pack(">HH", width, height) + rle
    object_data_length = len(object_data)
    if object_data_length > 0xFFFFFF:
        raise ValueError(f"PGS object too large: {object_data_length} bytes")

    first_capacity = 0xFFFF - 7
    next_capacity = 0xFFFF - 4
    chunks = []
    offset = 0
    first = True
    while offset < object_data_length:
        capacity = first_capacity if first else next_capacity
        part = object_data[offset : offset + capacity]
        offset += len(part)
        last = offset >= object_data_length
        flags = (0x80 if first else 0x00) | (0x40 if last else 0x00)
        payload = bytearray(struct.pack(">HBB", object_id, object_version, flags))
        if first:
            payload.extend(object_data_length.to_bytes(3, "big"))
        payload.extend(part)
        chunks.append(bytes(payload))
        first = False
    return chunks


class _null_timer:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False
