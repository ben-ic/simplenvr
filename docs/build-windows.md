# Build SimpleNVR on Windows (x86_64)

Target: a self-signed SimpleNVR installer (`.msi` + NSIS `.exe`) you can install on any Windows x86_64 machine. No code signing cert required — users click through the SmartScreen "Run anyway" warning once on first install.

Target triple: **`x86_64-pc-windows-msvc`**.

> These steps must be executed **on the Windows machine itself**. Tauri cannot cleanly cross-compile to Windows from macOS. Sit at the Windows machine (or RDP into it) for the build.

---

## 1. One-time machine setup

Run each block in **PowerShell as a regular user** unless noted. Reboot after the Rust install if prompted.

### 1.1 Git + Node.js + Python

```powershell
# From an elevated PowerShell (Admin), use winget to install the basics.
winget install --id Git.Git -e
winget install --id OpenJS.NodeJS.LTS -e
winget install --id Python.Python.3.12 -e
```

Verify in a **new** PowerShell window (so PATH updates are picked up):

```powershell
git --version
node --version
npm --version
python --version
```

### 1.2 Rust toolchain

```powershell
# Install rustup via winget (preferred) or from https://rustup.rs
winget install --id Rustlang.Rustup -e
```

Open a new PowerShell and verify:

```powershell
rustc --version
rustup default stable
rustup show
# Should show: Default host: x86_64-pc-windows-msvc
```

### 1.3 Microsoft C++ Build Tools (required by Rust MSVC target)

Tauri needs the MSVC linker. Install Visual Studio Build Tools with the "Desktop development with C++" workload:

```powershell
winget install --id Microsoft.VisualStudio.2022.BuildTools -e --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
```

After install, verify from a **new Developer PowerShell for VS 2022** (Start menu → search "Developer PowerShell"):

```powershell
cl.exe
# Should print the MSVC compiler banner (not "command not found")
```

### 1.4 WebView2 runtime

Windows 11 and recent Windows 10 ship with WebView2. If the build machine is older, install it:

```powershell
winget install --id Microsoft.EdgeWebView2Runtime -e
```

### 1.5 Tauri CLI

```powershell
cargo install tauri-cli --version "^2.0"
cargo tauri --version
```

---

## 2. Clone the repo

```powershell
cd $HOME
git clone https://github.com/ben-ic/simplenvr.git
cd simplenvr
```

---

## 3. Build pipeline

Run each step from the repo root (`$HOME\simplenvr`) in a **Developer PowerShell for VS 2022** window. That window has `cl.exe` on PATH, which the MSVC linker requires.

### 3.1 Fetch bundled binaries (FFmpeg + go2rtc + libmpv)

```powershell
.\scripts\fetch_ffmpeg.ps1
.\scripts\fetch_go2rtc.ps1
.\scripts\fetch_mpv.ps1
```

All three scripts download pinned, SHA-verified binaries:

- `ffmpeg-x86_64-pc-windows-msvc.exe` → `src-tauri\binaries\`
- `ffprobe-x86_64-pc-windows-msvc.exe` → `src-tauri\binaries\`
- `go2rtc-x86_64-pc-windows-msvc.exe` → `src-tauri\binaries\`
- `mpv.lib` (MSVC import library) → `src-tauri\`
- `mpv-2.dll` (runtime library) → `src-tauri\binaries\` and `src-tauri\`

The libmpv files come from [ben-ic/libmpv-win64](https://github.com/ben-ic/libmpv-win64) — a static build where FFmpeg, libass, libplacebo, and all other dependencies are baked into the single `mpv-2.dll` (~39 MB). No additional DLLs needed at runtime.

Verify:

```powershell
dir src-tauri\binaries\*.exe
dir src-tauri\mpv.lib
dir src-tauri\mpv-2.dll
```

### 3.2 Build the tether supervisor binary

```powershell
.\scripts\build_tether.ps1
```

Produces `src-tauri\binaries\tether-x86_64-pc-windows-msvc.exe`. This is the cross-platform parent-death supervisor; on Windows it uses a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` to guarantee children die when SimpleNVR dies.

