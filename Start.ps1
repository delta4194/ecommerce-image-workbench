$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$port = 3788
$url = "http://127.0.0.1:$port"

function Find-Python {
    $local = Join-Path $appRoot 'python\python.exe'
    if (Test-Path -LiteralPath $local) { return $local }
    $bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $bundled) { return $bundled }
    foreach ($name in @('python', 'python3', 'py')) {
        $found = Get-Command $name -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
    }
    return $null
}

$runtime = Find-Python
if (-not $runtime) {
    Write-Host ''
    Write-Host '没有找到可用的 Python。' -ForegroundColor Red
    Write-Host '请先安装 Python 3.10 以上版本（勾选 Add Python to PATH），装完再双击本文件。'
    Write-Host '下载地址： https://www.python.org/downloads/windows/'
    Read-Host '按回车关闭'
    exit 1
}

# 依赖检查：Pillow
$pillow = & $runtime -c "import PIL, sys; print(PIL.__version__)" 2>$null
if (-not $pillow) {
    Write-Host '缺少 Pillow，正在尝试自动安装…' -ForegroundColor Yellow
    & $runtime -m pip install --quiet --disable-pip-version-check Pillow
    $pillow = & $runtime -c "import PIL; print(PIL.__version__)" 2>$null
    if (-not $pillow) {
        Write-Host 'Pillow 安装失败：请手动执行  python -m pip install Pillow  后重试。' -ForegroundColor Red
        Read-Host '按回车关闭'
        exit 1
    }
}

$alreadyRunning = $false
try {
    $info = Invoke-RestMethod -Uri "$url/api/bridge/state" -TimeoutSec 3
    if ($info.app -eq 'ecommerce-auto') { $alreadyRunning = $true }
} catch { }

if (-not $alreadyRunning) {
    $log = Join-Path $appRoot 'service.log'
    $errorLog = Join-Path $appRoot 'service-error.log'
    $server = Join-Path $appRoot 'server.py'
    $process = Start-Process -FilePath $runtime -ArgumentList ('"' + $server + '"') -WorkingDirectory $appRoot `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput $log -RedirectStandardError $errorLog
    $process.Id | Set-Content -LiteralPath (Join-Path $appRoot 'service.pid')
    $ready = $false
    for ($i = 0; $i -lt 25; $i++) {
        Start-Sleep -Milliseconds 400
        try {
            Invoke-RestMethod -Uri "$url/api/bridge/state" -TimeoutSec 3 | Out-Null
            $ready = $true
            break
        } catch { }
    }
    if (-not $ready) {
        Write-Host '启动失败，请查看本目录下的 service-error.log。' -ForegroundColor Red
        Write-Host '常见原因：程序被安全软件拦截，或文件夹没有写入权限（请解压到 D 盘等可写目录，不要放在 C:\Program Files）。'
        Read-Host '按回车关闭'
        exit 1
    }
}

Start-Process $url
Write-Host "已启动：$url" -ForegroundColor Green
