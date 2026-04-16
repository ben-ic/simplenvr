# Build the SimpleNVR Windows installer end-to-end.
#
# Orchestrates BUILD-WINDOWS.md section 3:
#   1. Fetch bundled binaries (FFmpeg, go2rtc)
#   2. Build the tether supervisor
#   3. Create/reuse .venv and bundle the Python backend via PyInstaller
#   4. cargo tauri build  (produces .msi + NSIS .exe)
#
# Prerequisites: run scripts\setup_windows.ps1 first, then reopen in a
# 'Developer PowerShell for VS 2022' window (so cl.exe is on PATH).
#
# Usage (from repo root, in Developer PowerShell for VS 2022):
#   .\scripts\build_windows.ps1
#
# The script does NOT run the resulting installer -- it just builds it.
# Output paths are printed at the end.

[CmdletBinding()]
param(
    [switch]$SkipVenvInstall,  # set if you know backend\requirements.txt hasn't changed
    [switch]$ForceBundle       # force PyInstaller rebundle even when backend inputs are unchanged
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Test-PythonMatches {
    param(
        [Parameter(Mandatory)] [string]$PythonPath,
        [Parameter(Mandatory)] [string]$ExpectedPlatform,
        [string]$ExpectedVersionPrefix
    )

    if (-not (Test-Path $PythonPath)) {
        return $false
    }

    $probe = & $PythonPath -c "import sys,sysconfig; print(sysconfig.get_platform()); print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
    if ($LASTEXITCODE -ne 0 -or $probe.Count -lt 2) {
        return $false
    }

    if ($probe[0] -ne $ExpectedPlatform) {
        return $false
    }

    if ($ExpectedVersionPrefix -and -not $probe[1].StartsWith($ExpectedVersionPrefix)) {
        return $false
    }

    return $true
}

function Resolve-X64Python312 {
    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($candidate in @(
        'C:\Python312-x64\python.exe',
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312-x64\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
    )) {
        if ($candidate -and (Test-Path $candidate)) {
            $candidates.Add($candidate)
        }
    }

    $uvRoot = Join-Path $env:APPDATA 'uv\python'
    if (Test-Path $uvRoot) {
        Get-ChildItem -Path $uvRoot -Directory -Filter 'cpython-3.12.*-windows-x86_64-none' -ErrorAction SilentlyContinue |
            ForEach-Object {
                $candidate = Join-Path $_.FullName 'python.exe'
                if (Test-Path $candidate) {
                    $candidates.Add($candidate)
                }
            }
    }

    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-PythonMatches -PythonPath $candidate -ExpectedPlatform 'win-amd64' -ExpectedVersionPrefix '3.12') {
            return $candidate
        }
    }

    return $null
}

# --- Repo-root sanity check (BUILD-WINDOWS.md line 173 warning) -----------
if (-not (Test-Path (Join-Path $RepoRoot 'src-tauri\tauri.conf.json'))) {
    Write-Error "[build] must run from repo root. Expected src-tauri\tauri.conf.json."
    exit 1
}

# --- Prerequisite check ----------------------------------------------------
# Hard-fails if anything is missing, with a pointer to setup_windows.ps1.
# We check tools are on PATH, not versions -- setup_windows.ps1 pins versions.
function Test-Prerequisites {
    $required = @(
        @{ Name = 'git';     Hint = 'setup sec 1.1' },
        @{ Name = 'node';    Hint = 'setup sec 1.1' },
        @{ Name = 'npm';     Hint = 'setup sec 1.1' },
        @{ Name = 'python';  Hint = 'setup sec 1.1' },
        @{ Name = 'rustc';   Hint = 'setup sec 1.2 -- reopen shell after rustup install' },
        @{ Name = 'cargo';   Hint = 'setup sec 1.2 -- reopen shell after rustup install' },
        @{ Name = 'cl';      Hint = 'setup sec 1.3 -- must run from Developer PowerShell for VS 2022' },
        @{ Name = 'cargo-tauri'; Hint = 'cargo install tauri-cli --version "^2.0"' }
    )
    $missing = @()
    foreach ($tool in $required) {
        if (-not (Get-Command $tool.Name -ErrorAction SilentlyContinue)) {
            $missing += "  - $($tool.Name)  ($($tool.Hint))"
        }
    }
    if ($missing.Count -gt 0) {
        Write-Host "[build] missing prerequisites:" -ForegroundColor Red
        $missing | ForEach-Object { Write-Host $_ -ForegroundColor Red }
        Write-Host ""
        Write-Host "Run .\scripts\setup_windows.ps1 (as Admin), then reopen in"
        Write-Host "a 'Developer PowerShell for VS 2022' window and retry."
        exit 1
    }
}
Test-Prerequisites

