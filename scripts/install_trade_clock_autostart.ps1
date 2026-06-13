param(
    [string]$Profile = "daily_production",
    [string]$TaskName = "Ashare Trade Clock Daemon"
)

# Registers a Windows Scheduled Task that auto-starts the always-on trade clock
# daemon at every logon, restarts it if it crashes, and keeps running on battery.
# The daemon itself suppresses system standby (SetThreadExecutionState) while
# alive, so the machine will not sleep as long as the clock is running.
#
# This is OS-level glue lost during the H: slim; it does not add a new
# governance/scheduler layer inside the codebase (net-zero respected).

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$startScript = Join-Path $PSScriptRoot "start_trade_clock.ps1"

if (-not (Test-Path $startScript)) {
    throw "Cannot find launcher: $startScript"
}

$psArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$startScript`" -Profile `"$Profile`""

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $psArgs -WorkingDirectory $repoRoot

# Start shortly after logon so the desktop/network is ready.
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = "PT1M"

# Run as the current interactive user, with highest privileges (GPU + broker access).
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -MultipleInstances IgnoreNew

# Wake the machine if it ever does sleep (defense in depth alongside keep-awake).
$settings.WakeToRun = $true

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

Write-Output "Installed scheduled task: $TaskName"
Write-Output "Launcher: $startScript -Profile $Profile"
Write-Output "Behavior:"
Write-Output "  - starts 1 min after each logon"
Write-Output "  - auto-restarts every 2 min if it crashes (up to 999 times)"
Write-Output "  - keeps running on battery; daemon suppresses standby while alive"
Write-Output "  - WakeToRun enabled as a fallback"
Write-Output ""
Write-Output "Start it now without waiting for next logon:"
Write-Output "  Start-ScheduledTask -TaskName `"$TaskName`""
Write-Output "Check daemon status:"
Write-Output "  powershell -File `"$(Join-Path $PSScriptRoot 'show_trade_clock_status.ps1')`""
