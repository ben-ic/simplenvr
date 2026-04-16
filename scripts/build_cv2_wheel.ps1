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
# Windows native builds are still substantial even with Ninja).
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
#   - CMake 3.20+  (provided by Visual Studio Community C++ tools)
#   - Ninja        (installed into the repo venv by this script)
#   - Git         (winget install Git.Git)
#   - Python 3.11 or matching the repo's .venv

[CmdletBinding()]
param(
    # Repo convention on Windows is .venv-x64 (see build_windows.ps1 +
    # bundle_python.ps1). Override for local setups using .venv.
    [string]$VenvName = '.venv-x64'
)

$ErrorActionPreference = 'Stop'

# Pin: opencv-python uses unusual tag naming — tag "92" == release
# 4.13.0.92. Bump this intentionally; regenerate all platform wheels
# from the same tag so runtime behavior matches across OSes.
$OpencvPythonTag = '92'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir
$WorkDir   = Join-Path $env:TEMP 'opencv-selfbuild'
$OutputDir = Join-Path $RepoRoot 'vendor\cv2-wheels'

function Import-VsDevEnvironment {
    if ((Get-Command cl.exe -ErrorAction SilentlyContinue) -and (Get-Command nmake.exe -ErrorAction SilentlyContinue)) {
        return
    }

    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path $vswhere)) {
        Write-Error "vswhere not found at $vswhere. Install VS 2022 Build Tools and retry."
        exit 1
    }

    $installPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (-not $installPath) {
        Write-Error 'Could not locate a VS installation with VC tools via vswhere.'
        exit 1
    }

    $vsDevCmd = Join-Path $installPath 'Common7\Tools\VsDevCmd.bat'
    if (-not (Test-Path $vsDevCmd)) {
        Write-Error "VsDevCmd.bat not found at $vsDevCmd"
        exit 1
    }

    $setOutput = & cmd.exe /d /s /c "call `"$vsDevCmd`" -arch=x64 -host_arch=x64 >nul && set"
    foreach ($line in $setOutput) {
        $idx = $line.IndexOf('=')
        if ($idx -gt 0) {
            $name = $line.Substring(0, $idx)
            $value = $line.Substring($idx + 1)
            [Environment]::SetEnvironmentVariable($name, $value, 'Process')
        }
    }

    if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
        Write-Error 'cl.exe is still not available after importing VsDevCmd environment.'
        exit 1
    }
    if (-not (Get-Command nmake.exe -ErrorAction SilentlyContinue)) {
        Write-Error 'nmake.exe is still not available after importing VsDevCmd environment.'
        exit 1
    }
}

function Patch-SkbuildWindowsArchProbe([string]$PythonExePath) {
    $skbuildWindowsPy = & $PythonExePath -c "import pathlib, skbuild; print((pathlib.Path(skbuild.__file__).resolve().parent / 'platform_specifics' / 'windows.py'))"
    if ($LASTEXITCODE -ne 0 -or -not $skbuildWindowsPy) {
        Write-Error 'Failed to locate installed skbuild Windows platform module.'
        exit 1
    }

    $content = Get-Content $skbuildWindowsPy -Raw
    $newBlock = @"
def _compute_arch() -> str:
    """Currently only supports Intel -> ARM cross-compilation."""
    if sysconfig.get_platform() == "win-arm64" or "arm64" in os.environ.get("SETUPTOOLS_EXT_SUFFIX", "").lower():
        return "ARM64"
    if sysconfig.get_platform() == "win-amd64":
        return "x64"
    if platform.architecture()[0] == "64bit":
        return "x64"
    return "Win32"
"@

    if ($content -notmatch '(?ms)^def _compute_arch\(\) -> str:\s*.*?^\s*return "Win32"\s*$') {
        Write-Error 'Unexpected skbuild Windows platform module layout; arch probe patch could not be applied.'
        exit 1
    }

    $content = $content.Replace('`r`nimport sys', 'import sys')
    $content = $content.Replace('`r`nimport sysconfig', 'import sysconfig')

    if ($content -notmatch '(?m)^import sys\s*$') {
        $content = $content -replace '(?m)^(import subprocess\s*)$', "`$1`r`nimport sys"
    }

    if ($content -notmatch '(?m)^import sysconfig\s*$') {
        if ($content -match '(?m)^import sys\s*$') {
            $content = $content -replace '(?m)^(import sys\s*)$', "`$1`r`nimport sysconfig"
        } else {
            Write-Error 'Failed to repair skbuild Windows platform imports.'
            exit 1
        }
    }

    $patched = [regex]::Replace(
        $content,
        '(?ms)^def _compute_arch\(\) -> str:\s*.*?^\s*return "Win32"\s*$',
        $newBlock
    )

    $patched = $patched.Replace(
        'super().__init__(name, env, arch=arch, args=args)',
        'super().__init__(name, env, arch=None, args=args)'
    )
    $patched = $patched.Replace(
        'args = [f"-D_SKBUILD_FORCE_MSVC={vs_version}"]',
        'args = []'
    )
    Set-Content -Path $skbuildWindowsPy -Value $patched -NoNewline

}