### 3.3 Create the Python venv and bundle the backend

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r backend\requirements.txt
.\scripts\build_cv2_wheel.ps1   # one-time per release; ~45-60 min. See docs\cv2-selfbuild.md
.\scripts\bundle_python.ps1
```

`build_cv2_wheel.ps1` produces an LGPL-clean `opencv-python-headless` wheel (no FFmpeg, no libx264/libx265) in `vendor\cv2-wheels\`. You only need to rerun it when the pinned `OPENCV_PYTHON_TAG` in the script changes — roughly quarterly. See [`cv2-selfbuild.md`](cv2-selfbuild.md) for the full rationale.

`bundle_python.ps1` runs PyInstaller inside the venv and produces:

```
src-tauri\binaries\simplenvr-backend-dir\
```

It auto-installs your local cv2 wheel from `vendor\cv2-wheels\` over whatever PyPI served, then scans the bundle for GPL FFmpeg deps and fails the build if any slip through.

> If PyInstaller fails with missing modules (common with newly-added Python deps), edit `backend\main.spec` and add the missing module to `hiddenimports`, then re-run `.\scripts\bundle_python.ps1`.

> If `bundle_python.ps1` exits with "GPL FFmpeg deps found in the bundle", you skipped step 3.3a — run `.\scripts\build_cv2_wheel.ps1` first so the wheel lands in `vendor\cv2-wheels\`, then rerun `bundle_python.ps1`.

### 3.4 Frontend build

Done automatically by `cargo tauri build` via the `beforeBuildCommand` hook in `tauri.conf.json`. But for a one-time sanity check:

```powershell
cd frontend
npm install
npm run build
cd ..
```

### 3.5 Build the Tauri app + installer

```powershell
# Add src-tauri to LIB so the linker finds mpv.lib
$env:LIB = (Join-Path (Get-Location) 'src-tauri') + ';' + $env:LIB

cargo tauri build --target x86_64-pc-windows-msvc
```

This step takes 5-15 minutes on a first build (Rust compiles everything from scratch). Subsequent builds are much faster thanks to incremental compilation.

**How libmpv is bundled**: Tauri v2 auto-loads `src-tauri\tauri.windows.conf.json` on Windows builds. This file adds `mpv-2.dll` to the bundle resources so it's installed next to the `.exe` at runtime. The base `tauri.conf.json` doesn't include it because the DLL doesn't exist on Mac/Linux builds.

Output:

```
src-tauri\target\x86_64-pc-windows-msvc\release\bundle\msi\SimpleNVR_0.1.0_x64_en-US.msi
src-tauri\target\x86_64-pc-windows-msvc\release\bundle\nsis\SimpleNVR_0.1.0_x64-setup.exe
```

Either installer works; the NSIS one is smaller and uses the standard "next, next, finish" wizard.

> **Do NOT run `cargo tauri build` from inside `src-tauri\`.** It must be run from the repo root — the `beforeBuildCommand` in `tauri.conf.json` does `cd ../frontend && npm run build`, which only resolves correctly from the repo root.

---

## 4. Install and first run

1. Double-click the NSIS installer (`SimpleNVR_0.1.0_x64-setup.exe`).
2. Windows SmartScreen will warn: **"Windows protected your PC — Microsoft Defender SmartScreen prevented an unrecognized app from starting."** Click **More info → Run anyway**. This happens once per-machine, per-version of the unsigned installer.
3. The installer runs the NSIS wizard. Default install location is `C:\Program Files\SimpleNVR`.
4. Launch SimpleNVR from the Start menu.

On first launch:

- Tauri shell opens a window
- `tether` spawns `go2rtc` and `simplenvr-backend`
- Backend starts discovering cameras on the LAN via ONVIF WS-Discovery
- Within a few seconds, cameras should appear in the dashboard

---

## 5. Known gotchas

- **Windows Defender may quarantine the unsigned PyInstaller binary** (`simplenvr-backend.exe`) or the unsigned tether binary on first run. If so, add an exclusion for the install directory: Settings → Windows Security → Virus & threat protection → Manage settings → Exclusions → Add an exclusion → Folder → `C:\Program Files\SimpleNVR`.
- **Firewall prompt**: the first time SimpleNVR tries to listen on its internal ports (57321 for the backend, 1984 + 8554 for go2rtc), Windows Firewall will prompt for network access. Allow both **Private** and **Public** networks for LAN camera discovery to work.
- **ONVIF WS-Discovery uses multicast (239.255.255.250:3702)**. If the build machine is on a network that blocks multicast (some managed switches do), cameras won't auto-discover. Check the network mode is Private, not Public.
- **CPU decoder is currently the default** on Windows (no hardware acceleration). 5 x 5 MP streams will likely max the CPU. Hardware-decode autodetect is not yet enabled on Windows; for now, set a lower-resolution sub-stream on the cameras if CPU pegs.
- **First launch is slower** than subsequent launches because the PyInstaller bootloader unpacks the backend into `%LOCALAPPDATA%\Temp\_MEI*` on first run.
- **Log locations**:
  - Tauri shell logs: `%APPDATA%\com.simplenvr.app\logs\`
  - Backend logs: `%APPDATA%\com.simplenvr.app\logs\backend.log` (if the backend writes logs — check recorder/ and main.py configuration)
  - Recording storage: default `%APPDATA%\com.simplenvr.app\recordings\` (configurable in settings)

---

## 6. Iterating on changes

For a code change + rebuild cycle:

```powershell
# Frontend-only change:
cd frontend
npm run build
cd ..
cargo tauri build

