# Download (or build) LGPL-only static FFmpeg + ffprobe and place into
# src-tauri/binaries/ under Tauri sidecar naming.
#
# Usage:
#   .\scripts\fetch_ffmpeg.ps1                  # auto-detect host triple
#   .\scripts\fetch_ffmpeg.ps1 -Target <triple>
#   .\scripts\fetch_ffmpeg.ps1 -All             # all 4 targets
#
# Sources match scripts/fetch_ffmpeg.sh:
#   Windows x86_64 — BtbN/FFmpeg-Builds LGPL static
#   Linux   x86_64 — BtbN/FFmpeg-Builds LGPL static
#   macOS          — built from FFmpeg source on a macOS host (PowerShell on
#                    macOS works, but the script shells out to clang/make)
#
# Aborts if any installed ffmpeg reports --enable-gpl / --enable-libx264 etc.

[CmdletBinding()]
param(
    [string]$Target,
    [switch]$All
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

$RepoRoot = Split-Path -Parent $PSScriptRoot
$BinDir   = Join-Path $RepoRoot 'src-tauri\binaries'
$TmpDir   = Join-Path ([System.IO.Path]::GetTempPath()) ("fetch_ffmpeg_" + [System.Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $TmpDir | Out-Null

# --- Pinned versions and SHA256 hashes (must match fetch_ffmpeg.sh) -------
$FFmpegSrcVersion = '8.1'
$FFmpegSrcUrl     = "https://ffmpeg.org/releases/ffmpeg-$FFmpegSrcVersion.tar.xz"
$FFmpegSrcSha256  = 'b072aed6871998cce9b36e7774033105ca29e33632be5b6347f3206898e0756a'

$BtbnBase       = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest'
$BtbnWinUrl     = "$BtbnBase/ffmpeg-master-latest-win64-lgpl.zip"
$BtbnWinSha256  = '3bbaf13d82c361c96eeb189b987494a64cc19689b5f2d3e4eb932f091cb0afa4'
$BtbnLinuxUrl   = "$BtbnBase/ffmpeg-master-latest-linux64-lgpl.tar.xz"
$BtbnLinuxSha256= '81b9788454df43eba32c3c91f7949cd857de7bd556946f28c615ffe850457d2d'

# --- Helpers ---------------------------------------------------------------

function Write-Log([string]$msg)  { Write-Host "[fetch_ffmpeg] $msg" }
function Die([string]$msg)        { Write-Error "[fetch_ffmpeg] ERROR: $msg"; exit 1 }

function Get-HostTriple {
    if ($IsWindows -or $env:OS -eq 'Windows_NT') { return 'x86_64-pc-windows-msvc' }
    if ($IsMacOS) {
        if ((uname -m) -eq 'arm64') { return 'aarch64-apple-darwin' }
        return 'x86_64-apple-darwin'
    }
    if ($IsLinux) { return 'x86_64-unknown-linux-gnu' }
    Die "unsupported host platform"
}

function Test-Native([string]$triple) {
    return ($triple -eq (Get-HostTriple))
}

function Save-File([string]$Url, [string]$Dest) {
    Write-Log "downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing
}

function Test-Sha256([string]$File, [string]$Expected, [string]$Name) {
    $actual = (Get-FileHash -Algorithm SHA256 -Path $File).Hash.ToLowerInvariant()
    if ($Expected -eq '__PIN_AFTER_FIRST_RUN__') {
        Write-Log "WARNING: $Name SHA256 not yet pinned. Computed: $actual"
        return
    }
    if ($actual -ne $Expected.ToLowerInvariant()) {
        Die "$Name SHA256 mismatch: expected $Expected, got $actual"
    }
    Write-Log "$Name SHA256 OK"
}

function Test-LgplBinary([string]$Bin) {
    $cfg = & $Bin -version 2>$null | Select-String -Pattern '^\s*configuration:'
    if (-not $cfg) { Die "$Bin produced no configuration: line" }
    $line = $cfg.Line
    Write-Log "${Bin}: $line"
    foreach ($flag in @('--enable-gpl','--enable-libx264','--enable-libx265','--enable-libfdk-aac')) {
        if ($line -match [regex]::Escape($flag)) {
            Die "GPL/non-redistributable contamination: $Bin has $flag"
        }
    }
    Write-Log "${Bin}: LGPL OK (no GPL flags)"
}

# --- Per-target installers -------------------------------------------------

function Install-BtbnZip([string]$Url, [string]$Sha256, [string]$Triple, [string]$Suffix) {
    $zip = Join-Path $TmpDir "ffmpeg-$Triple.zip"
    Save-File $Url $zip
    Test-Sha256 $zip $Sha256 "ffmpeg($Triple)"

    $extract = Join-Path $TmpDir $Triple
    New-Item -ItemType Directory -Force -Path $extract | Out-Null
    Expand-Archive -Path $zip -DestinationPath $extract -Force

    $bindir = Get-ChildItem -Path $extract -Recurse -Directory -Filter 'bin' | Select-Object -First 1
    if (-not $bindir) { Die "couldn't find bin/ in $Url" }

    Copy-Item (Join-Path $bindir.FullName "ffmpeg$Suffix")  (Join-Path $BinDir "ffmpeg-$Triple$Suffix")  -Force
    Copy-Item (Join-Path $bindir.FullName "ffprobe$Suffix") (Join-Path $BinDir "ffprobe-$Triple$Suffix") -Force
}

function Install-BtbnTarXz([string]$Url, [string]$Sha256, [string]$Triple) {
    $tar = Join-Path $TmpDir "ffmpeg-$Triple.tar.xz"
    Save-File $Url $tar
    Test-Sha256 $tar $Sha256 "ffmpeg($Triple)"

    $extract = Join-Path $TmpDir $Triple
    New-Item -ItemType Directory -Force -Path $extract | Out-Null
    & tar -xJf $tar -C $extract
    if ($LASTEXITCODE -ne 0) { Die "tar extract failed for $Url" }

    $bindir = Get-ChildItem -Path $extract -Recurse -Directory -Filter 'bin' | Select-Object -First 1
    if (-not $bindir) { Die "couldn't find bin/ in $Url" }

    Copy-Item (Join-Path $bindir.FullName 'ffmpeg')  (Join-Path $BinDir "ffmpeg-$Triple")  -Force
    Copy-Item (Join-Path $bindir.FullName 'ffprobe') (Join-Path $BinDir "ffprobe-$Triple") -Force
    if ($IsLinux -or $IsMacOS) {
        & chmod +x (Join-Path $BinDir "ffmpeg-$Triple") (Join-Path $BinDir "ffprobe-$Triple")
    }
}

function Build-Macos([string]$Triple) {
    if (-not $IsMacOS) { Die "macOS source build requires running on macOS host" }
    # Delegate to the bash script for the heavy lifting — it already
    # contains the configure / make logic.
    $sh = Join-Path $PSScriptRoot 'fetch_ffmpeg.sh'
    & bash $sh --target $Triple
    if ($LASTEXITCODE -ne 0) { Die "macOS source build failed" }
}

function Install-Target([string]$Triple) {
    switch ($Triple) {
        'aarch64-apple-darwin'    { Build-Macos $Triple }
        'x86_64-apple-darwin'     { Build-Macos $Triple }
        'x86_64-pc-windows-msvc'  { Install-BtbnZip   $BtbnWinUrl   $BtbnWinSha256   $Triple '.exe' }
        'x86_64-unknown-linux-gnu'{ Install-BtbnTarXz $BtbnLinuxUrl $BtbnLinuxSha256 $Triple }
        default { Die "unknown target triple: $Triple" }
    }
}

# --- Main ------------------------------------------------------------------

if ($All) {
    $targets = @(
        'aarch64-apple-darwin',
        'x86_64-apple-darwin',
        'x86_64-pc-windows-msvc',
        'x86_64-unknown-linux-gnu'
    )
} elseif ($Target) {
    $targets = @($Target)
} else {
    $targets = @((Get-HostTriple))
}

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

try {
    foreach ($triple in $targets) {
        Write-Log "=== $triple ==="
        Install-Target $triple

        $suffix = if ($triple -eq 'x86_64-pc-windows-msvc') { '.exe' } else { '' }
        $ff = Join-Path $BinDir "ffmpeg-$triple$suffix"
        $fp = Join-Path $BinDir "ffprobe-$triple$suffix"
        if (Test-Native $triple) {
            Test-LgplBinary $ff
            & $fp -version | Select-Object -First 1
        } else {
            Write-Log "skipping LGPL runtime check for non-native target $triple"
        }
    }
    Write-Log "done. Binaries in $BinDir"
    Get-ChildItem $BinDir | Format-Table Name, Length
}
finally {
    if (Test-Path $TmpDir) { Remove-Item -Recurse -Force $TmpDir }
}
