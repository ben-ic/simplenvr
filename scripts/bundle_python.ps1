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

pip install --quiet 'pyinstaller>=6.0'

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

Write-Host "Bundle ready at: $FinalOut\"
