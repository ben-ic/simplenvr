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
# The script does NOT run the resulting installer — it just builds it.
# Output paths are printed at the end.

[CmdletBinding()]
param(
    [switch]$SkipVenvInstall  # set if you know backend\requirements.txt hasn't changed
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# --- Repo-root sanity check (BUILD-WINDOWS.md line 173 warning) -----------
if (-not (Test-Path (Join-Path $RepoRoot 'src-tauri\tauri.conf.json'))) {
    Write-Error "[build] must run from repo root. Expected src-tauri\tauri.conf.json."
    exit 1
}

# --- Prerequisite check ----------------------------------------------------
# Hard-fails if anything is missing, with a pointer to setup_windows.ps1.
# We check tools are on PATH, not versions — setup_windows.ps1 pins versions.
function Test-Prerequisites {
    $required = @(
        @{ Name = 'git';     Hint = 'setup §1.1' },
        @{ Name = 'node';    Hint = 'setup §1.1' },
        @{ Name = 'npm';     Hint = 'setup §1.1' },
        @{ Name = 'python';  Hint = 'setup §1.1' },
        @{ Name = 'rustc';   Hint = 'setup §1.2 — reopen shell after rustup install' },
        @{ Name = 'cargo';   Hint = 'setup §1.2 — reopen shell after rustup install' },
        @{ Name = 'cl';      Hint = 'setup §1.3 — must run from Developer PowerShell for VS 2022' },
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

# --- Host triple (for output path reporting) ------------------------------
$HostTriple = (rustc -vV | Select-String '^host:').ToString().Split(' ')[1]
Write-Host "[build] host triple: $HostTriple"

# --- 3.1 Fetch bundled binaries -------------------------------------------
Write-Host "[build] === 3.1 fetch ffmpeg + go2rtc ==="
& "$PSScriptRoot\fetch_ffmpeg.ps1"
if ($LASTEXITCODE -ne 0) { throw "fetch_ffmpeg failed" }
& "$PSScriptRoot\fetch_go2rtc.ps1"
if ($LASTEXITCODE -ne 0) { throw "fetch_go2rtc failed" }

# --- 3.2 Build tether supervisor ------------------------------------------
Write-Host "[build] === 3.2 build tether ==="
& "$PSScriptRoot\build_tether.ps1"
if ($LASTEXITCODE -ne 0) { throw "build_tether failed" }

# --- 3.3 Python venv + PyInstaller bundle ---------------------------------
Write-Host "[build] === 3.3 bundle python backend ==="
$VenvPath = Join-Path $RepoRoot '.venv'
if (-not (Test-Path $VenvPath)) {
    Write-Host "[build] creating .venv"
    python -m venv $VenvPath
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

& "$PSScriptRoot\bundle_python.ps1"
if ($LASTEXITCODE -ne 0) { throw "bundle_python failed" }

# Deactivate venv so cargo tauri build sees the system python if it needs one.
if (Get-Command deactivate -ErrorAction SilentlyContinue) { deactivate }

# --- 3.5 Tauri build (frontend built via beforeBuildCommand) --------------
Write-Host "[build] === 3.5 cargo tauri build (5-15 min on first run) ==="
cargo tauri build
if ($LASTEXITCODE -ne 0) { throw "cargo tauri build failed" }

# --- Report output paths ---------------------------------------------------
$BundleDir = Join-Path $RepoRoot 'src-tauri\target\release\bundle'
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
