# Download go2rtc release binaries and place into src-tauri/binaries/
# under Tauri sidecar naming (go2rtc-<triple>[.exe]).
#
# Usage:
#   scripts\fetch_go2rtc.ps1                  # auto-detect host triple
#   scripts\fetch_go2rtc.ps1 -Target <triple>
#   scripts\fetch_go2rtc.ps1 -All             # download all 6 targets
#
# go2rtc is MIT-licensed (https://github.com/AlexxIT/go2rtc).
# SHA256 is verified against pinned values in this script.

[CmdletBinding()]
param(
    [string]$Target,
    [switch]$All
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$BinDir   = Join-Path $RepoRoot 'src-tauri\binaries'
$ResDir   = Join-Path $RepoRoot 'src-tauri\resources'
$TmpDir   = Join-Path ([System.IO.Path]::GetTempPath()) ("fetch_go2rtc_" + [System.Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $TmpDir | Out-Null
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
New-Item -ItemType Directory -Force -Path $ResDir | Out-Null

# --- Pinned versions and SHA256 hashes -------------------------------------

$Go2rtcVersion = 'v1.9.14'
$Base = "https://github.com/AlexxIT/go2rtc/releases/download/$Go2rtcVersion"
$LicenseUrl = "https://raw.githubusercontent.com/AlexxIT/go2rtc/$Go2rtcVersion/LICENSE"

$Assets = @{
    'aarch64-apple-darwin'      = @{
        Url = "$Base/go2rtc_mac_arm64.zip"
        Sha = '919b78adc759d6b3883d1e1b2ac915ac0985bb903ff1897b4d228527bd64690c'
        Kind = 'zip-unix'
    }
    'x86_64-apple-darwin'       = @{
        Url = "$Base/go2rtc_mac_amd64.zip"
        Sha = '9b0b9a27a4dc3a5b8b93376e7e8fc2787c6af624a512842622be84aec0171c7a'
        Kind = 'zip-unix'
    }
    'x86_64-pc-windows-msvc'    = @{
        Url = "$Base/go2rtc_win64.zip"
        Sha = 'dd4167d75cb04abe618855b7c71f8658bd009f60c1a71835d134d2c11c939907'
        Kind = 'zip-exe'
    }
    'aarch64-pc-windows-msvc'   = @{
        Url = "$Base/go2rtc_win_arm64.zip"
        Sha = '814be0f6d8669025c7bccdd1f026ffaf613abae5352239f4ec84de543b94594a'
        Kind = 'zip-exe'
    }
    'x86_64-unknown-linux-gnu'  = @{
        Url = "$Base/go2rtc_linux_amd64"
        Sha = '32d616af226bd731678ffde328b94cfb94e30339bfefc469cfb76323144615a6'
        Kind = 'raw'
    }
    'aarch64-unknown-linux-gnu' = @{
        Url = "$Base/go2rtc_linux_arm64"
        Sha = '359fabade8a7a51e81a55fe6df6b0ef81764a5e1d63179577534eaaa71904b50'
        Kind = 'raw'
    }
}

function Detect-Triple {
    $arch = (Get-CimInstance -ClassName Win32_Processor | Select-Object -First 1).Architecture
    # 9 = x64, 12 = ARM64
    switch ($arch) {
        9  { return 'x86_64-pc-windows-msvc' }
        12 { return 'aarch64-pc-windows-msvc' }
        default { throw "Unsupported host architecture: $arch" }
    }
}

function Verify-Sha($Path, $Expected, $Name) {
    $actual = (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLower()
    if ($Expected -eq '__PIN_AFTER_FIRST_RUN__') {
        Write-Host "[fetch_go2rtc] WARNING: $Name SHA256 not pinned. Computed: $actual"
        return
    }
    if ($actual -ne $Expected.ToLower()) {
        throw "$Name SHA256 mismatch: expected $Expected, got $actual"
    }
    Write-Host "[fetch_go2rtc] $Name SHA256 OK"
}

function Install-Target($Triple) {
    if (-not $Assets.ContainsKey($Triple)) {
        throw "Unknown target triple: $Triple"
    }
    $asset = $Assets[$Triple]
    $url = $asset.Url
    $sha = $asset.Sha
    $kind = $asset.Kind
    $suffix = if ($kind -eq 'zip-exe') { '.exe' } else { '' }
    $earlyDest = Join-Path $BinDir "go2rtc-$Triple$suffix"
    if (Test-Path $earlyDest) {
        Write-Host "[fetch_go2rtc] $Triple already present, skipping download"
        return
    }
    Write-Host "[fetch_go2rtc] === $Triple ==="
    Write-Host "[fetch_go2rtc] downloading $url"

    $fileName = Split-Path $url -Leaf
    $dl = Join-Path $TmpDir $fileName
    Invoke-WebRequest -Uri $url -OutFile $dl -UseBasicParsing
    Verify-Sha $dl $sha "go2rtc($Triple)"

    switch ($kind) {
        'raw' {
            $dest = Join-Path $BinDir "go2rtc-$Triple"
            Copy-Item -Force $dl $dest
        }
        default {
            $extract = Join-Path $TmpDir "extract-$Triple"
            New-Item -ItemType Directory -Force -Path $extract | Out-Null
            Expand-Archive -Force -Path $dl -DestinationPath $extract
            if ($kind -eq 'zip-exe') {
                $found = Get-ChildItem -Path $extract -Recurse -Filter 'go2rtc*.exe' | Select-Object -First 1
                if (-not $found) { throw "no go2rtc*.exe in $dl" }
                $dest = Join-Path $BinDir "go2rtc-$Triple.exe"
            } else {
                $found = Get-ChildItem -Path $extract -Recurse -File |
                    Where-Object { $_.Name -like 'go2rtc*' -and $_.Extension -ne '.exe' } |
                    Select-Object -First 1
                if (-not $found) { throw "no go2rtc binary in $dl" }
                $dest = Join-Path $BinDir "go2rtc-$Triple"
            }
            Copy-Item -Force $found.FullName $dest
        }
    }
    # Strip Mark-of-the-Web so Attachment Manager doesn't block execution.
    if (Test-Path $dest) { Unblock-File -Path $dest }
    Write-Host "[fetch_go2rtc] installed $dest"
}

function Install-License {
    $dest = Join-Path $ResDir 'go2rtc-LICENSE'
    if (Test-Path $dest) { return }
    Write-Host "[fetch_go2rtc] fetching upstream LICENSE for $Go2rtcVersion"
    Invoke-WebRequest -Uri $LicenseUrl -OutFile $dest -UseBasicParsing
}

# --- Main ------------------------------------------------------------------

try {
    Install-License

    $targets = @()
    if ($All) {
        $targets = $Assets.Keys
    } elseif ($Target) {
        $targets = @($Target)
    } else {
        $targets = @(Detect-Triple)
    }

    foreach ($t in $targets) {
        Install-Target $t
    }

    Write-Host "[fetch_go2rtc] done. go2rtc binaries in $BinDir`:"
    Get-ChildItem $BinDir -Filter 'go2rtc*' | Format-Table Name, Length
} finally {
    if (Test-Path $TmpDir) { Remove-Item -Recurse -Force $TmpDir }
}
