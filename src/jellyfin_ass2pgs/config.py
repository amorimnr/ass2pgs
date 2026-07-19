from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class AppConfig:
    workers: int = 2
    keep_temp: bool = False
    overwrite: bool = False
    font_cache: Path = Path(".cache/fonts")
    work_dir: Path = Path(".cache/work")
    state_file: Path = Path(".cache/jellyfin-ass2pgs-state.json")
    pgs_matrix: str = "bt709"
    dynamic_render_fps: float = 24.0
    retry_failed_groups: int = 0
    libass_path: Path | None = None


def load_config(path: Path | None = None) -> AppConfig:
    defaults = AppConfig()
    data = {}
    if path is None:
        candidate = Path("config.toml")
        path = candidate if candidate.exists() else None
    if path is not None and path.exists():
        with path.open("rb") as file:
            data = tomllib.load(file)

    removed_cost_keys = sorted(
        key for key in data
        if key.startswith("cost_model") or key.endswith("_cost_s") or key.endswith("_gap_ms")
    )
    if removed_cost_keys:
        names = ", ".join(removed_cost_keys)
        raise ValueError(
            f"Removed ffmpeg cost-model setting(s) in config.toml: {names}. "
            "Direct libass rendering no longer groups by process cost."
        )

    dynamic_render_fps = float(data.get("dynamic_render_fps", defaults.dynamic_render_fps))
    retry_failed_groups = int(data.get("retry_failed_groups", defaults.retry_failed_groups))
    if dynamic_render_fps <= 0:
        raise ValueError("dynamic_render_fps must be greater than zero.")
    if retry_failed_groups < 0:
        raise ValueError("retry_failed_groups must be zero or greater.")
    raw_libass_path = data.get("libass_path")

    return AppConfig(
        workers=int(data.get("workers", defaults.workers)),
        keep_temp=bool(data.get("keep_temp", defaults.keep_temp)),
        overwrite=bool(data.get("overwrite", defaults.overwrite)),
        font_cache=Path(data.get("font_cache", defaults.font_cache)),
        work_dir=Path(data.get("work_dir", defaults.work_dir)),
        state_file=Path(data.get("state_file", defaults.state_file)),
        pgs_matrix=str(data.get("pgs_matrix", defaults.pgs_matrix)),
        dynamic_render_fps=dynamic_render_fps,
        retry_failed_groups=retry_failed_groups,
        libass_path=Path(raw_libass_path) if raw_libass_path else None,
    )
