param(
    [string]$TaskName = "Ashare Trade Clock Daemon"
)

# Removes the trade-clock autostart scheduled task. Does NOT stop a currently
# running daemon; use stop_trade_clock.ps1 for that.

$ErrorActionPreference = "Stop"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Output "No scheduled task named '$TaskName' found. Nothing to remove."
    exit 0
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Output "Removed scheduled task: $TaskName"
Write-Output "Note: a running daemon is not stopped. To stop it now:"
Write-Output "  powershell -File `"$(Join-Path $PSScriptRoot 'stop_trade_clock.ps1')`""
