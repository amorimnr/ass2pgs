from __future__ import annotations

import ctypes
import ctypes.util
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
from time import perf_counter
from typing import Callable, Iterable

from PIL import Image
import pysubs2

from .metrics import current_metrics


DEFAULT_WINDOWS_LIBASS = Path(r"C:\msys64\ucrt64\bin\libass-9.dll")


class LibassRenderError(RuntimeError):
    pass


class ASSImage(ctypes.Structure):
    pass


ASSImagePointer = ctypes.POINTER(ASSImage)
ASSImage._fields_ = [
    ("w", ctypes.c_int),
    ("h", ctypes.c_int),
    ("stride", ctypes.c_int),
    ("bitmap", ctypes.POINTER(ctypes.c_ubyte)),
    ("color", ctypes.c_uint32),
    ("dst_x", ctypes.c_int),
    ("dst_y", ctypes.c_int),
    ("next", ASSImagePointer),
    ("type", ctypes.c_int),
]


class ASSTrackHeader(ctypes.Structure):
    _fields_ = [
        ("n_styles", ctypes.c_int),
        ("max_styles", ctypes.c_int),
        ("n_events", ctypes.c_int),
        ("max_events", ctypes.c_int),
        ("styles", ctypes.c_void_p),
        ("events", ctypes.c_void_p),
        ("style_format", ctypes.c_char_p),
        ("event_format", ctypes.c_char_p),
        ("track_type", ctypes.c_int),
        ("PlayResX", ctypes.c_int),
        ("PlayResY", ctypes.c_int),
        ("Timer", ctypes.c_double),
        ("WrapStyle", ctypes.c_int),
        ("ScaledBorderAndShadow", ctypes.c_int),
        ("Kerning", ctypes.c_int),
        ("Language", ctypes.c_char_p),
        ("YCbCrMatrix", ctypes.c_int),
    ]


@dataclass(frozen=True)
class RenderedBitmap:
    x: int
    y: int
    image: Image.Image | None
    layer_count: int
    change: int
    render_s: float
    compose_s: float
    bbox_s: float
    crop_s: float


@dataclass(frozen=True)
class FontValidation:
    required: tuple[str, ...]
    available: tuple[str, ...]
    missing_from_attachments: tuple[str, ...]
    unreadable_attachments: tuple[Path, ...]


MessageCallback = Callable[[str], None]
_ASS_MESSAGE_CALLBACK = ctypes.CFUNCTYPE(
    None,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
)