function Ensure-RtspMosaicRepo {
    $pluginRoot = Join-Path (Split-Path -Parent $RepoRoot) 'tauri-plugin-rtsp-mosaic'
    $pluginUrl  = 'https://github.com/ben-ic/tauri-plugin-rtsp-mosaic.git'

    Write-Host "[build] === ensure tauri-plugin-rtsp-mosaic checkout ==="
    if (-not (Test-Path (Join-Path $pluginRoot '.git'))) {
        git clone $pluginUrl $pluginRoot
        if ($LASTEXITCODE -ne 0) { throw 'failed to clone tauri-plugin-rtsp-mosaic' }
    }

    if (-not (Test-Path (Join-Path $pluginRoot 'Cargo.toml'))) {
        throw "missing Cargo.toml in $pluginRoot"
    }

    $guestJs = Join-Path $pluginRoot 'guest-js'
    if (-not (Test-Path (Join-Path $guestJs 'package.json'))) {
        throw "missing guest-js/package.json in $guestJs"
    }

    $guestDist = Join-Path $guestJs 'dist'
    if (-not (Test-Path $guestDist)) {
        Write-Host '[build] plugin guest-js dist missing; running npm install + npm run build'
        Push-Location $guestJs
        try {
            npm install
            if ($LASTEXITCODE -ne 0) { throw 'plugin guest-js npm install failed' }
            npm run build
            if ($LASTEXITCODE -ne 0) { throw 'plugin guest-js npm run build failed' }
        }
        finally {
            Pop-Location
        }
    }
}

Ensure-RtspMosaicRepo

