param(
    [string]$Profile = "daily_production",
    [string]$ExecutionMode = "",
    [ValidateSet("default", "on", "off")]
    [string]$PrecisionTrade = "default",
    [string]$LogRoot = "",
    [switch]$Foreground
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$localSettings = Join-Path $repoRoot "src\ashare\engine\local_settings.py"
$localSettingsExample = Join-Path $repoRoot "src\ashare\engine\local_settings.example.py"
$serviceScript = Join-Path $repoRoot "trade_clock_service.py"
$tradeClockRoot = Join-Path $repoRoot "data\trade_clock"
$runtimeRoot = Join-Path $tradeClockRoot "runtime"
$pidPath = Join-Path $runtimeRoot "clock_supervisor.pid"
$stopRequestPath = Join-Path $runtimeRoot "stop_request.json"
$effectiveLogRoot = if ($LogRoot) { $LogRoot } else { $runtimeRoot }
$stdoutPath = Join-Path $effectiveLogRoot "clock_supervisor.stdout.log"
$stderrPath = Join-Path $effectiveLogRoot "clock_supervisor.stderr.log"

New-Item -ItemType Directory -Force -Path $tradeClockRoot | Out-Null
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
New-Item -ItemType Directory -Force -Path $effectiveLogRoot | Out-Null
Remove-Item $stopRequestPath -ErrorAction SilentlyContinue

function Get-PythonFromSettings {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SettingsPath
    )

    if (-not (Test-Path $SettingsPath)) {
        return $null
    }

    $content = Get-Content $SettingsPath -Raw -Encoding UTF8
    $match = [regex]::Match($content, '(?m)^PYTHON_EXECUTABLE\s*=\s*r?["'']([^"'']+)["'']')
    if (-not $match.Success) {
        return $null
    }

    $candidate = $match.Groups[1].Value.Trim()
    if (-not $candidate) {
        return $null
    }
    if ($candidate -match '^[A-Za-z]:\\path\\to\\') {
        return $null
    }
    if (-not (Test-Path $candidate)) {
        return $null
    }

    return [pscustomobject]@{
        FilePath = $candidate
        Prefix = @()
        Source = $SettingsPath
    }
}

function Get-BootstrapPython {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pythonCommand) {
        return [pscustomobject]@{
            FilePath = $pythonCommand.Source
            Prefix = @()
            Source = "PATH:python.exe"
        }
    }

    $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pyLauncher) {
        return [pscustomobject]@{
            FilePath = $pyLauncher.Source
            Prefix = @("-3")
            Source = "PATH:py.exe -3"
        }
    }

    return $null
}

function Get-PythonFromImportedSettings {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SettingsPath
    )

    if (-not (Test-Path $SettingsPath)) {
        return $null
    }

    $bootstrap = Get-BootstrapPython
    if (-not $bootstrap) {
        return $null
    }

    $probe = @"
import importlib.util
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('codex_local_settings_probe', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
value = getattr(module, 'PYTHON_EXECUTABLE', '')
print(value or '')
"@

    $candidate = ""
    try {
        $output = & $bootstrap.FilePath @($bootstrap.Prefix + @("-c", $probe, $SettingsPath)) 2>$null
        if ($LASTEXITCODE -eq 0) {
            $candidate = ($output | Select-Object -First 1).Trim()
        }
    } catch {
        $candidate = ""
    }

    if (-not $candidate) {
        return $null
    }
    if ($candidate -match '^[A-Za-z]:\\path\\to\\') {
        return $null
    }
    if (-not (Test-Path $candidate)) {
        return $null
    }

    return [pscustomobject]@{
        FilePath = $candidate
        Prefix = @()
        Source = "$SettingsPath (imported)"
    }
}

function Resolve-ResearchPython {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LocalSettingsPath,
        [Parameter(Mandatory = $true)]
        [string]$ExampleSettingsPath
    )

    foreach ($settingsPath in @($LocalSettingsPath, $ExampleSettingsPath)) {
        $resolved = Get-PythonFromSettings -SettingsPath $settingsPath
        if ($resolved) {
            return $resolved
        }
    }

    $importResolved = Get-PythonFromImportedSettings -SettingsPath $LocalSettingsPath
    if ($importResolved) {
        return $importResolved
    }

    foreach ($envName in @("ASHARE_RESEARCH_PYTHON", "PYTHON_EXECUTABLE")) {
        $candidate = [Environment]::GetEnvironmentVariable($envName)
        if ($candidate -and (Test-Path $candidate)) {
            return [pscustomobject]@{
                FilePath = $candidate
                Prefix = @()
                Source = "env:$envName"
            }
        }
    }

    $bootstrap = Get-BootstrapPython
    if ($bootstrap) {
        return $bootstrap
    }

    throw "Unable to resolve research Python. Checked local_settings.py, local_settings.example.py, environment variables, python.exe, and py.exe."
}

if (Test-Path $pidPath) {
    $existingPid = (Get-Content $pidPath -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($existingPid) {
        $proc = Get-Process -Id ([int]$existingPid) -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Output "Trade clock already running. PID=$existingPid"
            exit 0
        }
    }
    Remove-Item $pidPath -ErrorAction SilentlyContinue
}

$pythonResolution = Resolve-ResearchPython -LocalSettingsPath $localSettings -ExampleSettingsPath $localSettingsExample
$python = $pythonResolution.FilePath

$args = @()
if ($pythonResolution.Prefix) {
    $args += $pythonResolution.Prefix
}
$args += @(
    $serviceScript,
    "--profile", $Profile,
    "--skip-preflight"
)
if ($ExecutionMode) {
    $args += @("--execution-mode", $ExecutionMode)
}
if ($PrecisionTrade -ne "default") {
    $args += @("--precision-trade", $PrecisionTrade)
}

if ($Foreground) {
    Write-Output "Starting trade clock in foreground. Profile=$Profile"
    Write-Output "Python: $python (source=$($pythonResolution.Source))"
    Write-Output "Stdout: $stdoutPath"
    Write-Output "Stderr: $stderrPath"
    & $python @args 2>> $stderrPath | Tee-Object -FilePath $stdoutPath
    exit $LASTEXITCODE
}

$proc = Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $repoRoot -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
Set-Content -Path $pidPath -Value $proc.Id -Encoding UTF8

Write-Output "Started trade clock. PID=$($proc.Id) Profile=$Profile"
Write-Output "Python: $python (source=$($pythonResolution.Source))"
Write-Output "Stdout: $stdoutPath"
Write-Output "Stderr: $stderrPath"
