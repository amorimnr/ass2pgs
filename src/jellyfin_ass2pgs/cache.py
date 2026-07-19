from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
from pathlib import Path
import shutil
import tempfile
import threading
from time import time

from .tools import find_tool, run


_FONT_LOCK = threading.Lock()
_WORK_LOCK = threading.Lock()
_ACTIVE_WORK_DIRS: set[Path] = set()
WORK_MARKER_NAME = ".jellyfin-ass2pgs-work.json"


@dataclass(frozen=True)
class WorkCleanupReport:
    removed: tuple[Path, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass
class WorkDirectoryLease:
    path: Path
    keep_temp: bool
    cleanup_report: WorkCleanupReport
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        resolved = self.path.resolve()
        with _WORK_LOCK:
            _ACTIVE_WORK_DIRS.discard(resolved)
            if not self.keep_temp and self.path.exists():
                shutil.rmtree(self.path)


def prepare_work_directory(work_parent: Path, *, prefix: str, keep_temp: bool) -> WorkDirectoryLease:
    work_parent = work_parent.resolve()
    work_parent.mkdir(parents=True, exist_ok=True)
    with _WORK_LOCK:
        cleanup_report = _cleanup_orphaned_work_dirs(work_parent)
        path = Path(tempfile.mkdtemp(prefix=prefix, dir=work_parent))
        marker = {
            "pid": os.getpid(),
            "created_at": time(),
            "keep_temp": keep_temp,
        }
        (path / WORK_MARKER_NAME).write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")
        _ACTIVE_WORK_DIRS.add(path.resolve())
    return WorkDirectoryLease(path, keep_temp, cleanup_report)


def cleanup_orphaned_work_dirs(work_parent: Path) -> WorkCleanupReport:
    work_parent = work_parent.resolve()
    work_parent.mkdir(parents=True, exist_ok=True)
    with _WORK_LOCK:
        return _cleanup_orphaned_work_dirs(work_parent)


def _cleanup_orphaned_work_dirs(work_parent: Path) -> WorkCleanupReport:
    removed: list[Path] = []
    warnings: list[str] = []
    for candidate in sorted(path for path in work_parent.iterdir() if path.is_dir()):
        resolved = candidate.resolve()
        if resolved in _ACTIVE_WORK_DIRS:
            continue
        marker_path = candidate / WORK_MARKER_NAME
        if not marker_path.exists():
            warnings.append(f"unmanaged work directory left untouched: {candidate}")
            continue
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            pid = int(marker["pid"])
            keep_temp = bool(marker["keep_temp"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            warnings.append(f"invalid work directory marker left untouched: {candidate}")
            continue
        if keep_temp:
            continue
        if pid != os.getpid() and _pid_is_alive(pid):
            continue
        try:
            shutil.rmtree(candidate)
        except OSError as exc:
            warnings.append(f"could not remove orphaned work directory {candidate}: {exc}")
        else:
            removed.append(candidate)
    return WorkCleanupReport(tuple(removed), tuple(warnings))


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def extract_fonts_cached(info: dict, mkv_path: Path, font_cache: Path) -> tuple[Path, ...]:
    with _FONT_LOCK:
        attachments = []
        cached_paths: list[Path] = []
        font_cache.mkdir(parents=True, exist_ok=True)

        for attachment in info.get("attachments", []):
            content_type = attachment.get("content_type", "").lower()
            file_name = attachment.get("file_name", f"attachment-{attachment['id']}")
            if not (content_type.startswith("font/") or Path(file_name).suffix.lower() in {".ttf", ".otf", ".ttc"}):
                continue

            uid = attachment.get("properties", {}).get("uid", attachment["id"])
            output = font_cache / f"{uid}_{_safe_file_name(file_name)}"
            cached_paths.append(output)
            if not output.exists() or output.stat().st_size != int(attachment.get("size", 0)):
                attachments.append((attachment["id"], output))

        if attachments:
            mkvextract = find_tool("mkvextract")
            args = [mkvextract, "attachments", mkv_path]
            args.extend(f"{attachment_id}:{output_path}" for attachment_id, output_path in attachments)
            run(args)

        return tuple(path for path in cached_paths if _is_font(path))


def _safe_file_name(file_name: str) -> str:
    name = Path(file_name).name
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)


def _is_font(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in {".ttf", ".otf", ".ttc"}
