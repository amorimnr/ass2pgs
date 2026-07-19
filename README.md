# jellyfin-ass2pgs

`jellyfin-ass2pgs` converts ASS subtitle tracks inside Matroska files to PGS
bitmap tracks while preserving the original video, audio, attachments, and ASS
tracks. Text is never recognized or rewritten: libass renders the complete ASS
composition, and Python encodes that bitmap as PGS.

The project is intended for personal Jellyfin libraries whose clients cannot
render ASS subtitles reliably.

## What it does

- Finds ASS tracks with `mkvmerge -J`.
- Extracts ASS and attached fonts with `mkvextract`.
- Renders static timeline states once and samples genuinely animated states.
- Preserves layers, clipping, blur, overlap, alpha, and ASS transforms through
  libass.
- Crops visible pixels before palette quantization and PGS RLE encoding.
- Adds a new `S_HDMV/PGS` track without transcoding video or audio.
- Preserves language and default/forced flags; generated titles end in ` (PGS)`.
- Skips a selected ASS track when its matching generated PGS already exists.

## Requirements

- Python 3.12 or newer
- MKVToolNix (`mkvmerge` and `mkvextract`)
- libass 0.17 or newer
- FFmpeg, recommended for media inspection and synthetic fixture generation

The production conversion path calls libass directly and does not start an
FFmpeg renderer process.

### Windows

1. Install [Python](https://www.python.org/downloads/),
   [MKVToolNix](https://mkvtoolnix.download/downloads.html), and
   [FFmpeg](https://ffmpeg.org/download.html). Ensure their command-line tools
   are available on `PATH`.
2. Install [MSYS2](https://www.msys2.org/) and open its **UCRT64** terminal.
3. Update MSYS2, reopen the UCRT64 terminal if requested, then install libass:

   ```bash
   pacman -Syu
   pacman -S mingw-w64-ucrt-x86_64-libass
   ```

The package installs `C:\msys64\ucrt64\bin\libass-9.dll`, which the application
checks automatically. The package and its DLL list are documented on the
[official MSYS2 package page](https://packages.msys2.org/packages/mingw-w64-ucrt-x86_64-libass).

For a non-default location, set `libass_path` in `config.toml` or the
`LIBASS_PATH` environment variable. Keep libass dependency DLLs in the same
directory as `libass-9.dll`.

### Ubuntu/Debian

On Ubuntu 24.04:

```bash
sudo apt update
sudo apt install python3 python3-venv ffmpeg mkvtoolnix libass9
```

Package names can differ on other releases. The loader checks `LIBASS_PATH`,
then the system library search path (`libass.so.9` or `libass.so`).

## Installation

```bash
git clone https://github.com/amorimnr/ass2pgs.git
cd jellyfin-ass2pgs
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install .
ass2pgs --help
```

Linux:

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
ass2pgs --help
```

Use `python -m pip install -e ".[test]"` instead when developing locally.

Copy `config.example.toml` to `config.toml` only when overriding defaults.
`config.toml` is intentionally ignored because it can contain machine-specific
paths.

## Usage

List subtitle tracks and their Matroska IDs:

```bash
ass2pgs tracks episode.mkv
```

Convert one file to a separate output:

```bash
ass2pgs convert episode.mkv \
  --track-name "dialogue" --output episode.with-pgs.mkv
```

Track filters may be combined and use AND semantics:

```text
--track-index 4       Matroska track ID shown by `tracks`
--track-index 3,4     Multiple IDs
--track-index all     Every ASS track
--track-name dialogue Case-insensitive name substring
--track-lang eng      IETF or legacy Matroska language tag
```

Without filters, every ASS track is selected. Unselected original tracks remain
in the output.

Convert a library in place, with atomic replacement after a successful mux:

```bash
ass2pgs convert-library /mnt/media/jellyfin --in-place
```

Or keep the source library untouched:

```bash
ass2pgs convert-library /mnt/media/jellyfin \
  --output-dir /mnt/media/jellyfin-pgs --track-lang eng
```

Resume using the same output mode and selectors:

```bash
ass2pgs resume /mnt/media/jellyfin \
  --output-dir /mnt/media/jellyfin-pgs --track-lang eng
```

Use `--force` to replace matching generated PGS tracks. Outputs made by older
builds without the ` (PGS)` suffix can be migrated with one forced run.

`ass2pgs` is the recommended command after installation. The longer
`jellyfin-ass2pgs` console command and `python -m jellyfin_ass2pgs` remain
available for compatibility and expose the same subcommands and flags.

## Safety

- Library in-place conversion requires the explicit `--in-place` flag.
- `mkvmerge` writes a unique temporary file next to the destination; the final
  path is replaced only after muxing succeeds.
- One failed MKV does not stop the rest of a library run.
- Ctrl+C cancels pending work and does not publish partial SUP/MKV output.
- Original ASS tracks are retained.

Keep backups when first running any in-place media workflow.

## Tests

```bash
python -m pip install -e ".[test]"
python -m pytest -q
```

The repository tests use only hand-written synthetic ASS data and generated
byte fixtures. No episode, extracted subtitle, release font, or other
copyrighted media is required.

See [docs/architecture.md](docs/architecture.md) for the conversion flow and
format boundaries. A complete deployment walkthrough is available in
[docs/ubuntu-server.md](docs/ubuntu-server.md).
