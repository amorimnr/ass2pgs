from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
from typing import Iterator


_CURRENT_METRICS: ContextVar["Metrics | None"] = ContextVar("jellyfin_ass2pgs_metrics", default=None)


@dataclass
class GroupMetrics:
    index: int
    kind: str
    start_ms: int
    end_ms: int
    samples: int
    track_id: int | None = None
    frames_rendered: int = 0
    frames_used: int = 0
    group_wall_time_s: float = 0.0
    time_to_first_frame_s: float = 0.0
    python_processing_time_s: float = 0.0
    estimated_frames: int = 0
    libass_render_time_s: float = 0.0
    compose_rgba_time_s: float = 0.0
    hashing_time_s: float = 0.0

    @property
    def window_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)

    @property
    def frames_discarded(self) -> int:
        return max(0, self.frames_rendered - self.frames_used)

    @property
    def frames_per_sample(self) -> float:
        if self.samples <= 0:
            return 0.0
        return self.frames_rendered / self.samples

    @property
    def total_s(self) -> float:
        return self.group_wall_time_s

    @total_s.setter
    def total_s(self, value: float) -> None:
        self.group_wall_time_s = value

    @property
    def first_frame_latency_s(self) -> float:
        return self.time_to_first_frame_s

    @first_frame_latency_s.setter
    def first_frame_latency_s(self, value: float) -> None:
        self.time_to_first_frame_s = value