class LibassRenderer:
    """One reusable libass library/renderer/track context.

    The returned pixels are already cropped to the minimum visible alpha box.
    libass owns the ASS_Image list, so composition is completed before the next
    call to ass_render_frame.
    """

    def __init__(
        self,
        ass_path: Path,
        *,
        size: tuple[int, int],
        font_paths: Iterable[Path] = (),
        libass_path: Path | str | None = None,
        warning_callback: MessageCallback | None = None,
    ) -> None:
        self.ass_path = ass_path.resolve()
        self.size = size
        self.font_paths = tuple(Path(path).resolve() for path in font_paths)
        self.libass_path = libass_path
        self.warning_callback = warning_callback

        self.api: ctypes.CDLL | None = None
        self.library: int | None = None
        self.renderer: int | None = None
        self.track: int | None = None
        self._ass_buffer = None
        self._font_buffers: list[ctypes.Array] = []
        self._dll_directory = None
        self._message_callback = None
        self._messages: list[tuple[int, str]] = []
        self.ycbcr_matrix = 0
        self.version = 0
        self.validation = FontValidation((), (), (), ())
        self._last_bitmap: RenderedBitmap | None = None

    def __enter__(self) -> "LibassRenderer":
        self.initialize()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def initialized(self) -> bool:
        return bool(self.api is not None and self.library and self.renderer and self.track)

    def initialize(self) -> None:
        if self.initialized:
            return
        started = perf_counter()
        try:
            self.api, self._dll_directory = _load_libass(self.libass_path)
            self._bind_api()
            self.version = int(self.api.ass_library_version())

            self.library = self.api.ass_library_init()
            if not self.library:
                raise LibassRenderError("ass_library_init returned NULL")
            self._install_message_callback()
            self.api.ass_set_extract_fonts(self.library, 1)

            self.validation = validate_attached_fonts(self.ass_path, self.font_paths)
            self._report_font_validation()
            if self.font_paths:
                self.api.ass_set_fonts_dir(self.library, _path_bytes(self.font_paths[0].parent))
            self._add_fonts()

            self.renderer = self.api.ass_renderer_init(self.library)
            if not self.renderer:
                raise LibassRenderError("ass_renderer_init returned NULL")
            width, height = self.size
            if width <= 0 or height <= 0:
                raise LibassRenderError(f"Invalid render size: {width}x{height}")
            self.api.ass_set_frame_size(self.renderer, width, height)
            self.api.ass_set_storage_size(self.renderer, width, height)
            self.api.ass_set_fonts(self.renderer, None, None, 1, None, 1)

            ass_data = self.ass_path.read_bytes()
            if not ass_data:
                raise LibassRenderError(f"ASS track is empty: {self.ass_path}")
            self._ass_buffer = ctypes.create_string_buffer(ass_data)
            self.track = self.api.ass_read_memory(
                self.library,
                ctypes.cast(self._ass_buffer, ctypes.c_void_p),
                len(ass_data),
                b"UTF-8",
            )
            if not self.track:
                raise LibassRenderError(f"ass_read_memory returned NULL for {self.ass_path}")
            header = ctypes.cast(self.track, ctypes.POINTER(ASSTrackHeader)).contents
            self.ycbcr_matrix = int(header.YCbCrMatrix)
            self._raise_new_libass_errors(0, operation="initialization")
        except BaseException:
            self.close()
            raise
        finally:
            metrics = current_metrics()
            if metrics:
                metrics.add_time("libass_init", perf_counter() - started)
                metrics.inc("libass_initializations")

    def reinitialize(self) -> None:
        self.close()
        metrics = current_metrics()
        if metrics:
            metrics.inc("libass_reinitializations")
        self.initialize()

    def close(self) -> None:
        api = self.api
        if api is not None:
            if self.track:
                api.ass_free_track(self.track)
            if self.renderer:
                api.ass_renderer_done(self.renderer)
            if self.library:
                api.ass_library_done(self.library)
        self.track = None
        self.renderer = None
        self.library = None
        self.api = None
        self._ass_buffer = None
        self._font_buffers.clear()
        self._message_callback = None
        self._messages.clear()
        self._last_bitmap = None
        if self._dll_directory is not None:
            self._dll_directory.close()
            self._dll_directory = None

    def render(self, timestamp_ms: int, *, expect_content: bool = False) -> RenderedBitmap:
        if timestamp_ms < 0:
            raise LibassRenderError(f"Invalid negative render timestamp: {timestamp_ms} ms")
        if not self.initialized or self.api is None:
            raise LibassRenderError("LibassRenderer is not initialized")

        error_cursor = len(self._messages)
        change = ctypes.c_int(0)
        render_started = perf_counter()
        try:
            images = self.api.ass_render_frame(
                self.renderer,
                self.track,
                int(timestamp_ms),
                ctypes.byref(change),
            )
        except (OSError, ValueError, ctypes.ArgumentError) as exc:
            raise LibassRenderError(f"ass_render_frame failed at {timestamp_ms} ms: {exc}") from exc
        render_s = perf_counter() - render_started
        self._raise_new_libass_errors(error_cursor, operation=f"render at {timestamp_ms} ms")

        if change.value == 0 and self._last_bitmap is not None:
            previous = self._last_bitmap
            if expect_content and previous.image is None:
                raise LibassRenderError(
                    f"ass_render_frame returned no visible images at {timestamp_ms} ms while content was expected"
                )
            result = RenderedBitmap(
                x=previous.x,
                y=previous.y,
                image=previous.image,
                layer_count=previous.layer_count,
                change=0,
                render_s=render_s,
                compose_s=0.0,
                bbox_s=0.0,
                crop_s=0.0,
            )
            self._record_metrics(result)
            return result

        compose_started = perf_counter()
        try:
            x, y, image, layer_count, bbox_s, crop_s = compose_ass_images_cropped(
                images,
                self.size,
                limited_range=_uses_limited_range(self.ycbcr_matrix),
            )
        except (ValueError, OSError, ctypes.ArgumentError) as exc:
            raise LibassRenderError(f"Invalid ASS_Image list at {timestamp_ms} ms: {exc}") from exc
        compose_s = perf_counter() - compose_started
        if expect_content and image is None:
            raise LibassRenderError(
                f"ass_render_frame returned no visible images at {timestamp_ms} ms while content was expected"
            )

        result = RenderedBitmap(
            x=x,
            y=y,
            image=image,
            layer_count=layer_count,
            change=change.value,
            render_s=render_s,
            compose_s=compose_s,
            bbox_s=bbox_s,
            crop_s=crop_s,
        )
        self._last_bitmap = result
        self._record_metrics(result)
        return result

    def _record_metrics(self, result: RenderedBitmap) -> None:
        metrics = current_metrics()
        if metrics:
            metrics.inc("libass_render_calls")
            metrics.inc("frames_rendered")
            metrics.inc("frames_used")
            metrics.add_time("libass_render", result.render_s)
            metrics.add_time("compose_rgba", result.compose_s)
            metrics.add_time("bbox", result.bbox_s)
            metrics.add_time("crop", result.crop_s)

    def _bind_api(self) -> None:
        if self.api is None:
            raise LibassRenderError("libass DLL is not loaded")
        api = self.api
        api.ass_library_version.argtypes = []
        api.ass_library_version.restype = ctypes.c_int
        api.ass_library_init.argtypes = []
        api.ass_library_init.restype = ctypes.c_void_p
        api.ass_library_done.argtypes = [ctypes.c_void_p]
        api.ass_library_done.restype = None
        api.ass_set_message_cb.argtypes = [ctypes.c_void_p, _ASS_MESSAGE_CALLBACK, ctypes.c_void_p]
        api.ass_set_message_cb.restype = None
        api.ass_set_fonts_dir.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        api.ass_set_fonts_dir.restype = None
        api.ass_set_extract_fonts.argtypes = [ctypes.c_void_p, ctypes.c_int]
        api.ass_set_extract_fonts.restype = None
        api.ass_add_font.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_int]
        api.ass_add_font.restype = None
        api.ass_renderer_init.argtypes = [ctypes.c_void_p]
        api.ass_renderer_init.restype = ctypes.c_void_p
        api.ass_renderer_done.argtypes = [ctypes.c_void_p]
        api.ass_renderer_done.restype = None
        api.ass_set_frame_size.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        api.ass_set_frame_size.restype = None
        api.ass_set_storage_size.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        api.ass_set_storage_size.restype = None
        api.ass_set_fonts.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        api.ass_set_fonts.restype = None
        api.ass_read_memory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p]
        api.ass_read_memory.restype = ctypes.c_void_p
        api.ass_free_track.argtypes = [ctypes.c_void_p]
        api.ass_free_track.restype = None
        api.ass_render_frame.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_longlong,
            ctypes.POINTER(ctypes.c_int),
        ]
        api.ass_render_frame.restype = ASSImagePointer

    def _install_message_callback(self) -> None:
        if self.api is None or not self.library:
            raise LibassRenderError("Cannot install libass callback before library initialization")

        def receive(level: int, fmt: bytes | None, _args, _data) -> None:
            try:
                message = (fmt or b"").decode("utf-8", errors="replace").strip()
                self._messages.append((int(level), message))
            except BaseException:
                return

        self._message_callback = _ASS_MESSAGE_CALLBACK(receive)
        self.api.ass_set_message_cb(self.library, self._message_callback, None)

    def _add_fonts(self) -> None:
        if self.api is None or not self.library:
            raise LibassRenderError("Cannot add fonts before library initialization")
        for path in self.font_paths:
            try:
                data = path.read_bytes()
            except OSError as exc:
                self._warn(f"Could not read attached font {path.name}: {exc}")
                continue
            if not data:
                self._warn(f"Attached font is empty and was ignored: {path.name}")
                continue
            buffer = ctypes.create_string_buffer(data)
            self._font_buffers.append(buffer)
            self.api.ass_add_font(
                self.library,
                os.fsencode(path.name),
                ctypes.cast(buffer, ctypes.c_void_p),
                len(data),
            )

    def _raise_new_libass_errors(self, cursor: int, *, operation: str) -> None:
        errors = [message for level, message in self._messages[cursor:] if level <= 1]
        if errors:
            detail = "; ".join(errors[-3:])
            raise LibassRenderError(f"libass reported an error during {operation}: {detail}")

    def _report_font_validation(self) -> None:
        validation = self.validation
        if validation.unreadable_attachments:
            names = ", ".join(path.name for path in validation.unreadable_attachments)
            self._warn(f"Could not inspect attached font file(s): {names}")
        if validation.missing_from_attachments:
            names = ", ".join(validation.missing_from_attachments)
            self._warn(
                "ASS font(s) not present in this MKV's attachments: "
                f"{names}. libass will use the system font provider or a fallback."
            )

    def _warn(self, message: str) -> None:
        if self.warning_callback:
            self.warning_callback(message)


