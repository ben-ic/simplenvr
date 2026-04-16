# Ensure NMake is available (from Visual Studio Community C++ tools)
function Ensure-Tool {
    param(
        [string]$ToolName,
        [string]$WingetId,
        [string]$InstallArgs
    )
    try {
        $tool = Get-Command $ToolName -ErrorAction Stop
        Write-Host "$ToolName found: $($tool.Source)"
    } catch {
        Write-Host "$ToolName not found. Attempting to install Visual Studio Build Tools via winget..."
        $wingetCmd = "winget install --id $WingetId -e --force --accept-source-agreements --accept-package-agreements --override '$InstallArgs'"
        Write-Host $wingetCmd
        iex $wingetCmd
        # Refresh PATH
        $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
        $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
        $env:Path = "$machine;$user"
        try {
            $tool = Get-Command $ToolName -ErrorAction Stop
            Write-Host "$ToolName found after install: $($tool.Source)"
        } catch {
            Write-Error "$ToolName still not found after installing Visual Studio Build Tools. Please ensure Visual Studio Community with the Desktop development with C++ workload is installed and available in your PATH."
            exit 1
        }
    }
}

Ensure-Tool -ToolName 'nmake' -WingetId 'Microsoft.VisualStudio.2022.BuildTools' -InstallArgs '--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.Windows10SDK.19041 --add Microsoft.VisualStudio.Component.VC.CMake.Project --includeRecommended'
Ensure-Tool -ToolName 'cmake' -WingetId 'Microsoft.VisualStudio.2022.BuildTools' -InstallArgs '--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.Windows10SDK.19041 --add Microsoft.VisualStudio.Component.VC.CMake.Project --includeRecommended'
# One-time Windows machine setup for SimpleNVR development.
#
# Installs all prerequisites documented in BUILD-WINDOWS.md section 1:
#   - Git, Node.js LTS, Python 3.12, CMake
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

function Ensure-X64Python312OnArm64 {
    if ($env:PROCESSOR_ARCHITECTURE -ne 'ARM64') {
        return
    }

    $x64Python = Resolve-X64Python312
    if ($x64Python) {
        Write-Host "[setup] ARM64 host: x64 Python 3.12 already available at $x64Python"
        return
    }

    Write-Host '[setup] ARM64 host: installing x64 Python 3.12 for .venv-x64 builds'
    & winget install --id Python.Python.3.12 -e --architecture x64 --accept-source-agreements --accept-package-agreements --force
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {
        Write-Error "[setup] failed to install x64 Python 3.12 (winget exit $LASTEXITCODE)."
        exit 1
    }

    $x64Python = Resolve-X64Python312
    if (-not $x64Python) {
        Write-Error '[setup] x64 Python 3.12 still not found after winget install. Install it manually, then rerun setup_windows.ps1.'
        exit 1
    }

    Write-Host "[setup] ARM64 host: verified x64 Python 3.12 at $x64Python"
}

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

    $listOut = & winget list --id $Id -e --accept-source-agreements 2>&1
    if ($LASTEXITCODE -eq 0 -and -not ($listOut -match 'No installed package found matching input criteria')) {
        Write-Host "[setup] already installed: $Id (skipping)"
        return
    }

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
Ensure-X64Python312OnArm64
# Visual Studio Community C++ tools provide CMake; no Kitware.CMake install needed

# --- 1.2 Rust toolchain ----------------------------------------------------
Install-WingetPackage -Id 'Rustlang.Rustup'

# --- 1.3 VS 2022 Build Tools (VCTools workload) ----------------------------
# The --override string is passed verbatim to the VS installer so we get the
# C++ desktop workload (cl.exe + Windows SDK + CMake) without the full IDE.
Install-WingetPackage -Id 'Microsoft.VisualStudio.2022.BuildTools' `
    -Override '--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended'

# --- 1.4 WebView2 runtime --------------------------------------------------
Install-WingetPackage -Id 'Microsoft.EdgeWebView2Runtime'

# Try to install tauri-cli now if cargo is already visible in this shell.
# If rustup PATH isn't active yet, we keep the final instructions below.
$machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
$userPath    = [Environment]::GetEnvironmentVariable('Path', 'User')
$env:Path = "$machinePath;$userPath"
if (Get-Command cargo -ErrorAction SilentlyContinue) {
    Write-Host "[setup] cargo detected; installing tauri-cli (^2.0, --locked)"
    cargo install tauri-cli --version "^2.0" --locked
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[setup]   tauri-cli install returned exit code $LASTEXITCODE; you'll rerun after reopening shell."
    }
} else {
    Write-Host "[setup] cargo not yet on PATH in this shell; tauri-cli install deferred until new shell."
}

# --- End of pass 1 ---------------------------------------------------------
Write-Host ""
Write-Host "============================================================"
Write-Host "[setup] Pass 1 complete."
Write-Host ""
Write-Host "  Close this window and open a NEW 'Developer PowerShell for"
Write-Host "  VS 2022' (Start menu -> search 'Developer PowerShell')."
Write-Host ""
Write-Host "  In that new window, run:"
Write-Host "      cargo install tauri-cli --version `"^2.0`" --locked"
Write-Host "      cargo tauri --version"
Write-Host ""
Write-Host "  Then run .\scripts\build_windows.ps1 to build the installer."
Write-Host "============================================================"
