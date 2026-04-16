
[CmdletBinding()]
param(
    [switch]$WithCv2,
    [switch]$SkipVenvInstall,
    [switch]$ForceBundle
)

# Detect if running inside a VS Developer Command Prompt
function Test-IsVSDevShell {
    return [bool]($env:VSCMD_ARG_TGT_ARCH -or $env:VisualStudioVersion)
}

function Get-VSWherePath {
    foreach ($candidate in @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft Visual Studio\Installer\vswhere.exe')
    )) {
        if ($candidate -and (Test-Path $candidate)) {
            return $candidate
        }
    }
    return $null
}

function Get-VSInstallPath {
    $vswhere = Get-VSWherePath
    if (-not $vswhere) {
        return $null
    }

    $installPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (-not $installPath) {
        $installPath = & $vswhere -latest -products * -property installationPath
    }

    if ($installPath) {
        return ($installPath | Select-Object -First 1).Trim()
    }

    return $null
}

# Ensure NMake and CMake are available (from Visual Studio Community C++ tools)

function Import-VSBuildEnvironment {
    $vsInstallPath = Get-VSInstallPath
    if (-not $vsInstallPath) {
        Write-Host 'No Visual Studio installation found by vswhere.'
        return $false
    }

    $commands = @(
        @{ Path = (Join-Path $vsInstallPath 'Common7\Tools\VsDevCmd.bat'); Args = '-arch=x64 -host_arch=x64' },
        @{ Path = (Join-Path $vsInstallPath 'VC\Auxiliary\Build\vcvarsall.bat'); Args = 'x64' }
    )

    foreach ($command in $commands) {
        if (-not (Test-Path $command.Path)) {
            continue
        }

        Write-Host "Importing VS build environment: $($command.Path) $($command.Args)"
        $envDump = & cmd.exe /d /s /c "call `"$($command.Path)`" $($command.Args) >nul 2>&1 && set"
        if (-not $envDump) {
            continue
        }

        $hasDevShellMarker = $envDump | Where-Object {
            $_ -match '^VSCMD_ARG_TGT_ARCH=' -or $_ -match '^VisualStudioVersion='
        } | Select-Object -First 1
        if (-not $hasDevShellMarker) {
            continue
        }

        foreach ($line in $envDump) {
            if ($line -match '^(.*?)=(.*)$') {
                Set-Item -Path "Env:$($matches[1])" -Value $matches[2]
            }
        }

        return $true
    }

    Write-Host "Could not find or import VsDevCmd.bat or vcvarsall.bat in $vsInstallPath."
    return $false
}

if (-not (Test-IsVSDevShell)) {
    Write-Host 'Not running in a VS Developer Command Prompt. Importing the Visual Studio build environment into this PowerShell session...'
    if (-not (Import-VSBuildEnvironment) -or -not (Test-IsVSDevShell)) {
        Write-Error 'Could not initialize the Visual Studio developer environment automatically. Install Visual Studio Build Tools or Community with the Desktop development with C++ workload.'
        exit 1
    }
}

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
            Write-Host "$ToolName still not found after installing. Attempting to import VS build environment..."
            Import-VSBuildEnvironment
            try {
                $tool = Get-Command $ToolName -ErrorAction Stop
                Write-Host "$ToolName found after importing VS build environment: $($tool.Source)"
            } catch {
                Write-Error "$ToolName still not found after installing Visual Studio Build Tools and importing the build environment. Please ensure Visual Studio Community with the Desktop development with C++ workload is installed and available in your PATH."
                exit 1
            }
        }
    }
}

Ensure-Tool -ToolName 'nmake' -WingetId 'Microsoft.VisualStudio.2022.BuildTools' -InstallArgs '--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.Windows10SDK.19041 --add Microsoft.VisualStudio.Component.VC.CMake.Project --includeRecommended'
Ensure-Tool -ToolName 'cmake' -WingetId 'Microsoft.VisualStudio.2022.BuildTools' -InstallArgs '--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.Windows10SDK.19041 --add Microsoft.VisualStudio.Component.VC.CMake.Project --includeRecommended'

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "[win-all] === $Text ==="
}

function Fail([string]$Message) {
    Write-Error "[win-all] $Message"
    exit 1
}

function Test-IsAdmin {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Refresh-PathFromRegistry {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

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
        Write-Host "[win-all] ARM64 host: x64 Python 3.12 already available at $x64Python"
        return
    }

    Write-Host '[win-all] ARM64 host: installing x64 Python 3.12 for .venv-x64 builds'
    & winget install --id Python.Python.3.12 -e --architecture x64 --accept-source-agreements --accept-package-agreements --force
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {
        Fail "failed to install x64 Python 3.12 (winget exit $LASTEXITCODE)."
    }

    Refresh-PathFromRegistry
    $x64Python = Resolve-X64Python312
    if (-not $x64Python) {
        Fail 'x64 Python 3.12 still not found after winget install. Install it manually, then rerun this script.'
    }

    Write-Host "[win-all] ARM64 host: verified x64 Python 3.12 at $x64Python"
}

function Install-WingetPackage {
    param(
        [Parameter(Mandatory)] [string]$Id,
        [string]$Override
    )

    $listOut = & winget list --id $Id -e --accept-source-agreements 2>&1
    if ($LASTEXITCODE -eq 0 -and -not ($listOut -match 'No installed package found matching input criteria')) {
        Write-Host "[win-all] already installed: $Id (skipping)"
        return
    }

    Write-Host "[win-all] winget install $Id"
    $args = @(
        'install', '--id', $Id, '-e',
        '--accept-source-agreements', '--accept-package-agreements'
    )
    if ($Override) {
        $args += @('--override', $Override)
    }

    & winget @args
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {
        throw "winget install failed for $Id with exit code $LASTEXITCODE"
    }
}

function Ensure-WindowsPrereqs {
    Write-Step 'installing prerequisites via winget'

    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail "winget not found. Install 'App Installer' from Microsoft Store and retry."
    }

    Install-WingetPackage -Id 'Git.Git'
    Install-WingetPackage -Id 'OpenJS.NodeJS.LTS'
    Install-WingetPackage -Id 'Python.Python.3.12'
    Ensure-X64Python312OnArm64
    # Visual Studio Community C++ tools provide CMake; no Kitware.CMake install needed
    Install-WingetPackage -Id 'Rustlang.Rustup'
    Install-WingetPackage -Id 'Microsoft.VisualStudio.2022.BuildTools' -Override '--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended'
    Install-WingetPackage -Id 'Microsoft.EdgeWebView2Runtime'

    Refresh-PathFromRegistry
}

function Import-VsDevEnvironment {
    Write-Step 'loading VS 2022 C++ build environment'

    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path $vswhere)) {
        Fail "vswhere not found at $vswhere. Install VS 2022 Build Tools and retry."
    }

    $installPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (-not $installPath) {
        Fail 'Could not locate a VS installation with VC tools via vswhere.'
    }

    $vsDevCmd = Join-Path $installPath 'Common7\Tools\VsDevCmd.bat'
    if (-not (Test-Path $vsDevCmd)) {
        Fail "VsDevCmd.bat not found at $vsDevCmd"
    }

    $setOutput = & cmd.exe /d /s /c "call `"$vsDevCmd`" -arch=x64 -host_arch=x64 >nul && set"
    foreach ($line in $setOutput) {
        $idx = $line.IndexOf('=')
        if ($idx -gt 0) {
            $name = $line.Substring(0, $idx)
            $value = $line.Substring($idx + 1)
            [Environment]::SetEnvironmentVariable($name, $value, 'Process')
        }
    }

    if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
        Fail 'cl.exe is still not available after importing VsDevCmd environment.'
    }

    $libPaths = @($env:LIB -split ';' | Where-Object { $_ -and (Test-Path $_) })
    $requiredLibs = @('vcruntime.lib', 'libcmt.lib', 'ucrt.lib')
    $missingLibs = @()
    foreach ($lib in $requiredLibs) {
        $found = $false
        foreach ($libPath in $libPaths) {
            if (Test-Path (Join-Path $libPath $lib)) {
                $found = $true
                break
            }
        }
        if (-not $found) {
            $missingLibs += $lib
        }
    }

    if ($missingLibs.Count -gt 0) {
        $repairCmd = 'winget install --id Microsoft.VisualStudio.2022.BuildTools -e --force --accept-source-agreements --accept-package-agreements --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.Windows10SDK.19041 --includeRecommended"'
        Fail ("VS build environment is missing required linker libraries: {0}.`nRun this in an elevated PowerShell, then rerun this script:`n{1}" -f ($missingLibs -join ', '), $repairCmd)
    }
}

