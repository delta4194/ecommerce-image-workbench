$appRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$stopped = 0

$pidFile = Join-Path $appRoot 'service.pid'
if (Test-Path -LiteralPath $pidFile) {
    $serverPid = (Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($serverPid) {
        try { Stop-Process -Id ([int]$serverPid) -Force -ErrorAction Stop; $stopped++ } catch { }
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

# 兜底：按命令行匹配本目录的 server.py
$pattern = [regex]::Escape($appRoot)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match $pattern -and $_.CommandLine -match 'server\.py' } |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $stopped++ } catch { } }

if ($stopped -gt 0) { Write-Host "已停止 $stopped 个进程。" -ForegroundColor Green }
else { Write-Host '程序当前没有在运行。' -ForegroundColor Yellow }
