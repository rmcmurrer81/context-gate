<#
.SYNOPSIS
Creates or refreshes the ContextGate shortcut on the current user's Desktop.

.DESCRIPTION
Writes exactly one shortcut named ContextGate.lnk. The shortcut starts the
repository's run.ps1 launcher in Windows PowerShell, so a double-click opens
the local ContextGate app and performs its normal first-run setup when needed.

.EXAMPLE
.\install-shortcut.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$RunScript = Join-Path $ProjectRoot "run.ps1"

if (-not (Test-Path -LiteralPath $RunScript -PathType Leaf)) {
    throw "ContextGate launcher was not found at '$RunScript'. Keep install-shortcut.ps1 beside run.ps1 and try again."
}

$Desktop = [Environment]::GetFolderPath("Desktop")
if ([string]::IsNullOrWhiteSpace($Desktop) -or -not (Test-Path -LiteralPath $Desktop -PathType Container)) {
    throw "The current user's Desktop folder could not be resolved."
}

$WindowsRoot = [Environment]::GetEnvironmentVariable("SystemRoot")
if ([string]::IsNullOrWhiteSpace($WindowsRoot)) {
    throw "The Windows system directory could not be resolved."
}

$PowerShellPath = Join-Path $WindowsRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
if (-not (Test-Path -LiteralPath $PowerShellPath -PathType Leaf)) {
    throw "Windows PowerShell was not found at '$PowerShellPath'."
}

$ShortcutPath = Join-Path $Desktop "ContextGate.lnk"
$Shell = $null
$Shortcut = $null

try {
    $Shell = New-Object -ComObject "WScript.Shell"
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $PowerShellPath
    $Shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$RunScript`""
    $Shortcut.WorkingDirectory = $ProjectRoot
    $Shortcut.Description = "Start ContextGate locally"
    $Shortcut.IconLocation = "$PowerShellPath,0"
    $Shortcut.Save()
} finally {
    if ($null -ne $Shortcut) {
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($Shortcut)
    }
    if ($null -ne $Shell) {
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($Shell)
    }
}

Write-Host "ContextGate shortcut is ready: $ShortcutPath"