def compose_ass_images_cropped(
    images: ASSImagePointer,
    size: tuple[int, int],
    *,
    limited_range: bool = False,
) -> tuple[int, int, Image.Image | None, int, float, float]:
    frame_width, frame_height = size
    bbox_started = perf_counter()
    layers: list[ASSImage] = []
    bounds: list[tuple[int, int, int, int]] = []
    current = images
    visited = 0
    while current:
        visited += 1
        if visited > 100_000:
            raise ValueError("ASS_Image list appears cyclic or unreasonably large")
        layer = current.contents
        if layer.w < 0 or layer.h < 0 or layer.stride < layer.w:
            raise ValueError(
                f"Invalid ASS_Image dimensions: {layer.w}x{layer.h}, stride={layer.stride}"
            )
        if layer.w and layer.h and layer.bitmap and (layer.color & 0xFF) < 255:
            left = max(0, layer.dst_x)
            top = max(0, layer.dst_y)
            right = min(frame_width, layer.dst_x + layer.w)
            bottom = min(frame_height, layer.dst_y + layer.h)
            if right > left and bottom > top:
                layers.append(layer)
                bounds.append((left, top, right, bottom))
        current = layer.next
    bbox_s = perf_counter() - bbox_started
    if not bounds:
        return 0, 0, None, 0, bbox_s, 0.0

    union_left = min(item[0] for item in bounds)
    union_top = min(item[1] for item in bounds)
    union_right = max(item[2] for item in bounds)
    union_bottom = max(item[3] for item in bounds)
    image = Image.new("RGBA", (union_right - union_left, union_bottom - union_top))
    for layer in layers:
        color = int(layer.color)
        red = (color >> 24) & 0xFF
        green = (color >> 16) & 0xFF
        blue = (color >> 8) & 0xFF
        if limited_range:
            red, green, blue = (
                round(16 + channel * 219 / 255)
                for channel in (red, green, blue)
            )
        mask = _copy_bitmap_mask(layer)
        transparency = color & 0xFF
        if transparency:
            opacity = 255 - transparency
            mask = mask.point([round(coverage * opacity / 255) for coverage in range(256)])
        source = Image.new("RGBA", (layer.w, layer.h), (red, green, blue, 0))
        source.putalpha(mask)
        image.alpha_composite(
            source,
            (layer.dst_x - union_left, layer.dst_y - union_top),
        )

    crop_started = perf_counter()
    visible_bbox = image.getchannel("A").getbbox()
    if visible_bbox is None:
        return 0, 0, None, len(layers), bbox_s, perf_counter() - crop_started
    image = image.crop(visible_bbox)
    crop_s = perf_counter() - crop_started
    return (
        union_left + visible_bbox[0],
        union_top + visible_bbox[1],
        image,
        len(layers),
        bbox_s,
        crop_s,
    )