function Compute-BackendFingerprint {
    $root = [System.IO.Path]::GetFullPath($RepoRoot)
    $rootNormalized = ($root.TrimEnd([char]'\', [char]'/') + '\')
    $files = New-Object System.Collections.Generic.List[string]

    $backendDir = Join-Path $root 'backend'
    if (Test-Path $backendDir) {
        Get-ChildItem -Path $backendDir -Recurse -File |
            Where-Object { $_.FullName -notmatch '\\__pycache__\\' } |
            Sort-Object FullName |
            ForEach-Object { $files.Add($_.FullName) }
    }

    foreach ($extra in @('backend\requirements.txt', 'backend\main.spec', 'scripts\bundle_python.ps1')) {
        $p = Join-Path $root $extra
        if (Test-Path $p) { $files.Add($p) }
    }

    # Include selected local cv2 wheel metadata so wheel updates trigger rebundle.
    $wheelGlob = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
        'opencv_python_headless-*-win_arm64.whl'
    } else {
        'opencv_python_headless-*-win_amd64.whl'
    }
    $wheelDir = Join-Path $root 'vendor\cv2-wheels'
    if (Test-Path $wheelDir) {
        $wheel = Get-ChildItem -Path $wheelDir -Filter $wheelGlob -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($wheel) {
            $files.Add($wheel.FullName)
        }
    }

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        foreach ($file in $files) {
            if (-not (Test-Path $file)) { continue }
            $full = [System.IO.Path]::GetFullPath($file)
            if ($full.StartsWith($rootNormalized, [System.StringComparison]::OrdinalIgnoreCase)) {
                $rel = $full.Substring($rootNormalized.Length)
            } else {
                # Fallback for any path outside repo root; keep hash input stable.
                $rel = $full
            }
            $rel = $rel.Replace('\\', '/')
            $relBytes = [System.Text.Encoding]::UTF8.GetBytes($rel)
            [void]$sha.TransformBlock($relBytes, 0, $relBytes.Length, $null, 0)

            $bytes = [System.IO.File]::ReadAllBytes($file)
            [void]$sha.TransformBlock($bytes, 0, $bytes.Length, $null, 0)
        }
        [void]$sha.TransformFinalBlock([byte[]]::new(0), 0, 0)
        return ([System.BitConverter]::ToString($sha.Hash)).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

# --- Target triple --------------------------------------------------------
# Always build x86_64 packages -- the resulting installer runs on both
# x86_64 and ARM64 Windows (via emulation on the latter).
$TargetTriple = 'x86_64-pc-windows-msvc'
$HostTriple   = (rustc -vV | Select-String '^host:').ToString().Split(' ')[1]
Write-Host "[build] host triple:   $HostTriple"
Write-Host "[build] target triple: $TargetTriple"

# --- 3.1 Fetch bundled binaries -------------------------------------------
Write-Host "[build] === 3.1 fetch ffmpeg + go2rtc + mpv + yamnet ==="
& "$PSScriptRoot\fetch_ffmpeg.ps1" -Target $TargetTriple
if ($LASTEXITCODE -ne 0) { throw "fetch_ffmpeg failed" }
& "$PSScriptRoot\fetch_go2rtc.ps1" -Target $TargetTriple
if ($LASTEXITCODE -ne 0) { throw "fetch_go2rtc failed" }
& "$PSScriptRoot\fetch_mpv.ps1"
if ($LASTEXITCODE -ne 0) { throw "fetch_mpv failed" }
& "$PSScriptRoot\fetch_yamnet.ps1"
if ($LASTEXITCODE -ne 0) { throw "fetch_yamnet failed" }

# --- 3.2 Build tether supervisor ------------------------------------------
Write-Host "[build] === 3.2 build tether ==="
& "$PSScriptRoot\build_tether.ps1" -Target $TargetTriple
if ($LASTEXITCODE -ne 0) { throw "build_tether failed" }

# --- 3.3 Python venv + PyInstaller bundle ---------------------------------
Write-Host "[build] === 3.3 bundle python backend ==="

# On ARM64 Windows hosts, use an x86_64 Python 3.12 to create the venv so
# that all pip packages (numpy, opencv, onnxruntime) have pre-built wheels.
# The resulting PyInstaller bundle is x86_64 and runs via emulation on ARM64.
if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
    $PyExe = Resolve-X64Python312
    if (-not $PyExe) {
        throw 'ARM64 host requires an x64 Python 3.12 interpreter for .venv-x64. Install x64 Python 3.12, then rerun this script.'
    }
    Write-Host "[build] ARM64 host -- using x86_64 Python for backend bundle: $PyExe"
} else {
    $PyExe = 'python'
}

$VenvPath = Join-Path $RepoRoot '.venv-x64'
if ((Test-Path (Join-Path $VenvPath 'Scripts\python.exe')) -and ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') -and -not (Test-PythonMatches -PythonPath (Join-Path $VenvPath 'Scripts\python.exe') -ExpectedPlatform 'win-amd64' -ExpectedVersionPrefix '3.12')) {
    Write-Host '[build] Existing .venv-x64 is not x64 Python 3.12; recreating it.'
    Remove-Item -Recurse -Force $VenvPath
}

if (-not (Test-Path $VenvPath)) {
    Write-Host "[build] creating $VenvPath"
    & $PyExe -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) { throw "python -m venv failed" }
}

# Activate venv for the rest of this script only.
& (Join-Path $VenvPath 'Scripts\Activate.ps1')

