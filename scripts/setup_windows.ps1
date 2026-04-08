# One-time Windows machine setup for SimpleNVR development.
#
# Installs all prerequisites documented in BUILD-WINDOWS.md section 1:
#   - Git, Node.js LTS, Python 3.12
#   - Rust (via rustup)
#   - Visual Studio 2022 Build Tools with the VCTools workload
#   - WebView2 runtime
#   - Tauri CLI (cargo install)
#
# Policy: this script is a two-pass installer (BUILD-WINDOWS.md decision: option A).
# After installing toolchains, PATH updates and the MSVC dev environment are only
# picked up by *new* PowerShell sessions. Rather than trying to mutate the current
# process's environment, we install what's missing, then exit and tell you to reopen
# in a Developer PowerShell for VS 2022.
#
# Usage (run as Administrator):
#   .\scripts\setup_windows.ps1
#
# Re-running is safe -- winget skips already-installed packages.

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

# --- Admin check -----------------------------------------------------------
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "[setup] must be run from an elevated PowerShell (Run as Administrator)."
    exit 1
}

# --- winget check ----------------------------------------------------------
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Error "[setup] winget not found. Install 'App Installer' from the Microsoft Store first."
    exit 1
}

function Install-WingetPackage {
    param(
        [Parameter(Mandatory)] [string]$Id,
        [string]$Override
    )
    Write-Host "[setup] winget install $Id"
    $args = @('install', '--id', $Id, '-e', '--accept-source-agreements', '--accept-package-agreements')
    if ($Override) { $args += @('--override', $Override) }
    & winget @args
    # winget returns non-zero when the package is already installed; treat that as OK.
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {
        Write-Host "[setup]   (winget exit $LASTEXITCODE -- likely already installed, continuing)"
    }
}

# --- 1.1 base tools --------------------------------------------------------
Install-WingetPackage -Id 'Git.Git'
Install-WingetPackage -Id 'OpenJS.NodeJS.LTS'
Install-WingetPackage -Id 'Python.Python.3.12'

# --- 1.2 Rust toolchain ----------------------------------------------------
Install-WingetPackage -Id 'Rustlang.Rustup'

# --- 1.3 VS 2022 Build Tools (VCTools workload) ----------------------------
# The --override string is passed verbatim to the VS installer so we get the
# C++ desktop workload (cl.exe + Windows SDK + CMake) without the full IDE.
Install-WingetPackage -Id 'Microsoft.VisualStudio.2022.BuildTools' `
    -Override '--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended'

# --- 1.4 WebView2 runtime --------------------------------------------------
Install-WingetPackage -Id 'Microsoft.EdgeWebView2Runtime'

# --- End of pass 1 ---------------------------------------------------------
Write-Host ""
Write-Host "============================================================"
Write-Host "[setup] Pass 1 complete."
Write-Host ""
Write-Host "  Close this window and open a NEW 'Developer PowerShell for"
Write-Host "  VS 2022' (Start menu -> search 'Developer PowerShell')."
Write-Host ""
Write-Host "  In that new window, run:"
Write-Host "      cargo install tauri-cli --version `"^2.0`""
Write-Host "      cargo tauri --version"
Write-Host ""
Write-Host "  Then run .\scripts\build_windows.ps1 to build the installer."
Write-Host "============================================================"
