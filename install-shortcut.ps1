<#
.SYNOPSIS
Creates or refreshes the ContextGate Demo Kit on the current user's Desktop.

.DESCRIPTION
Copies the tested presentation artifacts into one Desktop folder and creates a
quiet Start ContextGate shortcut. The shortcut starts the repository's run.ps1
launcher in hidden Windows PowerShell, so a double-click performs normal
first-run setup and opens the local app without leaving an extra terminal
window. If the older Desktop ContextGate.lnk exists, it is refreshed to use the
same quiet launch behavior.

.EXAMPLE
.\install-shortcut.ps1

.EXAMPLE
.\install-shortcut.ps1 -ExampleReportStem "C:\path\to\contextgate-example-123456"
#>

[CmdletBinding()]
param(
    [Parameter()]
    [string]$ExampleReportStem = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$RunScript = Join-Path $ProjectRoot "run.ps1"
$Desktop = [Environment]::GetFolderPath("Desktop")
$WindowsRoot = [Environment]::GetEnvironmentVariable("SystemRoot")

if (-not (Test-Path -LiteralPath $RunScript -PathType Leaf)) {
    throw "ContextGate launcher was not found beside install-shortcut.ps1."
}
if ([string]::IsNullOrWhiteSpace($Desktop) -or -not (Test-Path -LiteralPath $Desktop -PathType Container)) {
    throw "The current user's Desktop folder could not be resolved."
}
if ([string]::IsNullOrWhiteSpace($WindowsRoot)) {
    throw "The Windows system directory could not be resolved."
}

$PowerShellPath = Join-Path $WindowsRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
if (-not (Test-Path -LiteralPath $PowerShellPath -PathType Leaf)) {
    throw "Windows PowerShell could not be found."
}

$KitSources = @(
    @{
        Source = Join-Path $ProjectRoot "docs\screenshots\01-dashboard.png"
        Name = "01-dashboard.png"
    },
    @{
        Source = Join-Path $ProjectRoot "docs\screenshots\02-red-explanation.png"
        Name = "02-red-explanation.png"
    },
    @{
        Source = Join-Path $ProjectRoot "docs\screenshots\03-correction-learned.png"
        Name = "03-correction-learned.png"
    },
    @{
        Source = Join-Path $ProjectRoot "docs\screenshots\04-event-intelligence.png"
        Name = "04-event-intelligence.png"
    },
    @{
        Source = Join-Path $ProjectRoot "docs\screenshots\05-website-sources.png"
        Name = "05-website-sources.png"
    },
    @{
        Source = Join-Path $ProjectRoot "docs\screenshots\06-calendar.png"
        Name = "06-calendar.png"
    },
    @{
        Source = Join-Path $ProjectRoot "docs\screenshots\07-sales-intelligence.png"
        Name = "07-sales-intelligence.png"
    },
    @{
        Source = Join-Path $ProjectRoot "docs\demo_script.md"
        Name = "Presentation Script.md"
    },
    @{
        Source = Join-Path $ProjectRoot "docs\judge_start_here.md"
        Name = "Judge Guide.md"
    },
    @{
        Source = Join-Path $ProjectRoot "README.md"
        Name = "README.md"
    },
    @{
        Source = Join-Path $ProjectRoot "docs\company_quickstart.md"
        Name = "Company Quickstart.md"
    }
)

$MissingSource = $KitSources | Where-Object {
    -not (Test-Path -LiteralPath $_.Source -PathType Leaf)
} | Select-Object -First 1
if ($null -ne $MissingSource) {
    throw "A required Demo Kit source file is missing: $($MissingSource.Name)"
}

$DemoKitPath = Join-Path $Desktop "ContextGate Demo Kit"
New-Item -ItemType Directory -Path $DemoKitPath -Force | Out-Null

foreach ($Item in $KitSources) {
    Copy-Item -LiteralPath $Item.Source -Destination (Join-Path $DemoKitPath $Item.Name) -Force
}

if (-not [string]::IsNullOrWhiteSpace($ExampleReportStem)) {
    $ResolvedReportStem = [System.IO.Path]::GetFullPath($ExampleReportStem)
    $ExampleReportSources = @(
        @{
            Source = "$ResolvedReportStem-report.docx"
            Name = "ContextGate Example Report.docx"
        },
        @{
            Source = "$ResolvedReportStem-report.pdf"
            Name = "ContextGate Example Report.pdf"
        },
        @{
            Source = "$ResolvedReportStem-pie-chart.png"
            Name = "ContextGate Example Pie Chart.png"
        }
    )

    $MissingReport = $ExampleReportSources | Where-Object {
        -not (Test-Path -LiteralPath $_.Source -PathType Leaf)
    } | Select-Object -First 1
    if ($null -ne $MissingReport) {
        throw "An example report file is missing: $($MissingReport.Source)"
    }

    $ExampleReportsPath = Join-Path $DemoKitPath "Example Reports"
    New-Item -ItemType Directory -Path $ExampleReportsPath -Force | Out-Null
    foreach ($Item in $ExampleReportSources) {
        Copy-Item -LiteralPath $Item.Source -Destination (Join-Path $ExampleReportsPath $Item.Name) -Force
    }
}

$ShortcutPaths = @((Join-Path $DemoKitPath "Start ContextGate.lnk"))
$LegacyShortcutPath = Join-Path $Desktop "ContextGate.lnk"
if (Test-Path -LiteralPath $LegacyShortcutPath -PathType Leaf) {
    $ShortcutPaths += $LegacyShortcutPath
}

$Shell = $null
$Shortcut = $null
try {
    $Shell = New-Object -ComObject "WScript.Shell"
    foreach ($ShortcutPath in $ShortcutPaths) {
        $Shortcut = $Shell.CreateShortcut($ShortcutPath)
        $Shortcut.TargetPath = $PowerShellPath
        $Shortcut.Arguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RunScript`" -Task app"
        $Shortcut.WorkingDirectory = $ProjectRoot
        $Shortcut.Description = "Start ContextGate locally without an extra terminal window"
        $Shortcut.IconLocation = "$PowerShellPath,0"
        $Shortcut.WindowStyle = 7
        $Shortcut.Save()
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($Shortcut)
        $Shortcut = $null
    }
} finally {
    if ($null -ne $Shortcut) {
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($Shortcut)
    }
    if ($null -ne $Shell) {
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($Shell)
    }
}

Write-Host "ContextGate Demo Kit is ready: $DemoKitPath"