def _copy_bitmap_mask(layer: ASSImage) -> Image.Image:
    base = ctypes.cast(layer.bitmap, ctypes.c_void_p).value
    if base is None:
        raise ValueError("ASS_Image has a NULL bitmap")
    byte_count = layer.stride * (layer.h - 1) + layer.w
    raw = ctypes.string_at(base, byte_count)
    return Image.frombytes("L", (layer.w, layer.h), raw, "raw", "L", layer.stride)


def validate_attached_fonts(ass_path: Path, font_paths: Iterable[Path]) -> FontValidation:
    required = _required_font_names(ass_path)
    available: set[str] = set()
    unreadable: list[Path] = []
    for path in font_paths:
        path = Path(path)
        try:
            available.update(_font_names(path))
        except (OSError, ValueError, TypeError):
            unreadable.append(path)

    normalized_available = {_normalize_font_name(name) for name in available}
    missing = tuple(
        name for name in required if _normalize_font_name(name) not in normalized_available
    )
    return FontValidation(
        required=tuple(required),
        available=tuple(sorted(available, key=str.casefold)),
        missing_from_attachments=missing,
        unreadable_attachments=tuple(unreadable),
    )


def _required_font_names(ass_path: Path) -> list[str]:
    subs = pysubs2.load(str(ass_path), format_="ass")
    names: set[str] = set()
    for event in subs.events:
        if event.is_comment or not event.text.strip() or event.end <= event.start:
            continue
        style = subs.styles.get(event.style)
        if style and style.fontname.strip():
            names.add(style.fontname.strip())
        for reset_style in _STYLE_RESET_RE.findall(event.text):
            reset = subs.styles.get(reset_style.strip())
            if reset and reset.fontname.strip():
                names.add(reset.fontname.strip())
        for override in _FONT_OVERRIDE_RE.findall(event.text):
            name = override.strip()
            if name:
                names.add(name)
    return sorted(names, key=str.casefold)


