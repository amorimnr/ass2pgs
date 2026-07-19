from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

from .metrics import increment_process_counter


DEFAULT_WINDOWS_PATHS = {
    "ffmpeg": [
        Path.home()
        / "AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe",
    ],
    "ffprobe": [
        Path.home()
        / "AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe",
    ],
    "mkvmerge": [Path("C:/Program Files/MKVToolNix/mkvmerge.exe")],
    "mkvextract": [Path("C:/Program Files/MKVToolNix/mkvextract.exe")],
}


def find_tool(name: str) -> str:
    env_name = name.upper()
    if override := os.environ.get(env_name):
        return override

    if found := shutil.which(name):
        return found

    for candidate in DEFAULT_WINDOWS_PATHS.get(name, []):
        if candidate.is_file():
            return str(candidate)
        if candidate.is_dir():
            matches = list(candidate.rglob(f"{name}.exe"))
            if matches:
                return str(matches[0])

    raise RuntimeError(
        f"Could not find {name!r}. Install it or set the {env_name} environment variable."
    )


def run(
    args: Iterable[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    process_metric: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [str(arg) for arg in args]
    increment_process_counter(process_metric or Path(command[0]).name)
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        detail = stderr or stdout or "no output"
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(command)}\n{detail}")
    return result
