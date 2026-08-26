# menu.ps1 — tg115bot 快捷管理菜单（init.ps1 创建的开始菜单快捷方式入口）
#
# 双击「tg115bot」快捷方式进入；也可直接:
#   powershell -ExecutionPolicy Bypass -File scripts\menu.ps1
#
#Requires -Version 5.1
$ErrorActionPreference = "Continue"

$Dir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Dir
$Svc = Join-Path $Dir "scripts\service.ps1"

function Info($m) { Write-Host "[+] $m" -ForegroundColor Green }

Write-Host "╔══════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║   tg115bot 服务管理                   ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════╝" -ForegroundColor Cyan

while ($true) {
    Write-Host ""
    Write-Host "  1) 启动        2) 停止        3) 重启"
    Write-Host "  4) 状态        5) 跟踪日志    6) 重跑初始化"
    Write-Host "  q) 退出"
    $c = Read-Host "选择"
    switch ($c) {
        "1" { powershell -ExecutionPolicy Bypass -File $Svc start }
        "2" { powershell -ExecutionPolicy Bypass -File $Svc stop }
        "3" { powershell -ExecutionPolicy Bypass -File $Svc restart }
        "4" { powershell -ExecutionPolicy Bypass -File $Svc status }
        "5" { Info "Ctrl+C 退出跟踪，回到本菜单"; powershell -ExecutionPolicy Bypass -File $Svc log }
        "6" { powershell -ExecutionPolicy Bypass -File (Join-Path $Dir "scripts\init.ps1") }
        "q" { exit 0 }
        default { Write-Host "无效选择" -ForegroundColor Yellow }
    }
}