function Ensure-RustToolchainMatchesVsEnv {
    Write-Step 'aligning Rust toolchain with VS x64 environment'

    if (-not (Get-Command rustup -ErrorAction SilentlyContinue)) {
        Fail 'rustup not found on PATH. Reopen shell after Rust installation and retry.'
    }

    $hostTriple = (rustc -vV | Select-String '^host:').ToString().Split(' ')[1]
    if ($hostTriple -eq 'aarch64-pc-windows-msvc') {
        $x64Toolchain = 'stable-x86_64-pc-windows-msvc'
        Write-Host "[win-all] ARM64 Rust host detected; switching this shell to $x64Toolchain to match VS x64 libraries."

        rustup toolchain install $x64Toolchain --force-non-host
        if ($LASTEXITCODE -ne 0) {
            Fail "rustup toolchain install $x64Toolchain --force-non-host failed."
        }

        [Environment]::SetEnvironmentVariable('RUSTUP_TOOLCHAIN', $x64Toolchain, 'Process')

        $effectiveHost = (rustc -vV | Select-String '^host:').ToString().Split(' ')[1]
        if ($effectiveHost -ne 'x86_64-pc-windows-msvc') {
            Fail "Failed to activate x64 Rust toolchain in current shell (effective host: $effectiveHost)."
        }
    }
}

