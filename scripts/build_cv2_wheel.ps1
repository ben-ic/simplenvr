# Build an LGPL-clean opencv-python-headless wheel for the current host
# (Windows x64 or arm64). macOS/Linux are covered by
# scripts/build_cv2_wheel.sh.
#
# Why this exists:
#   The stock opencv-python-headless wheel on PyPI bundles an FFmpeg
#   build that hard-links libx264/libx265 (GPL-2.0+). cv2's DLL pulls
#   avcodec in as an eager dependency at import time, so the GPL deps
#   get resolved into our closed-source bundle even though we never
#   call VideoCapture/VideoWriter. Building with -DWITH_FFMPEG=OFF
#   drops avcodec from the wheel. cv2's image algorithms (MOG2,
#   morphology, resize, matchTemplate, imencode) are unaffected.
#
# Output:
#   dist/cv2-wheels/opencv_python_headless-<version>-<tag>.whl
#   (one platform-specific wheel per run; dist/ is gitignored)
#
# Runtime: ~45-60 min on first run (OpenCV is a large C++ codebase and
# MSBuild parallelism on Windows is less efficient than make on Unix).
#
# Usage (PowerShell, from the repo root):
#   scripts\build_cv2_wheel.ps1
#
# Verify the output:
#   pip install vendor\cv2-wheels\opencv_python_headless-*.whl --force-reinstall
#   python -c "import cv2; print(cv2.__file__)"
#   dumpbin /dependents <that path>\cv2\cv2*.pyd | findstr /I "avcodec x26"
#     → should print nothing. Any hit = the build kept FFmpeg.
#
# Prerequisites (install once):
#   - Visual Studio 2022 with "Desktop development with C++" workload
#     (or the standalone Build Tools package)
#   - CMake 3.20+  (winget install Kitware.CMake)
#   - Git         (winget install Git.Git)
#   - Python 3.11 or matching the repo's .venv

$ErrorActionPreference = 'Stop'

# Pin: opencv-python uses unusual tag naming — tag "92" == release
# 4.13.0.92. Bump this intentionally; regenerate all platform wheels
# from the same tag so runtime behavior matches across OSes.
$OpencvPythonTag = '92'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir
$WorkDir   = Join-Path $env:TEMP 'opencv-selfbuild'
$OutputDir = Join-Path $RepoRoot 'vendor\cv2-wheels'

# ── Prerequisites ───────────────────────────────────────────────────
foreach ($cmd in @('cmake', 'git')) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Error "$cmd not found on PATH. Install via winget: winget install Kitware.CMake Git.Git"
        exit 1
    }
}

# Activate the repo venv so `pip wheel` uses the same Python the
# sidecar runs against.
$VenvActivate = Join-Path $RepoRoot '.venv\Scripts\Activate.ps1'
if (Test-Path $VenvActivate) {
    . $VenvActivate
} else {
    Write-Error ".venv not found at $($RepoRoot)\.venv — create it first (python -m venv .venv)."
    exit 1
}

# ── Fetch opencv-python source (cached by tag) ──────────────────────
New-Item -ItemType Directory -Force -Path $WorkDir, $OutputDir | Out-Null
Set-Location $WorkDir

$SrcDir = Join-Path $WorkDir 'opencv-python'
$existingTag = $null
if (Test-Path $SrcDir) {
    Push-Location $SrcDir
    try { $existingTag = (git describe --tags --exact-match 2>$null) } catch { }
    Pop-Location
}

if ($existingTag -ne $OpencvPythonTag) {
    if (Test-Path $SrcDir) { Remove-Item -Recurse -Force $SrcDir }
    Write-Host "Cloning opencv-python @ tag $OpencvPythonTag..."
    git clone --recursive --depth 1 --branch $OpencvPythonTag `
        https://github.com/opencv/opencv-python.git
} else {
    Write-Host "Reusing existing checkout at $SrcDir (tag $OpencvPythonTag)."
}

Set-Location $SrcDir

# ── Install Python build deps into the repo venv ───────────────────
pip install --quiet --upgrade pip setuptools wheel scikit-build

# ── Build ──────────────────────────────────────────────────────────
$NProc = [Environment]::ProcessorCount

Write-Host ""
Write-Host "Compiling with $NProc parallel jobs..."
Write-Host "  CMAKE_ARGS: -DWITH_FFMPEG=OFF -DWITH_GSTREAMER=OFF -DWITH_1394=OFF"
Write-Host "  Output:     $OutputDir"
Write-Host ""

# ENABLE_HEADLESS=1 selects opencv-python-headless (no Qt/GTK).
# CMAKE_BUILD_PARALLEL_LEVEL is the MSBuild-aware equivalent of -jN
# and is honored by scikit-build's CMake driver.
$env:ENABLE_HEADLESS                = '1'
$env:CMAKE_ARGS                     = '-DWITH_FFMPEG=OFF -DWITH_GSTREAMER=OFF -DWITH_1394=OFF'
$env:CMAKE_BUILD_PARALLEL_LEVEL     = "$NProc"

pip wheel . -w $OutputDir --verbose

Write-Host ""
Write-Host "Wheel built:"
Get-ChildItem (Join-Path $OutputDir 'opencv_python_headless-*.whl')