# ── Prerequisites ───────────────────────────────────────────────────
Import-VsDevEnvironment
foreach ($cmd in @('cmake', 'git')) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Error "$cmd not found on PATH after importing the Visual Studio build environment. Ensure Visual Studio Community with C++ and CMake tools is installed."
        exit 1
    }
}

# Activate the repo venv so `pip wheel` uses the same Python the
# sidecar runs against.
$VenvActivate = Join-Path $RepoRoot "$VenvName\Scripts\Activate.ps1"
$PythonExe = Join-Path $RepoRoot "$VenvName\Scripts\python.exe"
if (Test-Path $VenvActivate) {
    . $VenvActivate
} else {
    Write-Error ('{0} not found at {1}\{0} - create it first (python -m venv {0}), or pass -VenvName to match your local layout.' -f $VenvName, $RepoRoot)
    exit 1
}

if (-not (Test-Path $PythonExe)) {
    Write-Error ('Python executable not found at {0}.' -f $PythonExe)
    exit 1
}

# ── Fetch opencv-python source (cached by tag) ──────────────────────
New-Item -ItemType Directory -Force -Path $WorkDir, $OutputDir | Out-Null

# Skip everything if a wheel for this exact tag is already in OutputDir.
$existingWheel = Get-ChildItem (Join-Path $OutputDir "opencv_python_headless-4.13.0.$OpencvPythonTag-*.whl") -ErrorAction SilentlyContinue | Select-Object -First 1
if ($existingWheel) {
    Write-Host ("Wheel already built ({0}), skipping rebuild." -f $existingWheel.Name)
    exit 0
}

Set-Location $WorkDir

$SrcDir = Join-Path $WorkDir 'opencv-python'
$existingTag = $null
if (Test-Path $SrcDir) {
    Push-Location $SrcDir
    try { $existingTag = (git describe --tags --exact-match 2>$null) } catch { }
    Pop-Location
}

$freshClone = $false
if ($existingTag -ne $OpencvPythonTag) {
    if (Test-Path $SrcDir) { Remove-Item -Recurse -Force $SrcDir }
    Write-Host ('Cloning opencv-python (tag {0})...' -f $OpencvPythonTag)
    git clone --recursive --depth 1 --branch $OpencvPythonTag `
        https://github.com/opencv/opencv-python.git
    $freshClone = $true
} else {
    Write-Host ('Reusing existing checkout at {0} (tag {1}).' -f $SrcDir, $OpencvPythonTag)
}

Set-Location $SrcDir

# Only wipe the CMake cache when we re-cloned (tag changed). On reruns of
# the same tag, Ninja will do an incremental build — much faster.
if ($freshClone -and (Test-Path '_skbuild')) {
    Remove-Item -Recurse -Force '_skbuild'
}

# ── Patch upstream setup.py for Windows packaging/build quirks ─────────
# 1) opencv-python's setup.py (Windows branch) hardcodes
#      bin/opencv_videoio_ffmpeg\d{4}_64\.dll
#    in rearrange_cmake_output_data, then raises `Not found: ...` when the
#    packaging step can't find it. With -DWITH_FFMPEG=OFF that DLL is
#    never built, so packaging always fails on Windows. Non-Windows
#    branches default to []; only the nt branch needs this patched out.
# 2) On Windows ARM64 hosts running x64 Python under emulation,
#    platform.machine() still reports ARM64. Upstream setup.py then forces
#    CMAKE_GENERATOR_PLATFORM=ARM64, which breaks x64 command-line generators.
#    Use sysconfig.get_platform() instead.
# 3) On Windows x64 generally, upstream setup.py also forces
#    CMAKE_GENERATOR_PLATFORM=x64. That breaks Ninja/NMake; command-line
#    generators should infer x64 from the imported VS environment.
# Idempotent — running again on an already-patched tree is a no-op.
$SetupPy = Join-Path $SrcDir 'setup.py'
$patchNeedle = '[r"bin/opencv_videoio_ffmpeg\d{4}%s\.dll" % ("_64" if is64 else "")]'
$setupContent = Get-Content $SetupPy -Raw
if ($setupContent.Contains($patchNeedle) -or $setupContent.Contains('if platform.machine() == "ARM64" and sys.platform == "win32"')) {
    Write-Host 'Patching opencv-python setup.py: drop FFmpeg DLL requirement and fix Windows ARM64 x64-emulation platform detection.'
    $patched = $setupContent.Replace(
        "[r`"bin/opencv_videoio_ffmpeg\d{4}%s\.dll`" % (`"_64`" if is64 else `"`")]`r`n            if os.name == `"nt`"`r`n            else []",
        '[]'
    ).Replace(
        "[r`"bin/opencv_videoio_ffmpeg\d{4}%s\.dll`" % (`"_64`" if is64 else `"`")]`n            if os.name == `"nt`"`n            else []",
        '[]'
    ).Replace(
        'if platform.machine() == "ARM64" and sys.platform == "win32"',
        'if sysconfig.get_platform() == "win-arm64" and sys.platform == "win32"'
    ).Replace(
        '            else ["-DCMAKE_GENERATOR_PLATFORM=x64"] if is64 and sys.platform == "win32"',
        '            else [] if is64 and sys.platform == "win32"'
    )
    if ($patched -eq $setupContent) {
        Write-Error 'setup.py patch failed — upstream structure changed; inspect setup.py around the opencv_videoio_ffmpeg regex and update this block.'
        exit 1
    }
    Set-Content $SetupPy -Value $patched -NoNewline
}