function Ensure-CargoTauri {
    Write-Step 'ensuring cargo-tauri exists'

    if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
        Fail 'cargo not found on PATH. Rustup install may require a new shell; rerun this script.'
    }

    if (-not (Get-Command cargo-tauri -ErrorAction SilentlyContinue)) {
        $cargoInstallArgs = @('install', 'tauri-cli', '--version', '^2.0', '--locked')
        $rustHostTriple = (rustc -vV | Select-String '^host:').ToString().Split(' ')[1]

        if ($rustHostTriple -eq 'aarch64-pc-windows-msvc') {
            Write-Host '[win-all] ARM64 host detected; installing cargo-tauri for x86_64 to match VS x64 toolchain environment.'
            rustup target add x86_64-pc-windows-msvc
            if ($LASTEXITCODE -ne 0) {
                Fail 'rustup target add x86_64-pc-windows-msvc failed.'
            }
            $cargoInstallArgs += @('--target', 'x86_64-pc-windows-msvc')
        }

        cargo @cargoInstallArgs
        if ($LASTEXITCODE -ne 0) {
            Fail 'cargo install tauri-cli failed.'
        }
    }

    cargo tauri --version | Out-Host
}

function Ensure-RtspMosaicRepo {
    Write-Step 'ensuring tauri-plugin-rtsp-mosaic checkout'

    $pluginRoot = Join-Path (Split-Path -Parent $RepoRoot) 'tauri-plugin-rtsp-mosaic'
    $pluginUrl  = 'https://github.com/ben-ic/tauri-plugin-rtsp-mosaic.git'

    if (-not (Test-Path (Join-Path $pluginRoot '.git'))) {
        git clone $pluginUrl $pluginRoot
    }

    if (-not (Test-Path (Join-Path $pluginRoot 'Cargo.toml'))) {
        Fail "Missing Cargo.toml in $pluginRoot"
    }

    $guestJs = Join-Path $pluginRoot 'guest-js'
    if (-not (Test-Path (Join-Path $guestJs 'package.json'))) {
        Fail "Missing guest-js package.json in $guestJs"
    }

    Push-Location $guestJs
    try {
        npm install
        if ($LASTEXITCODE -ne 0) { Fail 'npm install failed in plugin guest-js.' }
        npm run build
        if ($LASTEXITCODE -ne 0) { Fail 'npm run build failed in plugin guest-js.' }
    }
    finally {
        Pop-Location
    }
}