@dataclass
class Metrics:
    timings: dict[str, float] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    groups: list[GroupMetrics] = field(default_factory=list)
    track_counters: dict[int, dict[str, int]] = field(default_factory=dict)

    @contextmanager
    def time(self, key: str) -> Iterator[None]:
        start = perf_counter()
        try:
            yield
        finally:
            self.add_time(key, perf_counter() - start)

    def add_time(self, key: str, seconds: float) -> None:
        self.timings[key] = self.timings.get(key, 0.0) + seconds

    def inc(self, key: str, amount: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + amount

    def set_counter(self, key: str, value: int) -> None:
        self.counters[key] = value

    def count(self, key: str) -> int:
        return self.counters.get(key, 0)

    def seconds(self, key: str) -> float:
        return self.timings.get(key, 0.0)

    def add_group(self, group: GroupMetrics) -> None:
        self.groups.append(group)

    def add_track_counters(self, track_id: int, counters: dict[str, int]) -> None:
        current = self.track_counters.setdefault(track_id, {})
        for key, value in counters.items():
            current[key] = current.get(key, 0) + value
            self.inc(key, value)


@contextmanager
def use_metrics(metrics: Metrics) -> Iterator[Metrics]:
    token = _CURRENT_METRICS.set(metrics)
    try:
        yield metrics
    finally:
        _CURRENT_METRICS.reset(token)


def current_metrics() -> Metrics | None:
    return _CURRENT_METRICS.get()


def increment_process_counter(tool_name: str) -> None:
    metrics = current_metrics()
    if metrics is None:
        return
    name = tool_name.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if name in {"ffmpeg", "mkvextract", "mkvmerge", "mkvmerge_probe", "mkvmerge_mux"}:
        metrics.inc(f"{name}_processes")


def format_report(metrics: Metrics) -> str:
    sep = "-" * 30
    lines = [sep]
    for label, key in [
        ("Tempo total", "total"),
        ("Leitura MKV", "read_mkv"),
        ("Extracao ASS", "extract_ass"),
        ("Extracao fontes", "extract_fonts"),
        ("Analise ASS", "analyze_ass"),
        ("Classificacao", "classify_events"),
        ("Timeline", "build_timeline"),
        ("RenderPlan", "build_render_plan"),
        ("Group wall time", "group_wall_time"),
        ("Inicializacao libass", "libass_init"),
        ("Render libass", "libass_render"),
        ("Composicao RGBA", "compose_rgba"),
        ("Primeiro frame", "time_to_first_frame"),
        ("Processamento Python", "python_processing_time"),
        ("Reconstrucao RGBA", "reconstruct_rgba"),
        ("Deteccao mudancas", "change_detection"),
        ("Bounding box", "bbox"),
        ("Crop", "crop"),
        ("Hashing", "hashing"),
        ("Quantizacao", "quantization"),
        ("RLE", "rle"),
        ("SUP", "sup_write"),
        ("Mux", "mux_mkv"),
    ]:
        lines.append(f"{label + ' ':.<28}{_fmt_seconds(metrics.seconds(key))}")

    lines.append(sep)
    for label, key in [
        ("Eventos", "events"),
        ("Intervalos", "intervals"),
        ("STATIC", "static_intervals"),
        ("DYNAMIC", "dynamic_intervals"),
        ("Static Groups", "static_groups"),
        ("Dynamic Groups", "dynamic_groups"),
        ("Renderizacoes estaticas", "static_renders"),
        ("Streams dinamicos", "dynamic_streams"),
        ("Frames renderizados", "frames_rendered"),
        ("Frames lidos", "frames_read"),
        ("Frames utilizados", "frames_used"),
        ("Bitmaps diferentes", "bitmaps_different"),
        ("Objetos PGS", "pgs_objects"),
        ("Renders diretos esperados", "expected_render_calls"),
        ("Renders diretos", "libass_render_calls"),
        ("Inicializacoes libass", "libass_initializations"),
        ("Reinicializacoes libass", "libass_reinitializations"),
        ("ffmpeg esperado", "expected_ffmpeg_processes"),
        ("ffmpeg executados", "ffmpeg_processes"),
        ("Tentativas de grupo falhas", "failed_group_attempts"),
        ("Retries de grupo", "group_retries"),
        ("mkvextract executados", "mkvextract_processes"),
        ("mkvmerge probe", "mkvmerge_probe_processes"),
        ("mkvmerge mux", "mkvmerge_mux_processes"),
    ]:
        lines.append(f"{label + ' ':.<28}{metrics.count(key)}")

    lines.append(sep)
    lines.append(f"{'Media grupo estatico ':.<28}{_group_avg(metrics, 'static')}")
    lines.append(f"{'Media grupo dinamico ':.<28}{_group_avg(metrics, 'dynamic')}")
    lines.append(f"{'Media por frame ':.<28}{_avg(metrics.seconds('python_processing_time'), metrics.count('frames_used'))}")
    lines.append(f"{'Media por objeto PGS ':.<28}{_avg(metrics.seconds('pgs_object_total'), metrics.count('pgs_objects'))}")
    lines.append(f"{'Descarte de frames ':.<28}{_discard_percent(metrics)}")
    lines.append(sep)
    if metrics.track_counters:
        lines.extend(_format_track_summary(metrics))
        lines.append(sep)
    if metrics.groups:
        lines.append("Render Groups")
        for group in metrics.groups:
            lines.extend(_format_group(group))
        lines.append(sep)
    return "\n".join(lines)


def _fmt_seconds(value: float) -> str:
    return f"{value:.3f}s"


def _avg(total_seconds: float, count: int) -> str:
    if count <= 0:
        return "n/a"
    return f"{(total_seconds / count):.6f}s"


def _group_avg(metrics: Metrics, kind: str) -> str:
    groups = [group for group in metrics.groups if group.kind == kind]
    if not groups:
        return "n/a"
    return f"{sum(group.group_wall_time_s for group in groups) / len(groups):.6f}s"


def _discard_percent(metrics: Metrics) -> str:
    read = metrics.count("frames_read")
    used = metrics.count("frames_used")
    if read <= 0:
        return "n/a"
    discarded = max(0, read - used)
    return f"{discarded / read * 100:.2f}%"


def _format_track_summary(metrics: Metrics) -> list[str]:
    lines = ["Tracks planned vs executed"]
    for track_id in sorted(metrics.track_counters):
        counters = metrics.track_counters[track_id]
        groups = [group for group in metrics.groups if group.track_id == track_id]
        static_groups = [group for group in groups if group.kind == "static"]
        dynamic_groups = [group for group in groups if group.kind == "dynamic"]
        lines.append("")
        lines.append(f"Track {track_id}")
        lines.append(
            "planned: "
            f"events={counters.get('events', 0)} "
            f"intervals={counters.get('intervals', 0)} "
            f"static_groups={counters.get('static_groups', 0)} "
            f"dynamic_groups={counters.get('dynamic_groups', 0)} "
            f"direct_renders={counters.get('expected_render_calls', 0)} "
            f"ffmpeg={counters.get('expected_ffmpeg_processes', 0)}"
        )
        lines.append(
            "executed: "
            f"groups={len(groups)} "
            f"static={len(static_groups)} "
            f"dynamic={len(dynamic_groups)} "
            f"frames={sum(group.frames_rendered for group in groups)} "
            f"used={sum(group.frames_used for group in groups)} "
            f"pgs={counters.get('executed_pgs_objects', 0)} "
            f"actual={sum(group.group_wall_time_s for group in groups):.3f}s"
        )

    total_wall = sum(group.group_wall_time_s for group in metrics.groups)
    lines.append("")
    lines.append(
        "Total planned: "
        f"groups={metrics.count('static_groups') + metrics.count('dynamic_groups')} "
        f"static={metrics.count('static_groups')} "
        f"dynamic={metrics.count('dynamic_groups')} "
        f"direct_renders={metrics.count('expected_render_calls')} "
        f"ffmpeg={metrics.count('expected_ffmpeg_processes')}"
    )
    lines.append(
        "Total executed: "
        f"groups={len(metrics.groups)} "
        f"frames={metrics.count('frames_rendered')} "
        f"used={metrics.count('frames_used')} "
        f"pgs={metrics.count('executed_pgs_objects')} "
        f"actual={total_wall:.3f}s"
    )
    return lines


def _format_group(group: GroupMetrics) -> list[str]:
    prefix = f"Track {group.track_id} " if group.track_id is not None else ""
    title = f"{prefix}{group.kind.title()} Group #{group.index}"
    return [
        "",
        title,
        f"window......................{group.window_ms / 1000:.3f}s",
        f"samples.....................{group.samples}",
        f"frames rendered............{group.frames_rendered}",
        f"frames used................{group.frames_used}",
        f"frames discarded...........{group.frames_discarded}",
        f"frames/sample..............{group.frames_per_sample:.2f}",
        f"actual cost................{group.group_wall_time_s:.3f}s",
        f"estimated frames...........{group.estimated_frames}",
        f"group wall time............{group.group_wall_time_s:.3f}s",
        f"time to first frame........{group.time_to_first_frame_s * 1000:.1f}ms",
        f"python processing..........{group.python_processing_time_s:.3f}s",
        f"libass render..............{group.libass_render_time_s:.3f}s",
        f"compose RGBA...............{group.compose_rgba_time_s:.3f}s",
        f"hashing....................{group.hashing_time_s:.3f}s",
    ]