if (-not $SkipVenvInstall) {
    Write-Host "[build] pip install backend\requirements.txt"
    python -m pip install --upgrade pip
    pip install -r (Join-Path $RepoRoot 'backend\requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
} else {
    Write-Host "[build] -SkipVenvInstall set, reusing existing venv packages"
}

$stampPath = Join-Path $RepoRoot 'src-tauri\binaries\.backend_bundle_fingerprint'
$outDir    = Join-Path $RepoRoot 'src-tauri\binaries\simplenvr-backend-dir'
$currFp    = Compute-BackendFingerprint
$prevFp    = if (Test-Path $stampPath) { (Get-Content $stampPath -Raw).Trim() } else { '' }

if ($ForceBundle) {
    Write-Host '[build] force bundle requested (-ForceBundle)'
    & "$PSScriptRoot\bundle_python.ps1" -VenvName '.venv-x64'
    if ($LASTEXITCODE -ne 0) { throw 'bundle_python failed' }
    Set-Content -Path $stampPath -Value $currFp -Encoding ASCII
} elseif (-not (Test-Path $outDir)) {
    Write-Host '[build] backend bundle missing; bundling Python backend'
    & "$PSScriptRoot\bundle_python.ps1" -VenvName '.venv-x64'
    if ($LASTEXITCODE -ne 0) { throw 'bundle_python failed' }
    Set-Content -Path $stampPath -Value $currFp -Encoding ASCII
} elseif ($currFp -ne $prevFp) {
    Write-Host '[build] backend inputs changed; rebundling Python backend'
    & "$PSScriptRoot\bundle_python.ps1" -VenvName '.venv-x64'
    if ($LASTEXITCODE -ne 0) { throw 'bundle_python failed' }
    Set-Content -Path $stampPath -Value $currFp -Encoding ASCII
} else {
    Write-Host '[build] backend unchanged; skipping rebundle'
}

# Deactivate venv so cargo tauri build sees the system python if it needs one.
if (Get-Command deactivate -ErrorAction SilentlyContinue) { deactivate }

# --- 3.5 Tauri build (frontend built via beforeBuildCommand) --------------
# Ensure frontend node_modules are present so tsc/vite are available when
# cargo tauri build runs its beforeBuildCommand ("npm run build").
Write-Host '[build] === 3.5 frontend npm install ==='
Push-Location (Join-Path $RepoRoot 'frontend')
try {
    npm install
    if ($LASTEXITCODE -ne 0) { throw 'frontend npm install failed' }
} finally {
    Pop-Location
}

Write-Host '[build] === 3.5 cargo tauri build ==='
# Add src-tauri to LIB so the linker finds mpv.lib
$env:LIB = (Join-Path $RepoRoot 'src-tauri') + ';' + $env:LIB
# tauri.windows.conf.json is auto-loaded by Tauri v2 on Windows builds,
# adding mpv-2.dll to bundle resources (the DLL doesn't exist on Mac/Linux).
cargo tauri build --target $TargetTriple
if ($LASTEXITCODE -ne 0) { throw "cargo tauri build failed" }

# --- Report output paths ---------------------------------------------------
if ($TargetTriple -eq $HostTriple) {
    $BundleDir = Join-Path $RepoRoot 'src-tauri\target\release\bundle'
} else {
    $BundleDir = Join-Path $RepoRoot "src-tauri\target\$TargetTriple\release\bundle"
}
$Msi  = Get-ChildItem -Path (Join-Path $BundleDir 'msi')  -Filter '*.msi' -ErrorAction SilentlyContinue | Select-Object -First 1
$Nsis = Get-ChildItem -Path (Join-Path $BundleDir 'nsis') -Filter '*-setup.exe' -ErrorAction SilentlyContinue | Select-Object -First 1

Write-Host ""
Write-Host "============================================================"
Write-Host "[build] Done."
if ($Msi)  { Write-Host "  MSI:  $($Msi.FullName)" }
if ($Nsis) { Write-Host "  NSIS: $($Nsis.FullName)" }
Write-Host ""
Write-Host "  Run whichever installer you prefer. NSIS is smaller and"
Write-Host "  uses the standard wizard; MSI integrates with group policy."
Write-Host "============================================================"
