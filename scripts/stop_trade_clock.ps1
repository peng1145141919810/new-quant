param(
    [int]$WaitSeconds = 30,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$tradeClockRoot = Join-Path $repoRoot "data\trade_clock"
$runtimeRoot = Join-Path $tradeClockRoot "runtime"
$pidPath = Join-Path $runtimeRoot "clock_supervisor.pid"
$stopRequestPath = Join-Path $runtimeRoot "stop_request.json"

if (-not (Test-Path $pidPath)) {
    Write-Output "Trade clock PID file not found."
    exit 0
}

$pidValue = (Get-Content $pidPath -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
if (-not $pidValue) {
    Remove-Item $pidPath -ErrorAction SilentlyContinue
    Write-Output "Trade clock PID file was empty."
    exit 0
}

$proc = Get-Process -Id ([int]$pidValue) -ErrorAction SilentlyContinue
if ($proc) {
    $stopPayload = @{
        requested_at = (Get-Date).ToString("s")
        requested_by = $env:USERNAME
        pid = [int]$pidValue
    } | ConvertTo-Json
    Set-Content -Path $stopRequestPath -Value $stopPayload -Encoding UTF8
    Write-Output "Stop request written. PID=$pidValue"
    $deadline = (Get-Date).AddSeconds([Math]::Max($WaitSeconds, 1))
    do {
        Start-Sleep -Seconds 2
        $proc = Get-Process -Id ([int]$pidValue) -ErrorAction SilentlyContinue
    } while ($proc -and (Get-Date) -lt $deadline)
    if ($proc -and $Force) {
        Stop-Process -Id ([int]$pidValue) -Force
        Write-Output "Force stopped trade clock. PID=$pidValue"
    } elseif ($proc) {
        Write-Output "Trade clock is still running after waiting $WaitSeconds seconds."
        Write-Output "Use -Force if you need an immediate stop."
        exit 0
    } else {
        Write-Output "Trade clock stopped gracefully. PID=$pidValue"
    }
} else {
    Write-Output "Trade clock process not running. PID=$pidValue"
}

Remove-Item $pidPath -ErrorAction SilentlyContinue
Remove-Item $stopRequestPath -ErrorAction SilentlyContinue
