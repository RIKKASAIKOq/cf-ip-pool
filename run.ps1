#Requires -Version 5.0
<#
.SYNOPSIS
  CF IP Pool 一键启动 (Windows PowerShell)
.DESCRIPTION
  Termux / Linux / macOS 用 ./run；本脚本给 Windows。
.EXAMPLE
  .\run.ps1
  .\run.ps1 --check
  .\run.ps1 schedule install
  .\run.ps1 --target 3
  .\run.ps1 win
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Passthrough
)

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

Set-Location -LiteralPath $PSScriptRoot

# 吃掉 win / windows 这种显式平台参数
if ($Passthrough.Count -ge 1 -and @('win', 'windows') -contains $Passthrough[0].ToLower()) {
    $Passthrough = if ($Passthrough.Count -gt 1) { $Passthrough[1..($Passthrough.Count - 1)] } else { @() }
}

# 裸 schedule 映射成 main.py 认的 --schedule
if ($Passthrough.Count -ge 1 -and @('schedule', 'sched') -contains $Passthrough[0].ToLower()) {
    $Rest = if ($Passthrough.Count -gt 1) { @($Passthrough[1..($Passthrough.Count - 1)]) } else { @() }
    $Passthrough = @('--schedule') + $Rest
}

$py = $null
foreach ($cand in @('python', 'python3', 'python.exe', 'py')) {
    $c = Get-Command $cand -ErrorAction SilentlyContinue
    if ($c) { $py = $c; break }
}
if (-not $py) {
    Write-Host '[FAIL] 没找到 python，请先安装 Python 3.9+ 并勾选 Add to PATH' -ForegroundColor Red
    Write-Host '       https://www.python.org/downloads/'
    if ($Host.Name -eq 'ConsoleHost') { Read-Host '按回车退出' | Out-Null }
    exit 127
}
$PYEXE = $py.Source

Write-Host ''
Write-Host "  platform: Windows" -ForegroundColor DarkGray
Write-Host "  python  : $PYEXE" -ForegroundColor DarkGray
Write-Host "  entry   : $PSScriptRoot\main.py" -ForegroundColor DarkGray
if ($Passthrough.Count -gt 0) { Write-Host "  args    : $($Passthrough -join ' ')" -ForegroundColor DarkGray }
Write-Host ''

& $PYEXE main.py @Passthrough
$rc = $LASTEXITCODE
if ($rc -ne 0) {
    Write-Host ''
    Write-Host "[FAIL] 退出码 $rc，上面有具体原因" -ForegroundColor Red
    if ($Host.Name -eq 'ConsoleHost') { Read-Host '按回车退出' | Out-Null }
}
exit $rc