# ── Install Python build deps into the repo venv ───────────────────
# Build isolation can hide NumPy headers on some host/python combos.
# Keep the build in this venv and pass NumPy's include path explicitly.
& $PythonExe -m pip install --quiet --upgrade pip setuptools wheel scikit-build ninja "numpy>=1.26,<3"
if ($LASTEXITCODE -ne 0) {
    Write-Error 'Failed to install cv2 build dependencies into the venv.'
    exit 1
}

Patch-SkbuildWindowsArchProbe -PythonExePath $PythonExe

$NumpyInclude = & $PythonExe -c "import numpy; print(numpy.get_include())"
if ($LASTEXITCODE -ne 0 -or -not $NumpyInclude) {
    Write-Error 'Failed to import numpy from the venv after dependency installation.'
    exit 1
}

$NumpyIncludeForCMake = $NumpyInclude.Trim() -replace '\\', '/'

# ── Build ──────────────────────────────────────────────────────────
$NProc = [Environment]::ProcessorCount
$targetProcessorArgs = ''
if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
    $targetProcessorArgs = ' -DCMAKE_SYSTEM_PROCESSOR=AMD64 -DPNG_ARM_NEON=off -DPNG_ARM_NEON_OPT=0'
}

Write-Host ""
Write-Host "Compiling with $NProc parallel jobs..."
Write-Host "  CMAKE_GENERATOR: Ninja"
Write-Host "  CMAKE_ARGS: -DWITH_FFMPEG=OFF -DWITH_GSTREAMER=OFF -DWITH_1394=OFF -DENABLE_LIBJPEG_TURBO_SIMD=OFF$targetProcessorArgs"
Write-Host "  NumPy include: $NumpyInclude"
Write-Host "  Output:     $OutputDir"
Write-Host ""

# ENABLE_HEADLESS=1 selects opencv-python-headless (no Qt/GTK).
# CMAKE_BUILD_PARALLEL_LEVEL is honored by scikit-build's CMake driver.
$env:ENABLE_HEADLESS                 = '1'
$env:CMAKE_GENERATOR                 = 'Ninja'
$env:CMAKE_ARGS                      = "-DWITH_FFMPEG=OFF -DWITH_GSTREAMER=OFF -DWITH_1394=OFF -DENABLE_LIBJPEG_TURBO_SIMD=OFF$targetProcessorArgs -DPython_NumPy_INCLUDE_DIRS:PATH=$NumpyIncludeForCMake -DPython3_NumPy_INCLUDE_DIRS:PATH=$NumpyIncludeForCMake"
$env:CMAKE_BUILD_PARALLEL_LEVEL      = "$NProc"
Remove-Item Env:CMAKE_GENERATOR_PLATFORM -ErrorAction SilentlyContinue
Remove-Item Env:SKBUILD_CONFIGURE_OPTIONS -ErrorAction SilentlyContinue

& $PythonExe -m pip wheel . -w $OutputDir --verbose --no-build-isolation
if ($LASTEXITCODE -ne 0) {
    Write-Error 'opencv-python wheel build failed.'
    exit 1
}

Write-Host ""
Write-Host "Wheel built:"
Get-ChildItem (Join-Path $OutputDir 'opencv_python_headless-*.whl')
