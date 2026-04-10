# Bundle the SimpleNVR Python backend into a single-file executable
# via PyInstaller, named with the rustc target triple to match Tauri's
# externalBin convention. Windows equivalent of bundle_python.sh.
#
# Usage:
#   .\scripts\bundle_python.ps1                     # auto-detect host triple
#   .\scripts\bundle_python.ps1 -Target <triple>    # explicit triple
#   .\scripts\bundle_python.ps1 -VenvName .venv-x64 # custom venv dir

[CmdletBinding()]
param(
    [string]$Target,
    [string]$VenvName = '.venv'
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir
Set-Location $RepoRoot

# Always use the project venv — never install PyInstaller globally.
& (Join-Path $RepoRoot "$VenvName\Scripts\Activate.ps1")

pip install --quiet 'pyinstaller>=6.0'

if (-not $Target) {
    $Target = (rustc -vV | Select-String '^host:').ToString().Split(' ')[1]
}
$TargetTriple = $Target
$OutRoot = "src-tauri\binaries"
$OutName = "simplenvr-backend-$TargetTriple"

if (-not (Test-Path $OutRoot)) {
    New-Item -ItemType Directory -Path $OutRoot | Out-Null
}

$BuildDir   = "build\pyinstaller"
$RawOutDir  = Join-Path $OutRoot "simplenvr-backend"
$RawOutFile = Join-Path $OutRoot "simplenvr-backend.exe"
$FinalOut   = Join-Path $OutRoot "$OutName.exe"

foreach ($p in @($BuildDir, $RawOutDir, $RawOutFile, $FinalOut)) {
    if (Test-Path $p) {
        Remove-Item -Recurse -Force $p
    }
}

pyinstaller backend\main.spec `
    --distpath $OutRoot `
    --workpath $BuildDir `
    --noconfirm

# Onefile output on Windows is simplenvr-backend.exe; rename to triple-suffixed.
Move-Item $RawOutFile $FinalOut

Write-Host "Bundle ready at: $FinalOut"