def _font_names(path: Path) -> set[str]:
    from fontTools.ttLib import TTCollection, TTFont

    fonts = []
    collection = None
    try:
        if path.suffix.lower() == ".ttc":
            collection = TTCollection(str(path), lazy=True)
            fonts = collection.fonts
        else:
            fonts = [TTFont(str(path), lazy=True)]
        names: set[str] = set()
        for font in fonts:
            name_table = font.get("name")
            if name_table is None:
                continue
            for record in name_table.names:
                if record.nameID not in {1, 2, 4, 6, 16, 17}:
                    continue
                try:
                    value = record.toUnicode().strip()
                except (UnicodeDecodeError, AttributeError):
                    continue
                if value:
                    names.add(value)
        return names
    finally:
        for font in fonts:
            font.close()
        if collection is not None:
            collection.close()


def _load_libass(libass_path: Path | str | None) -> tuple[ctypes.CDLL, object | None]:
    candidates = _libass_candidates(
        libass_path,
        env_path=os.environ.get("LIBASS_PATH"),
        discovered=ctypes.util.find_library("ass"),
        os_name=os.name,
        platform_name=sys.platform,
    )

    errors: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        dll_directory = None
        path = Path(candidate)
        try:
            if os.name == "nt" and path.exists() and hasattr(os, "add_dll_directory"):
                dll_directory = os.add_dll_directory(str(path.resolve().parent))
            return ctypes.CDLL(candidate), dll_directory
        except OSError as exc:
            if dll_directory is not None:
                dll_directory.close()
            errors.append(f"{candidate}: {exc}")
    attempted = ", ".join(seen)
    detail = "; ".join(errors[-3:]) or "no loadable candidates"
    raise LibassRenderError(
        f"Could not load libass. {_libass_install_hint(os.name, sys.platform)} "
        f"Tried: {attempted}. Last errors: {detail}"
    )


def _libass_candidates(
    configured_path: Path | str | None,
    *,
    env_path: str | None,
    discovered: str | None,
    os_name: str,
    platform_name: str,
) -> list[str]:
    candidates: list[str] = []
    if configured_path is not None:
        candidates.append(str(configured_path))
    if env_path:
        candidates.append(env_path)
    if os_name == "nt" and DEFAULT_WINDOWS_LIBASS.exists():
        candidates.append(str(DEFAULT_WINDOWS_LIBASS))
    if discovered:
        candidates.append(discovered)

    if os_name == "nt":
        candidates.extend(["libass-9.dll", "libass.dll"])
    elif platform_name == "darwin":
        candidates.extend(["libass.9.dylib", "libass.dylib"])
    else:
        candidates.extend(["libass.so.9", "libass.so"])
    return list(dict.fromkeys(candidates))


def _libass_install_hint(os_name: str, platform_name: str) -> str:
    if os_name == "nt":
        return (
            "Install MSYS2 UCRT64 libass with "
            "'pacman -S mingw-w64-ucrt-x86_64-libass', or set libass_path in "
            "config.toml / LIBASS_PATH to libass-9.dll."
        )
    if platform_name.startswith("linux"):
        return (
            "On Ubuntu/Debian install it with 'sudo apt install libass9', or set "
            "libass_path in config.toml / LIBASS_PATH to libass.so.9."
        )
    if platform_name == "darwin":
        return "Install libass (for example with Homebrew), or set LIBASS_PATH."
    return "Install libass for this system, or set libass_path in config.toml / LIBASS_PATH."


def _uses_limited_range(ycbcr_matrix: int) -> bool:
    return ycbcr_matrix in {0, 1, 3, 5, 7, 9}


def _normalize_font_name(value: str) -> str:
    value = value.lstrip("@").casefold()
    return "".join(character for character in value if character.isalnum())


def _path_bytes(path: Path) -> bytes:
    return os.fsencode(str(path.resolve()))


_FONT_OVERRIDE_RE = re.compile(r"\\fn([^\\}\r\n]*)", re.IGNORECASE)
_STYLE_RESET_RE = re.compile(r"\\r([^\\}\r\n]+)", re.IGNORECASE)
