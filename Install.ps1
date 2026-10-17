$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $appRoot '启动.cmd'
$icon = Join-Path $appRoot 'assets\app.ico'
$name = '商品图自动生成'

Write-Host ''
Write-Host '=== 首次安装 ===' -ForegroundColor Cyan

# 1) 桌面快捷方式
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop ($name + '.lnk')
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $launcher
$shortcut.WorkingDirectory = $appRoot
$shortcut.Description = '本地商品图自动生成（1688 × 亚马逊）'
if (Test-Path -LiteralPath $icon) { $shortcut.IconLocation = "$icon,0" }
$shortcut.Save()
Write-Host "[1/3] 已创建桌面快捷方式：$shortcutPath" -ForegroundColor Green

# 2) 打开扩展目录，方便安装浏览器助手
$collector = Join-Path $appRoot 'collector'
if (Test-Path -LiteralPath $collector) {
    Start-Process explorer.exe $collector
    Write-Host "[2/3] 已打开浏览器助手目录：$collector" -ForegroundColor Green
}

# 3) 打开浏览器扩展管理页（优先 Edge）
$edge = @(
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
$chrome = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($edge) { Start-Process $edge 'edge://extensions/'; Write-Host '[3/3] 已打开 Edge 扩展管理页' -ForegroundColor Green }
elseif ($chrome) { Start-Process $chrome 'chrome://extensions/'; Write-Host '[3/3] 已打开 Chrome 扩展管理页' -ForegroundColor Green }
else { Write-Host '[3/3] 没有检测到 Edge 或 Chrome，请手动打开浏览器的扩展管理页' -ForegroundColor Yellow }

Write-Host ''
Write-Host '接下来做两件事（只做一次）：' -ForegroundColor Cyan
Write-Host '  1. 在刚打开的扩展管理页里，打开右上角“开发者模式”，点“加载解压缩的扩展”，'
Write-Host '     选择刚弹出的 collector 文件夹。'
Write-Host '  2. 双击桌面的“商品图自动生成”，在网页右上角“更多设置”里填一次火山方舟 API Key。'
Write-Host ''
Write-Host '详细说明见本目录的「使用说明.txt」。'
