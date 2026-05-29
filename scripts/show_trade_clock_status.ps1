param(
    [string]$Date = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$tradeClockRoot = Join-Path $repoRoot "data\trade_clock"
$runtimeRoot = Join-Path $tradeClockRoot "runtime"
$phaseStateRoot = Join-Path $tradeClockRoot "phase_state"
$releaseRoot = Join-Path $repoRoot "data\trade_release_v1"
$omsRoot = Join-Path $repoRoot "data\live_execution_bridge\oms_v1"
$automationRoot = Join-Path $repoRoot "outputs\automation_runs"

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        return $null
    }
    try {
        return Get-Content -Path $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Show-Block {
    param(
        [string]$Title,
        [hashtable]$Pairs
    )
    Write-Host ""
    Write-Host "[$Title]" -ForegroundColor Cyan
    foreach ($key in $Pairs.Keys) {
        Write-Host ("{0}: {1}" -f $key, $Pairs[$key])
    }
}

$clockStatePath = Join-Path $tradeClockRoot "clock_state.json"
$runtimeStatePath = Join-Path $runtimeRoot "scheduler_runtime.json"
$latestReleasePath = Join-Path $releaseRoot "latest_release.json"
$safetyStatePath = Join-Path $tradeClockRoot "system_safety_state.json"
$simOmsPath = Join-Path $omsRoot "simulation\snapshots\oms_summary.json"
$shadowOmsPath = Join-Path $omsRoot "shadow\snapshots\oms_summary.json"

if (-not $Date) {
    $Date = (Get-Date).ToString("yyyyMMdd")
}
$phaseStatePath = Join-Path $phaseStateRoot ("{0}.json" -f $Date)
$dailyPackPath = Join-Path $automationRoot $Date

$clockState = Read-JsonFile -Path $clockStatePath
$runtimeState = Read-JsonFile -Path $runtimeStatePath
$phaseState = Read-JsonFile -Path $phaseStatePath
$latestRelease = Read-JsonFile -Path $latestReleasePath
$safetyState = Read-JsonFile -Path $safetyStatePath
$simOms = Read-JsonFile -Path $simOmsPath
$shadowOms = Read-JsonFile -Path $shadowOmsPath

Write-Host "Ashare Trade Clock Status" -ForegroundColor Green
Write-Host ("Date: {0}" -f $Date)

Show-Block -Title "Service" -Pairs @{
    "clock_state" = $(if ($clockState) { "ok" } else { "missing" })
    "service_alive" = $(if ($clockState) { $clockState.service_alive } else { "" })
    "current_mode" = $(if ($clockState) { $clockState.current_mode } else { "" })
    "scheduler_profile" = $(if ($clockState) { $clockState.scheduler_profile } else { "" })
    "stop_requested" = $(if ($clockState) { $clockState.stop_requested } else { "" })
    "next_due_phase" = $(if ($clockState) { $clockState.next_due_phase } else { "" })
    "next_due_at" = $(if ($clockState) { $clockState.next_due_at } else { "" })
    "last_heartbeat_at" = $(if ($clockState) { $clockState.last_heartbeat_at } else { "" })
    "runtime_state" = $runtimeStatePath
}

Show-Block -Title "Release" -Pairs @{
    "latest_release" = $(if ($latestRelease) { $latestRelease.release_id } else { "missing" })
    "trade_date" = $(if ($latestRelease) { $latestRelease.trade_date } else { "" })
    "profile" = $(if ($latestRelease) { $latestRelease.profile } else { "" })
    "source_mode" = $(if ($latestRelease) { $latestRelease.source_mode } else { "" })
    "manifest_path" = $(if ($latestRelease) { $latestRelease.manifest_path } else { "" })
}

Show-Block -Title "Safety" -Pairs @{
    "system_mode" = $(if ($clockState) { $clockState.system_mode } else { "" })
    "market_regime" = $(if ($clockState) { $clockState.market_safety_regime } else { "" })
    "manual_halt" = $(if ($clockState) { $clockState.manual_halt } else { "" })
    "manual_reduce_only" = $(if ($clockState) { $clockState.manual_reduce_only } else { "" })
    "halt_reason" = $(if ($safetyState) { $safetyState.halt_reason } else { "" })
    "safety_state" = $safetyStatePath
}

Show-Block -Title "Daily Phase State" -Pairs @{
    "phase_state" = $(if ($phaseState) { $phaseStatePath } else { "missing" })
    "research" = $(if ($phaseState) { $phaseState.phases.research.status } else { "" })
    "release" = $(if ($phaseState) { $phaseState.phases.release.status } else { "" })
    "preopen_gate" = $(if ($phaseState) { $phaseState.phases.preopen_gate.status } else { "" })
    "simulation" = $(if ($phaseState) { $phaseState.phases.simulation.status } else { "" })
    "shadow" = $(if ($phaseState) { $phaseState.phases.shadow.status } else { "" })
    "summary" = $(if ($phaseState) { $phaseState.phases.summary.status } else { "" })
}

Show-Block -Title "OMS" -Pairs @{
    "simulation_oms" = $(if ($simOms) { $simOmsPath } else { "missing" })
    "simulation_namespace" = $(if ($simOms) { $simOms.execution_namespace } else { "" })
    "simulation_status" = $(if ($simOms) { $simOms.status } else { "" })
    "shadow_oms" = $(if ($shadowOms) { $shadowOmsPath } else { "missing" })
    "shadow_namespace" = $(if ($shadowOms) { $shadowOms.execution_namespace } else { "" })
    "shadow_status" = $(if ($shadowOms) { $shadowOms.status } else { "" })
}

Show-Block -Title "Daily Pack" -Pairs @{
    "automation_pack" = $(if (Test-Path $dailyPackPath) { $dailyPackPath } else { "missing" })
    "report_txt" = $(if (Test-Path (Join-Path $dailyPackPath "daily_report.txt")) { Join-Path $dailyPackPath "daily_report.txt" } else { "" })
    "report_md" = $(if (Test-Path (Join-Path $dailyPackPath "daily_report.md")) { Join-Path $dailyPackPath "daily_report.md" } else { "" })
}
