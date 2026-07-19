from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import os
from pathlib import Path
from threading import Event
from typing import Iterable

from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn

from .config import AppConfig
from .mkv import TrackSelector
from .pipeline import ConversionResult, convert_mkv


@dataclass(frozen=True)
class LibraryResult:
    files_total: int
    files_converted: int
    files_skipped: int
    files_failed: int
    results: list[ConversionResult]
    failures: list[tuple[Path, str]]
    skips: list[tuple[Path, str]]

    @property
    def files_done(self) -> int:
        return self.files_converted + self.files_skipped


def find_mkvs(root: Path, *, exclude_roots: Iterable[Path] = ()) -> list[Path]:
    root = root.resolve()
    excluded = tuple(path.resolve() for path in exclude_roots)
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.casefold() == ".mkv"
        and not any(path.resolve().is_relative_to(exclude) for exclude in excluded)
    )


def convert_library(
    root: Path,
    *,
    config: AppConfig,
    force: bool = False,
    resume: bool = False,
    track_selector: TrackSelector | None = None,
    in_place: bool = False,
    output_dir: Path | None = None,
) -> LibraryResult:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"Library root is not a directory: {root}")
    if in_place == (output_dir is not None):
        raise ValueError("Choose exactly one library output mode: in_place=True or output_dir=...")

    resolved_output = output_dir.resolve() if output_dir else None
    if resolved_output == root:
        raise ValueError("output_dir must be different from the library root; use in_place=True explicitly instead.")
    excluded = (resolved_output,) if resolved_output and resolved_output.is_relative_to(root) else ()
    files = find_mkvs(root, exclude_roots=excluded)
    targets = {
        path: path if in_place else resolved_output / path.relative_to(root)
        for path in files
    }
    state = _load_state(config.state_file) if resume else {"files": {}}
    pending = []
    skips: list[tuple[Path, str]] = []
    for index, path in enumerate(files, start=1):
        if not force and resume and _is_completed(
            state,
            path,
            output_path=targets[path],
            track_selector=track_selector,
        ):
            skips.append((path, "completed in resume state"))
        else:
            pending.append((index, path))

    results: list[ConversionResult] = []
    failures: list[tuple[Path, str]] = []
    converted = 0
    failed = 0
    stop_event = Event()
    file_indexes = {path: index for index, path in enumerate(files, start=1)}

    config.state_file.parent.mkdir(parents=True, exist_ok=True)
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task("convert-library", total=len(files))
        for path, reason in skips:
            index = file_indexes[path]
            progress.console.print(f"[{index}/{len(files)}] SKIP {path} ({reason})")
            progress.advance(task)

        executor = ThreadPoolExecutor(max_workers=max(1, config.workers))
        futures = {}
        try:
            futures = {
                executor.submit(
                    convert_mkv,
                    path,
                    config=config,
                    force=force,
                    track_selector=track_selector,
                    output_path=None if in_place else targets[path],
                    cancel_requested=stop_event.is_set,
                ): (index, path)
                for index, path in pending
            }
            for future in as_completed(futures):
                index, path = futures[future]
                target = targets[path]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    failures.append((path, str(exc)))
                    progress.console.print(f"[{index}/{len(files)}] ERROR {path} ({exc})")
                    state["files"][_path_key(path)] = _state_entry(
                        path,
                        target,
                        status="failed",
                        track_selector=track_selector,
                        detail=str(exc),
                    )
                else:
                    results.append(result)
                    status, reason = _result_status(result)
                    if status == "converted":
                        converted += 1
                        progress.console.print(f"[{index}/{len(files)}] CONVERTED {path}")
                    else:
                        skips.append((path, reason))
                        progress.console.print(f"[{index}/{len(files)}] SKIP {path} ({reason})")
                    state["files"][_path_key(path)] = _state_entry(
                        path,
                        target,
                        status="complete",
                        track_selector=track_selector,
                        detail=reason,
                    )
                _save_state(config.state_file, state)
                progress.advance(task)
        except KeyboardInterrupt:
            stop_event.set()
            for future in futures:
                future.cancel()
            progress.console.print("Cancellation requested; waiting for active files to stop safely...")
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    return LibraryResult(len(files), converted, len(skips), failed, results, failures, skips)


def resume_library(
    root: Path,
    *,
    config: AppConfig,
    force: bool = False,
    track_selector: TrackSelector | None = None,
    in_place: bool = False,
    output_dir: Path | None = None,
) -> LibraryResult:
    return convert_library(
        root,
        config=config,
        force=force,
        resume=True,
        track_selector=track_selector,
        in_place=in_place,
        output_dir=output_dir,
    )


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"files": {}}
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    with tmp.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, sort_keys=True)
    tmp.replace(path)


def _is_completed(
    state: dict,
    path: Path,
    *,
    output_path: Path,
    track_selector: TrackSelector | None,
) -> bool:
    item = state.get("files", {}).get(_path_key(path))
    if not item or item.get("status") != "complete":
        return False
    return (
        item.get("size") == path.stat().st_size
        and item.get("mtime_ns") == path.stat().st_mtime_ns
        and item.get("output_path") == _path_key(output_path)
        and item.get("output_fingerprint") == _optional_fingerprint(output_path)
        and item.get("track_selector") == _selector_state(track_selector)
    )


def _fingerprint(path: Path) -> dict:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _optional_fingerprint(path: Path) -> dict | None:
    return _fingerprint(path) if path.exists() else None


def _state_entry(
    source_path: Path,
    output_path: Path,
    *,
    status: str,
    track_selector: TrackSelector | None,
    detail: str,
) -> dict:
    return {
        "status": status,
        "detail": detail,
        "track_selector": _selector_state(track_selector),
        "output_path": _path_key(output_path),
        "output_fingerprint": _optional_fingerprint(output_path),
        **_fingerprint(source_path),
    }


def _result_status(result: ConversionResult) -> tuple[str, str]:
    if result.changed:
        return "converted", ""
    if not result.tracks:
        return "skipped", "no ASS tracks"
    reasons = tuple(dict.fromkeys(track.reason or "no PGS written" for track in result.tracks))
    return "skipped", "; ".join(reasons)


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _selector_state(selector: TrackSelector | None) -> dict:
    selector = selector or TrackSelector()
    return {
        "indexes": sorted(selector.indexes) if selector.indexes is not None else None,
        "name": selector.name,
        "language": selector.language,
    }
