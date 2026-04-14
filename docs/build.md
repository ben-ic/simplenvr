# Building SimpleNVR from source

SimpleNVR is a [Tauri](https://tauri.app) v2 desktop app with three moving pieces:

- **Rust shell** (`src-tauri/`) — window, process supervisor, installer
- **Python backend** (`backend/`) — discovery, recording, detection, HTTP + WebSocket API
- **React + TS frontend** (`frontend/`) — the UI, built with Vite

On top of those it bundles several native binaries (`ffmpeg`, `go2rtc`, `libmpv` on macOS, the `tether` parent-death supervisor) and two ONNX model files (`D-FINE` for object detection, `YAMNet` for audio classification). A release build also requires an LGPL-clean, self-built `opencv-python-headless` wheel — the stock PyPI wheel is GPL-contaminated and we scan and reject it at bundle time.

There's a lot going on, which is why there are three scripts that wrap it all.

> **Windows has its own runbook.** Windows has enough platform-specific setup (winget, MSVC, Developer PowerShell, WebView2, Defender exclusions) that it gets its own doc: [`build-windows.md`](build-windows.md). The sections below cover **macOS and Linux**.

---

## TL;DR — three commands

From a fresh clone on macOS or Linux:

```bash
git clone https://github.com/ben-ic/simplenvr.git && cd simplenvr

./scripts/setup.sh --with-cv2   # one-time machine setup  (~45 min first run)
./scripts/dev.sh                # run SimpleNVR in dev mode
./scripts/build.sh              # build the shippable installer
```

The first command is the slow one (cv2 wheel compile takes 30–40 min). The other two are fast and you'll run them many times.

---

## Prerequisites

`scripts/setup.sh` checks all of these on its first run and fails with one consolidated list of what you're missing. The check is non-interactive — install the tools yourself using whatever package manager you prefer, then rerun `setup.sh`.

| Tool | Why | Install |
|---|---|---|
| **Rust** (stable) | Builds the Tauri shell and the `tether` supervisor | [rustup.rs](https://rustup.rs) |
| **cargo-tauri** `^2.0` | Tauri's build driver | `cargo install tauri-cli --version '^2.0'` |
| **Node.js 20 LTS** + `npm` | Frontend build | [nodejs.org](https://nodejs.org) or `brew install node` |
| **Python 3.11+** | Backend runtime + PyInstaller bundle | `brew install python@3.12` / `apt install python3.12 python3.12-venv` |
| **CMake 3.20+** | Compiles the self-built cv2 wheel | `brew install cmake` / `apt install cmake` |
| **`gh` CLI** *(macOS only)* | Downloads the pinned libmpv dylib from a GitHub release | `brew install gh` then `gh auth login` |
| **System build tools** | C/C++ toolchain for native extensions | macOS: `xcode-select --install` · Linux: `apt install build-essential libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev libssl-dev` |

---

## `./scripts/setup.sh` — one-time machine setup

Run this once after cloning. It's idempotent, so you can safely rerun it after pulling if anything looks off.

```bash
./scripts/setup.sh              # core setup
./scripts/setup.sh --with-cv2   # + build the LGPL-clean cv2 wheel (30-40 min)
```

What it does, in order:

1. Checks every prereq above is on PATH. Fails loud with a single list.
2. Creates `.venv` and installs `backend/requirements.txt`.
3. Fetches pinned bundled binaries: `ffmpeg`, `go2rtc`, `D-FINE` (verify-only, the weights are committed in-tree), `YAMNet` (verify-only, same), and `libmpv` on macOS.
4. Compiles the `tether` supervisor.
5. Runs `npm install` in `frontend/` (surfaces npm problems before your first dev run).
6. **With `--with-cv2`** only: builds `opencv-python-headless` with `-DWITH_FFMPEG=OFF` and drops the wheel in `vendor/cv2-wheels/`. This is the slow step — see [`cv2-selfbuild.md`](cv2-selfbuild.md) for why it exists.

### When to rerun `--with-cv2`

Almost never. The wheel is pinned to a specific `OPENCV_PYTHON_TAG` in `scripts/build_cv2_wheel.sh` (currently `92` == OpenCV 4.13.0.92). You only need to rebuild it when that tag changes, which happens roughly quarterly and is always an intentional bump.

If you skip `--with-cv2` on the first run, `./scripts/dev.sh` will still work — but `./scripts/build.sh` will fail immediately with a pointer back here.

---

## `./scripts/dev.sh` — run in development mode

```bash
./scripts/dev.sh                # rebundle + launch
./scripts/dev.sh --skip-bundle  # only if you *know* backend/ is unchanged
```

> **⚠️ The load-bearing gotcha.** `cargo tauri dev` runs the **PyInstaller onedir bundle** at `src-tauri/binaries/simplenvr-backend-dir/`, not your `backend/*.py` source files. If you edit Python and rerun a plain `cargo tauri dev`, your change is invisible until you rebundle.
>
> `./scripts/dev.sh` rebundles first, every time. That's its whole job. Use it, not the raw cargo command.

What hot-reloads, and what doesn't:

- **Frontend** (React / CSS / TS): hot-reloads automatically. No action.
- **Rust** (`src-tauri/`): auto-recompiles in place while `dev.sh` is running.
- **Python** (`backend/`): **does not** hot-reload. `Ctrl-C`, then rerun `./scripts/dev.sh`.

### What dev mode gives you

- Vite dev server at `http://localhost:3000`
- Live backend stdout/stderr in your terminal (if Python crashes, you see the traceback)
- Tauri shell's own logs at `~/Library/Logs/com.simplenvr.app/` (macOS) or `~/.local/share/com.simplenvr.app/logs/` (Linux)

---

## `./scripts/build.sh` — ship a release build

```bash
./scripts/build.sh                  # host platform, default bundle (dmg on macOS, deb on Linux)
./scripts/build.sh --bundles app    # just the .app, no dmg
./scripts/build.sh --bundles deb    # Linux .deb
```

What it does:

1. Sanity-checks that `setup.sh` has been run (`.venv` + fetched binaries present).
2. **Sanity-checks that an LGPL-clean cv2 wheel is at `vendor/cv2-wheels/`**, and fails fast with a pointer to `./scripts/setup.sh --with-cv2` if not. Much better than wasting 4 minutes on a PyInstaller build that then dies at the GPL scanner.
3. Runs `scripts/bundle_python.sh`:
   - Reinstalls the LGPL-clean cv2 wheel over whatever `pip install -r requirements.txt` pulled from PyPI.
   - Runs PyInstaller → `src-tauri/binaries/simplenvr-backend-dir/`.
   - Walks the bundle looking for `libx264*`, `libx265*`, `libavcodec*`, etc. Any hit = the build fails. This is the safety net against future pip changes silently reintroducing GPL.
4. Runs `cargo tauri build --bundles <format>`.
5. Prints the output path.

Output lands in `src-tauri/target/release/bundle/<format>/`.

---

## What's in the scripts directory

You don't normally need to run these individually — `setup.sh` / `dev.sh` / `build.sh` drive them. Reach for them when something upstream changes:

| Script | When to run directly |
|---|---|
| `fetch_ffmpeg.sh` | Bumping FFmpeg or adding a new target triple |
| `fetch_go2rtc.sh` | Bumping go2rtc version (update the SHA256 too) |
| `fetch_mpv.sh` | Bumping the libmpv release in `ben-ic/libmpv-macos` |
| `fetch_dfine.sh` | Verifying / re-pinning the committed D-FINE weights |
| `fetch_yamnet.sh` | Verifying / re-pinning the committed YAMNet weights |
| `build_tether.sh` | Rebuilding the supervisor after editing `src-tauri/tether/` |
| `build_cv2_wheel.sh` | Bumping `OPENCV_PYTHON_TAG` |
| `bundle_python.sh` | Manual PyInstaller run without Tauri — rarely useful |

---

## Related reading

- [`build-windows.md`](build-windows.md) — the full Windows build runbook
- [`cv2-selfbuild.md`](cv2-selfbuild.md) — why the self-built opencv wheel exists, how to verify it's LGPL-clean
- [`architecture.md`](architecture.md) — what these bundled binaries actually do at runtime
