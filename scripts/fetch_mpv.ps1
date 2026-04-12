# Download pre-built libmpv (mpv.lib + mpv-2.dll) for Windows x86_64.
#
# Source: https://github.com/ben-ic/libmpv-win64
# Built from official mpv-player/mpv source with Clang.
#
# Usage:
#   .\scripts\fetch_mpv.ps1

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$BinDir   = Join-Path $RepoRoot 'src-tauri\binaries'
$LibDir   = Join-Path $RepoRoot 'src-tauri'

# --- Pinned release ---
$Tag     = 'v0.41.0-da4789c-static'
$Base    = "https://github.com/ben-ic/libmpv-win64/releases/download/$Tag"
$LibUrl  = "$Base/mpv.lib"
$DllUrl  = "$Base/mpv-2.dll"
$LibSha  = '488ed297e4a75b468a6993605cf4624dc316eae0ec71700a9459f686abd2fd27'
$DllSha  = '3f0d7693fc9689d733b507d264d1acfaf3be0863b92b5043d6ce210f73b5363e'

function Write-Log([string]$msg) { Write-Host "[fetch_mpv] $msg" }

function Test-Sha256([string]$File, [string]$Expected, [string]$Name) {
    $actual = (Get-FileHash -Algorithm SHA256 -Path $File).Hash.ToLowerInvariant()
    if ($actual -ne $Expected.ToLowerInvariant()) {
        throw "[fetch_mpv] $Name SHA256 mismatch: expected $Expected, got $actual"
    }
    Write-Log "$Name SHA256 OK"
}

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

# mpv.lib (import library for linking)
$libDest = Join-Path $LibDir 'mpv.lib'
if (-not (Test-Path $libDest)) {
    Write-Log "downloading $LibUrl"
    Invoke-WebRequest -Uri $LibUrl -OutFile $libDest -UseBasicParsing
    Test-Sha256 $libDest $LibSha 'mpv.lib'
} else {
    Write-Log "mpv.lib already present, skipping download"
}

# mpv-2.dll (runtime shared library)
$dllDest = Join-Path $BinDir 'mpv-2.dll'
if (-not (Test-Path $dllDest)) {
    Write-Log "downloading $DllUrl"
    Invoke-WebRequest -Uri $DllUrl -OutFile $dllDest -UseBasicParsing
    Unblock-File -Path $dllDest
    Test-Sha256 $dllDest $DllSha 'mpv-2.dll'
} else {
    Write-Log "mpv-2.dll already present, skipping download"
}

# Also copy to src-tauri/ root so Tauri bundles it next to the exe.
$dllRoot = Join-Path $LibDir 'mpv-2.dll'
Copy-Item -Path $dllDest -Destination $dllRoot -Force
Write-Log "copied mpv-2.dll to $LibDir"

Write-Log "done. mpv.lib in $LibDir, mpv-2.dll in $BinDir and $LibDir"
