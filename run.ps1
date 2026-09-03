<#
.SYNOPSIS
Sets up and runs ContextGate locally on Windows.

.DESCRIPTION
Creates .venv on first use, installs dependencies only when the requirements
files change, and forces ContextGate's safe local mode. The default task starts
the ContextGate web console on localhost. The lab task starts the legacy
Streamlit operator lab with its email prompt and usage telemetry disabled.

.PARAMETER Task
One of: app (default), lab, demo, acceptance, doctor, or test.

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
    [ValidateSet("app", "lab", "demo", "acceptance", "doctor", "test")]
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

function Get-ContextGateFileHashPrefix {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath
    )

    # Use .NET directly so the launcher also works when Windows PowerShell is
    # started from a hidden shortcut and its utility module is not auto-loaded.
    $Sha256 = [System.Security.Cryptography.SHA256]::Create()
    $Stream = [System.IO.File]::OpenRead($LiteralPath)
    try {
        $Bytes = $Sha256.ComputeHash($Stream)
        $Hex = [System.BitConverter]::ToString($Bytes).Replace("-", "")
        return $Hex.Substring(0, 16)
    } finally {
        $Stream.Dispose()
        $Sha256.Dispose()
    }
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
        Get-ContextGateFileHashPrefix -LiteralPath $File
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
$env:STREAMLIT_SERVER_SHOW_EMAIL_PROMPT = "false"
$env:STREAMLIT_BROWSER_GATHER_USAGE_STATS = "false"

$AppUrl = "http://127.0.0.1:8501"
$HealthUrl = "$AppUrl/api/health"

function Test-ContextGateWebConsole {
    $Response = $null
    try {
        # Keep this module-free because hidden Windows PowerShell shortcuts do
        # not always auto-load Microsoft.PowerShell.Utility.
        $Request = [System.Net.HttpWebRequest]::Create($HealthUrl)
        $Request.Method = "GET"
        $Request.Timeout = 2000
        $Request.ReadWriteTimeout = 2000
        $Request.Proxy = $null
        $Response = $Request.GetResponse()
        $StatusCode = [int]$Response.StatusCode
        if ($StatusCode -lt 200 -or $StatusCode -ge 300) {
            return $false
        }

        $Reader = [System.IO.StreamReader]::new($Response.GetResponseStream())
        try {
            $Content = $Reader.ReadToEnd()
        } finally {
            $Reader.Dispose()
        }
        return (
            $Content -match '"service"\s*:\s*"ContextGate"' -and
            $Content -match '"status"\s*:\s*"ok"'
        )
    } catch {
        return $false
    } finally {
        if ($null -ne $Response) {
            $Response.Dispose()
        }
    }
}

function Open-ContextGateBrowser {
    $StartInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $StartInfo.FileName = $AppUrl
    $StartInfo.UseShellExecute = $true
    [System.Diagnostics.Process]::Start($StartInfo) | Out-Null
}

switch ($Task) {
    "app" {
        if (Test-ContextGateWebConsole) {
            Write-Host "ContextGate is already running at $AppUrl"
            Open-ContextGateBrowser
            exit 0
        }
        & $VirtualPython -m context_gate.web_console
    }
    "lab" {
        & $VirtualPython -m streamlit run app.py `
            --server.address 127.0.0.1 `
            --server.maxUploadSize 10 `
            --server.showEmailPrompt false `
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