if (-not (Test-IsAdmin)) {
    Write-Host '[win-all] Not running elevated; continuing. If a package install later requires elevation, winget or the installer will report that directly.'
}

Ensure-WindowsPrereqs
Import-VsDevEnvironment
Ensure-RustToolchainMatchesVsEnv
Ensure-CargoTauri
Ensure-RtspMosaicRepo

if ($WithCv2) {
    Write-Step 'building LGPL-clean cv2 wheel (slow)'
    $venvX64 = Join-Path $RepoRoot '.venv-x64'
    $venvPython = Join-Path $venvX64 'Scripts\python.exe'
    $pyForCv2 = 'python'

    if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
        $pyForCv2 = Resolve-X64Python312
        if (-not $pyForCv2) {
            Fail 'ARM64 host requires an x64 Python 3.12 interpreter for -WithCv2. Install x64 Python 3.12, then rerun this script.'
        }
        Write-Host "[win-all] ARM64 host -- using x64 Python for cv2 wheel build: $pyForCv2"
    }

    if ((Test-Path $venvPython) -and ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') -and -not (Test-PythonMatches -PythonPath $venvPython -ExpectedPlatform 'win-amd64' -ExpectedVersionPrefix '3.12')) {
        Write-Host '[win-all] Existing .venv-x64 is not x64 Python 3.12; recreating it.'
        Remove-Item -Recurse -Force $venvX64
    }

    if (-not (Test-Path $venvX64)) {
        & $pyForCv2 -m venv $venvX64
        if ($LASTEXITCODE -ne 0) { Fail 'python -m venv .venv-x64 failed.' }
    }
    & (Join-Path $PSScriptRoot 'build_cv2_wheel.ps1') -VenvName '.venv-x64'
    if ($LASTEXITCODE -ne 0) { Fail 'build_cv2_wheel.ps1 failed.' }
}

Write-Step 'running Windows build pipeline'
$buildArgs = @()
if ($SkipVenvInstall) { $buildArgs += '-SkipVenvInstall' }
if ($ForceBundle) { $buildArgs += '-ForceBundle' }
& (Join-Path $PSScriptRoot 'build_windows.ps1') @buildArgs
if ($LASTEXITCODE -ne 0) { Fail 'build_windows.ps1 failed.' }

$bundleBase = Join-Path $RepoRoot 'src-tauri\target\x86_64-pc-windows-msvc\release\bundle'
if (-not (Test-Path $bundleBase)) {
    $bundleBase = Join-Path $RepoRoot 'src-tauri\target\release\bundle'
}

$exePath = Join-Path $RepoRoot 'src-tauri\target\x86_64-pc-windows-msvc\debug\app.exe'
if (-not (Test-Path $exePath)) {
    $exePath = Join-Path $RepoRoot 'src-tauri\target\debug\app.exe'
}

Write-Host ''
Write-Host '============================================================'
Write-Host '[win-all] Build complete.'
Write-Host "[win-all] Bundle output: $bundleBase"
Write-Host ''
Write-Host '[win-all] Run commands:'
Write-Host '  1) Dev mode (from this same shell):'
Write-Host '     cargo tauri dev --target x86_64-pc-windows-msvc'
Write-Host '  2) Launch built app binary directly (if present):'
Write-Host "     & \"$exePath\""
Write-Host '  3) Install and run packaged app:'
Write-Host "     Get-ChildItem \"$bundleBase\\nsis\" -Filter *-setup.exe | Select-Object -First 1 | ForEach-Object { Start-Process $_.FullName }"
Write-Host '============================================================'
