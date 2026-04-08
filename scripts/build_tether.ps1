# Build the tether supervisor binary and install it under Tauri sidecar
# naming (tether-<triple>.exe) into src-tauri/binaries/.
#
# tether is our cross-platform parent-death child supervisor. Source:
# src-tauri/tether/. See src-tauri/tether/src/main.rs for the
# per-platform mechanism (PDEATHSIG on Linux, Job Object on Windows,
# stdin-EOF watchdog on macOS).
#
# Usage:
#   .\scripts\build_tether.ps1                  # auto-detect host triple
#   .\scripts\build_tether.ps1 -Target <triple>

[CmdletBinding()]
param(
    [string]$Target
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$BinDir   = Join-Path $RepoRoot 'src-tauri\binaries'
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

function Get-HostTriple {
    $rustcOut = rustc -vV 2>$null | Select-String '^host:'
    if (-not $rustcOut) {
        Write-Error "rustc not found — install Rust toolchain first (https://rustup.rs)"
        exit 1
    }
    return $rustcOut.ToString().Split(' ')[1]
}

if (-not $Target) {
    $Target = Get-HostTriple
}

Write-Host "[build_tether] building tether for $Target"

$HostTriple = Get-HostTriple
Push-Location (Join-Path $RepoRoot 'src-tauri')
try {
    if ($Target -eq $HostTriple) {
        # Native build — use default target dir.
        cargo build -p tether --release
        if ($LASTEXITCODE -ne 0) { throw "cargo build failed" }
        $SrcBin = Join-Path $RepoRoot 'src-tauri\target\release\tether.exe'
    } else {
        # Cross-compile — requires `rustup target add <triple>` beforehand.
        cargo build -p tether --release --target $Target
        if ($LASTEXITCODE -ne 0) { throw "cargo build failed" }
        $SrcBin = Join-Path $RepoRoot "src-tauri\target\$Target\release\tether.exe"
    }
}
finally {
    Pop-Location
}

# On non-Windows targets the binary has no .exe; handle both.
if (-not (Test-Path $SrcBin)) {
    $SrcBin = $SrcBin -replace '\.exe$', ''
    if (-not (Test-Path $SrcBin)) {
        Write-Error "[build_tether] built binary not found at expected path"
        exit 1
    }
}

$Ext = if ($Target -like '*windows*') { '.exe' } else { '' }
$Dest = Join-Path $BinDir "tether-$Target$Ext"

Copy-Item -Force $SrcBin $Dest
Write-Host "[build_tether] installed $Dest"
Get-Item $Dest | Format-Table Name, Length
