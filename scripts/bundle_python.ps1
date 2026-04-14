# Bundle the SimpleNVR Python backend into a directory (onedir mode)
# via PyInstaller. The output directory is bundled into the Tauri app
# via the `resources` config. Windows equivalent of bundle_python.sh.
#
# Usage:
#   .\scripts\bundle_python.ps1                     # default venv
#   .\scripts\bundle_python.ps1 -VenvName .venv-x64 # custom venv dir

[CmdletBinding()]
param(
    [string]$VenvName = '.venv'
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir
Set-Location $RepoRoot

# Always use the project venv — never install PyInstaller globally.
& (Join-Path $RepoRoot "$VenvName\Scripts\Activate.ps1")

# Ensure backend runtime deps (onnxruntime, opencv, scipy, psutil, ...)
# are present in the venv before PyInstaller analyzes imports — a
# missing dep would be silently omitted from the bundle and bite at
# sidecar startup. Idempotent; quiet when already satisfied.
pip install --quiet -r backend/requirements.txt
pip install --quiet 'pyinstaller>=6.0'

# Replace stock opencv-python-headless with our self-built LGPL-clean
# wheel if one is present at vendor\cv2-wheels\. Stock PyPI ships
# libavcodec linked against libx264/libx265 (GPL-2.0+); the self-built
# wheel is compiled with -DWITH_FFMPEG=OFF so those deps don't exist.
# See docs\cv2-selfbuild.md and scripts\build_cv2_wheel.ps1.
$Cv2WheelDir = Join-Path $RepoRoot 'vendor\cv2-wheels'
switch ($env:PROCESSOR_ARCHITECTURE) {
    'ARM64' { $WheelGlob = 'opencv_python_headless-*-win_arm64.whl' }
    'AMD64' { $WheelGlob = 'opencv_python_headless-*-win_amd64.whl' }
    default { $WheelGlob = $null }
}
if ($WheelGlob -and (Test-Path $Cv2WheelDir)) {
    $LocalWheel = Get-ChildItem -Path $Cv2WheelDir -Filter $WheelGlob -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($LocalWheel) {
        Write-Host "Using self-built cv2 wheel: $($LocalWheel.Name)"
        pip install --quiet --force-reinstall --no-deps $LocalWheel.FullName
    } else {
        Write-Warning "no self-built cv2 wheel found at $Cv2WheelDir matching $WheelGlob"
        Write-Warning "Bundle will likely contain GPL FFmpeg deps. Run"
        Write-Warning "scripts\build_cv2_wheel.ps1 first for a license-clean build."
    }
}

# Verify the in-tree D-FINE weights before building so we fail before
# PyInstaller's analysis step rather than in the middle of it.
& (Join-Path $ScriptDir 'fetch_dfine.ps1')

$OutRoot = "src-tauri\binaries"
# Fixed name (no triple suffix) — Tauri resources don't use the
# externalBin triple-suffix convention.
$OutName = "simplenvr-backend-dir"

if (-not (Test-Path $OutRoot)) {
    New-Item -ItemType Directory -Path $OutRoot | Out-Null
}

$BuildDir   = "build\pyinstaller"
$RawOutDir  = Join-Path $OutRoot "simplenvr-backend"
$FinalOut   = Join-Path $OutRoot $OutName

foreach ($p in @($BuildDir, $RawOutDir, $FinalOut)) {
    if (Test-Path $p) {
        Remove-Item -Recurse -Force $p
    }
}

pyinstaller backend\main.spec `
    --distpath $OutRoot `
    --workpath $BuildDir `
    --noconfirm

# Onedir output is a directory; rename to triple-suffixed name.
Move-Item $RawOutDir $FinalOut

# Post-bundle GPL scan — fails loud if any FFmpeg GPL-able dep (avcodec,
# avformat, avutil, swscale, postproc, x264, x265) slipped into the bundle.
# Catches: someone re-running `pip install --upgrade` between bundle builds,
# a new dep that pulls FFmpeg transitively, or a future pip resolver change.
# File-name scan (not dep analysis) — trivially cross-platform and covers
# the realistic failure mode (stock opencv-python-headless shipping these
# as standalone DLLs).
Write-Host "Scanning bundle for GPL FFmpeg deps..."
$BadPatterns = @(
    'avcodec*.dll', 'avformat*.dll', 'avutil*.dll',
    'swscale*.dll', 'postproc*.dll',
    'avdevice*.dll', 'avfilter*.dll',
    'x264*.dll', 'x265*.dll',
    'libavcodec*', 'libavformat*', 'libavutil*',
    'libswscale*', 'libpostproc*',
    'libavdevice*', 'libavfilter*',
    'libx264*', 'libx265*'
)
$Hits = @()
foreach ($pattern in $BadPatterns) {
    $found = Get-ChildItem -Recurse -File -Path $FinalOut -Filter $pattern -ErrorAction SilentlyContinue
    if ($found) { $Hits += $found }
}

if ($Hits.Count -gt 0) {
    Write-Host "ERROR: GPL FFmpeg deps found in the bundle:" -ForegroundColor Red
    $Hits | ForEach-Object { Write-Host "  $($_.FullName)" -ForegroundColor Red }
    Write-Host ""
    Write-Host "Stock opencv-python-headless was installed instead of the self-built" -ForegroundColor Red
    Write-Host "LGPL-clean wheel. Fix: run scripts\build_cv2_wheel.ps1, then re-run" -ForegroundColor Red
    Write-Host "this script. See docs\cv2-selfbuild.md for details." -ForegroundColor Red
    exit 1
}
Write-Host "  -> no GPL FFmpeg deps found"

Write-Host "Bundle ready at: $FinalOut\"
