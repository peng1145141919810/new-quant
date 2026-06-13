#requires -Version 5.1
<#
.SYNOPSIS
  在当前 PowerShell session 内设置 UTF-8 环境，让 Python / supervisor / event_lake 日志的中文不再乱码。

.DESCRIPTION
  解决 Bug #5：PowerShell 默认 Console code page 是 GBK / CP936，supervisor 写入 UTF-8 的 log 在终端显示乱码。
  此脚本在当前 session 设置：
    - Console code page → 65001 (UTF-8)
    - $OutputEncoding → UTF8
    - 进程级 PYTHONIOENCODING → utf-8
  对所有跨 native exe → PowerShell pipe 的中文输出都生效。

.EXAMPLE
  PS> . H:\Ashare\scripts\dev_env_utf8.ps1
#>

# 注意要用 dot-source（前面加点空格）才能影响当前 session：
#   . H:\Ashare\scripts\dev_env_utf8.ps1
# 否则只影响子 shell。

chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding  = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding            = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING      = 'utf-8'
$env:PYTHONUNBUFFERED      = '1'

Write-Host '[ok] PowerShell session set to UTF-8.'
Write-Host '  - Console code page  : 65001'
Write-Host '  - $OutputEncoding    : UTF-8 (no BOM)'
Write-Host '  - $env:PYTHONIOENCODING : utf-8'
Write-Host '  - $env:PYTHONUNBUFFERED : 1'
Write-Host ''
Write-Host '验证：Get-Content "H:\Ashare\data\event_lake\logs\run_20260530.log" -Tail 5 -Encoding UTF8'
