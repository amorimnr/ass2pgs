# Ubuntu Server setup

These steps target Ubuntu Server 24.04, whose standard repositories provide
Python 3.12 and the required libass runtime.

## Install system packages

```bash
sudo apt update
sudo apt install -y git python3 python3-venv ffmpeg mkvtoolnix libass9 tmux
```

Confirm the installation:

```bash
python3 --version
mkvmerge --version
mkvextract --version
ldconfig -p | grep libass
```

Python must be 3.12 or newer. The libass check should show `libass.so.9`.

Confirm that Python can discover the same library through `ctypes`:

```bash
python3 -c "import ctypes.util; print(ctypes.util.find_library('ass'))"
```

The expected result is `libass.so.9` or another non-empty libass soname. If it
prints `None`, reinstall `libass9` or set `LIBASS_PATH` to the full `.so` path.

## Clone and install

Keep application code outside the Jellyfin media library:

```bash
cd /opt
sudo git clone https://github.com/amorimnr/ass2pgs.git jellyfin-ass2pgs
sudo chown -R "$USER":"$USER" /opt/jellyfin-ass2pgs
cd /opt/jellyfin-ass2pgs

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
ass2pgs --help
```

For development, replace `python -m pip install .` with
`python -m pip install -e ".[test]"`.

To reproduce the automated Linux validation after a development install:

```bash
ass2pgs --help
python -m jellyfin_ass2pgs --help
python -m pytest -q
```

## Configure

```bash
cp config.example.toml config.toml
nano config.toml
```

A conservative starting point is:

```toml
workers = 2
keep_temp = false
overwrite = false
font_cache = ".cache/fonts"
work_dir = ".cache/work"
state_file = ".cache/jellyfin-ass2pgs-state.json"
pgs_matrix = "bt709"
dynamic_render_fps = 24.0
retry_failed_groups = 0
```

Increase `workers` only after observing CPU and memory use. Each worker handles
a different MKV and owns a separate libass context.

## Check media permissions

The account running the command needs read access to the library. In-place mode
also needs permission to create and atomically replace files in every media
directory.

```bash
namei -l /mnt/media/jellyfin/Series/Example/Episode01.mkv
ls -l /mnt/media/jellyfin/Series/Example/Episode01.mkv
```

Use a normal administration account with access to the media group. Avoid
running the converter as the `jellyfin` service account unless that is an
intentional part of your permission model.

## Test one episode safely

```bash
source /opt/jellyfin-ass2pgs/.venv/bin/activate
cd /opt/jellyfin-ass2pgs

ass2pgs tracks "/mnt/media/jellyfin/Series/Example/Episode01.mkv"

mkdir -p "$HOME/ass2pgs-test"
ass2pgs convert \
  "/mnt/media/jellyfin/Series/Example/Episode01.mkv" \
  --track-lang eng \
  --output "$HOME/ass2pgs-test/Episode01.mkv"

ass2pgs tracks "$HOME/ass2pgs-test/Episode01.mkv"
```

Play the output before enabling in-place conversion. Generated subtitle titles
end in ` (PGS)`.

## Convert the library

The safest first library run writes to a separate root:

```bash
ass2pgs convert-library \
  /mnt/media/jellyfin \
  --output-dir /mnt/media/jellyfin-pgs \
  --track-lang eng
```

After backups and a successful sample, in-place mode is:

```bash
ass2pgs convert-library \
  /mnt/media/jellyfin \
  --in-place \
  --track-lang eng
```

Run long jobs inside `tmux` so an SSH disconnect does not stop them:

```bash
tmux new -s ass2pgs
cd /opt/jellyfin-ass2pgs
source .venv/bin/activate
ass2pgs convert-library /mnt/media/jellyfin --in-place --track-lang eng
```

Detach with `Ctrl+B`, then `D`. Reattach with:

```bash
tmux attach -t ass2pgs
```

If interrupted, continue with the same selectors and output mode:

```bash
ass2pgs resume /mnt/media/jellyfin --in-place --track-lang eng
```

## Update later

```bash
cd /opt/jellyfin-ass2pgs
source .venv/bin/activate
git pull --ff-only
python -m pip install --upgrade .
```

`python -m jellyfin_ass2pgs` remains available as a fallback if the virtual
environment's `bin` directory is not active.