# Backend Python change:
.\.venv\Scripts\Activate.ps1
.\scripts\bundle_python.ps1
cargo tauri build

# Rust / tether change:
.\scripts\build_tether.ps1    # only if tether changed
cargo tauri build

# Nuclear option (fresh rebuild, ~15 min):
Remove-Item -Recurse -Force src-tauri\target
cargo tauri build
```

---

## 7. Rebuilding libmpv (only when upgrading mpv)

The pre-built `mpv-2.dll` and `mpv.lib` are hosted at [ben-ic/libmpv-win64](https://github.com/ben-ic/libmpv-win64). To rebuild from source (e.g., to upgrade mpv or FFmpeg):

1. Clone the mpv source to `C:\Users\cates\dev\mpv-build` (or wherever you keep it)
2. Run the build script from **Developer PowerShell for VS 2022**:

```powershell
cd C:\Users\cates\dev\mpv-build
powershell -ExecutionPolicy Bypass -File .\build-libmpv.ps1
```

The script:
- Sets up `clang-cl` as the compiler (required by libplacebo)
- Builds all dependencies (FFmpeg, libass, libplacebo, harfbuzz, freetype, fribidi, zlib) as **static libraries** via meson subprojects
- Produces `mpv-2.dll` (~39 MB, all deps baked in) and `mpv.lib` (13 KB import library)
- Key flags: `-Ddefault_library=shared` for mpv itself, `-D<subproject>:default_library=static` for all dependencies

3. Upload the new build as a GitHub release:

```powershell
cd ..\libmpv-win64
$tag = "v0.41.0-$(git -C ..\mpv-build rev-parse --short HEAD)-static"
gh release create $tag ..\mpv-build\build\mpv-2.dll ..\mpv-build\build\mpv.lib --title $tag --notes "Static build"
```

4. Update `scripts\fetch_mpv.ps1` in the simplenvr repo with the new tag and SHA256 hashes:

```powershell
Get-FileHash ..\mpv-build\build\mpv-2.dll -Algorithm SHA256
Get-FileHash ..\mpv-build\build\mpv.lib -Algorithm SHA256
```

---

## 8. Code signing (later, when you ship to strangers)

Self-signed is fine for personal use on your own machines. Before any public distribution:

- **Cheapest path**: Azure Trusted Signing ($10/month) or SSL.com standard code signing (~$200/year)
- Add the cert thumbprint to `tauri.conf.json` under `bundle.windows.signCommand`
- Reference: https://tauri.app/v2/distribute/sign/windows/

Until then, accept the SmartScreen warning once per install.
