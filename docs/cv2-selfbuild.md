# Self-built `opencv-python-headless` wheels

SimpleNVR ships a custom build of `opencv-python-headless` with FFmpeg disabled. This page explains why and how to regenerate the per-platform wheels.

## Why

The stock `opencv-python-headless` wheel on PyPI bundles an internal FFmpeg build linked against `libx264` and `libx265` — both **GPL-2.0+**. `cv2.abi3.so` pulls `libavcodec` in as an eager (`LC_LOAD_DYLIB`) dependency, so the OS dynamic linker resolves `libx264`/`libx265` at `import cv2` — before any cv2 code runs. Shipping the stock wheel would infect SimpleNVR's closed-source bundle with GPL.

Because SimpleNVR never calls `cv2.VideoCapture` or `cv2.VideoWriter` — we drive all video I/O through bundled `ffmpeg` as a subprocess, which is LGPL — we simply don't need FFmpeg inside cv2. Rebuilding with `-DWITH_FFMPEG=OFF` drops libavcodec from the wheel entirely. All image-array algorithms we actually use (MOG2 background subtraction, morphology, `matchTemplate`, `resize`, `imencode`, `cvtColor`) are implemented in OpenCV proper and are unaffected.

## How

One wheel per OS/arch, built from the same `opencv-python` tag so runtime behavior matches across platforms.

| Host | Command | Output wheel |
|---|---|---|
| macOS (arm64) | `scripts/build_cv2_wheel.sh` | `vendor/cv2-wheels/opencv_python_headless-*-macosx_*_arm64.whl` |
| Linux (x64 / arm64) | `scripts/build_cv2_wheel.sh` | `vendor/cv2-wheels/opencv_python_headless-*-linux_*.whl` |
| Windows (x64 / arm64) | `scripts\build_cv2_wheel.ps1` | `vendor\cv2-wheels\opencv_python_headless-*-win_*.whl` |

Both scripts:

1. Check prerequisites (cmake, git, repo `.venv`).
2. Clone `opencv-python` at the pinned tag (`OPENCV_PYTHON_TAG` in the script — bump intentionally, regenerate all platforms together).
3. Invoke `pip wheel .` with `ENABLE_HEADLESS=1` and `CMAKE_ARGS="-DWITH_FFMPEG=OFF -DWITH_GSTREAMER=OFF -DWITH_1394=OFF"`.
4. Drop the wheel in `vendor/cv2-wheels/` (gitignored).

Build times: ~30-40 min on M-series mac, ~20-30 min on an 8-core Linux host, ~45-60 min on Windows.

Prerequisite install per platform is in the script header comments.

## Verify

After installing the wheel into the repo `.venv`:

```bash
pip install vendor/cv2-wheels/opencv_python_headless-*.whl --force-reinstall
python -c "import cv2; print(cv2.__file__)"
```

Then inspect the installed cv2 module's native deps — nothing below should print:

```bash
# macOS
otool -L .venv/lib/python3.11/site-packages/cv2/.dylibs/*.dylib 2>/dev/null \
    | grep -iE 'x264|x265|avcodec|avformat'

# Linux
ldd .venv/lib/python3.11/site-packages/cv2/cv2*.so 2>/dev/null \
    | grep -iE 'x264|x265|avcodec|avformat'
```

```powershell
# Windows
dumpbin /dependents .venv\Lib\site-packages\cv2\cv2*.pyd `
    | Select-String -Pattern 'avcodec|x26'
```

If any of those command prints output, the build kept FFmpeg — check the CMake flags and rerun.

## Bundle-time enforcement

`scripts/bundle_python.sh` (macOS / Linux) and `scripts/bundle_python.ps1` (Windows) enforce the LGPL-clean cv2 in two steps:

1. **Auto-install the local wheel.** After `pip install -r backend/requirements.txt` runs (which would otherwise pull stock cv2 from PyPI), each bundler detects the current OS + arch, globs `vendor/cv2-wheels/` for a matching wheel, and `pip install --force-reinstall --no-deps`-es it over whatever pip just installed. If no matching wheel is present, the bundler prints a loud WARNING and continues — you get a fast feedback loop when you haven't built the wheel yet, rather than a failed build 4 minutes in.

2. **Post-bundle GPL scan.** After PyInstaller finishes, each bundler walks the output directory for files named `libavcodec*`, `libavformat*`, `libavutil*`, `libswscale*`, `libpostproc*`, `libavdevice*`, `libavfilter*`, `libx264*`, `libx265*` (and the Windows `.dll` equivalents). Any hit → the script exits non-zero with a pointer to this doc. This is the safety net against: a future `pip install --upgrade` between bundle builds, a new dep transitively pulling FFmpeg, or upstream wheel behavior change.

So the workflow on any platform is:

```
1. scripts/build_cv2_wheel.{sh,ps1}   # once per release — wheel lands in vendor/cv2-wheels/
2. scripts/bundle_python.{sh,ps1}     # every bundle — auto-picks up your wheel, scans for leaks
3. cargo tauri dev  /  cargo tauri build   # normal Tauri flow
```

## Consuming the wheels (release pipeline)

Once all four platform wheels are built (macOS arm64, Linux x64, Windows x64, Windows arm64), upload them to a GitHub release on `ben-ic/opencv-python-lgpl` and pin them in `backend/requirements.txt` via environment-marker-gated URLs — same pattern `ben-ic/libmpv-macos` uses for the LGPL libmpv dylib.

The CI job that regenerates these wheels runs on release only, not per PR — the upstream `opencv-python` bumps quarterly at most. Once the release URLs are pinned in `requirements.txt`, the local-wheel autopickup in `bundle_python.{sh,ps1}` becomes belt-and-suspenders rather than required — developers can still build locally for testing, but a fresh clone with `pip install -r requirements.txt` also gets an LGPL-clean cv2 on every supported platform.
