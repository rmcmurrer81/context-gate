<#
.SYNOPSIS
Sets up and runs ContextGate locally on Windows.

.DESCRIPTION
Creates .venv on first use, installs dependencies only when the requirements
files change, and forces ContextGate's safe local mode. The default task starts
the Streamlit application on localhost with usage telemetry disabled.

.PARAMETER Task
One of: app (default), demo, acceptance, doctor, or test.

.PARAMETER Dev
Install requirements-dev.txt instead of requirements.txt.

.PARAMETER SkipInstall
Skip dependency installation. Useful only when the virtual environment is
already prepared.

.EXAMPLE
.\run.ps1

.EXAMPLE
.\run.ps1 doctor

.EXAMPLE
.\run.ps1 acceptance

.EXAMPLE
.\run.ps1 test -Dev
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("app", "demo", "acceptance", "doctor", "test")]
    [string]$Task = "app",

    [switch]$Dev,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = $PSScriptRoot
$VirtualEnvironment = Join-Path $ProjectRoot ".venv"
$VirtualPython = Join-Path $VirtualEnvironment "Scripts\python.exe"
$Requirements = if ($Dev) {
    Join-Path $ProjectRoot "requirements-dev.txt"
} else {
    Join-Path $ProjectRoot "requirements.txt"
}

Set-Location -LiteralPath $ProjectRoot

if (-not (Test-Path -LiteralPath $VirtualPython -PathType Leaf)) {
    Write-Host "Creating local virtual environment in .venv ..."
    $Launcher = Get-Command "py" -ErrorAction SilentlyContinue
    if ($null -ne $Launcher) {
        & $Launcher.Source -3 -m venv $VirtualEnvironment
    } else {
        $Launcher = Get-Command "python" -ErrorAction SilentlyContinue
        if ($null -eq $Launcher) {
            throw "Python 3.11 or newer was not found. Install Python, then run this script again."
        }
        & $Launcher.Source -m venv $VirtualEnvironment
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Python could not create .venv (exit code $LASTEXITCODE)."
    }
}

if (-not (Test-Path -LiteralPath $VirtualPython -PathType Leaf)) {
    throw ".venv exists but its Python executable is missing. Recreate .venv and try again."
}

& $VirtualPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 'ContextGate requires Python 3.11 or newer.')"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

if (-not $SkipInstall) {
    $HashFiles = @((Join-Path $ProjectRoot "requirements.txt"))
    if ($Dev) {
        $HashFiles += $Requirements
    }
    $HashParts = foreach ($File in $HashFiles) {
        (Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash.Substring(0, 16)
    }
    $DependencyHash = $HashParts -join "-"
    $DependencyProfile = if ($Dev) { "dev" } else { "app" }
    $ReadyMarker = Join-Path $VirtualEnvironment ".context-gate-$DependencyProfile-$DependencyHash.ready"

    if (-not (Test-Path -LiteralPath $ReadyMarker -PathType Leaf)) {
        Write-Host "Installing $DependencyProfile dependencies ..."
        & $VirtualPython -m pip install --disable-pip-version-check -r $Requirements
        if ($LASTEXITCODE -ne 0) {
            throw "Dependency installation failed (exit code $LASTEXITCODE)."
        }
        New-Item -ItemType File -Path $ReadyMarker -Force | Out-Null
    }
}

# The public launchers are deliberately local-only. Workshop cloud integration
# remains an explicit, separate action and is never reached from this script.
$env:CONTEXTGATE_MODE = "local"
$env:PYTHONUTF8 = "1"

switch ($Task) {
    "app" {
        & $VirtualPython -m streamlit run app.py `
            --server.address 127.0.0.1 `
            --server.maxUploadSize 10 `
            --browser.gatherUsageStats false
    }
    "demo" {
        & $VirtualPython -m context_gate
    }
    "acceptance" {
        & $VirtualPython scripts/acceptance_matrix.py
    }
    "doctor" {
        & $VirtualPython scripts/doctor.py
    }
    "test" {
        & $VirtualPython -m pytest
    }
}

exit $LASTEXITCODE
