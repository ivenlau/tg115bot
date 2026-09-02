# service.ps1 — tg115bot Windows 服务管理（后台运行 + PID 文件）
#
# 与 Linux 版 service.sh 对应：
#   .\scripts\service.ps1 start     启动（已在运行则提示）
#   .\scripts\service.ps1 stop      停止（先友好通知，10s 后强杀）
#   .\scripts\service.ps1 restart   重启（更新代码/配置后用这个）
#   .\scripts\service.ps1 status    查看运行状态
#   .\scripts\service.ps1 log [N]   跟踪运行日志（Ctrl+C 退出，N=尾部行数默认50）
#
# 说明:
#   - Start-Process -WindowStyle Hidden 后台运行，关终端不影响
#   - PID 记录在 run\tg115bot.pid，防重复启动
#   - 标准输出/错误追加到 logs\stdout.log（业务日志另在 logs\tg115bot.log 轮转）
#   - 自动使用项目目录下的 .venv（不存在则提示先跑 init.ps1）
#   - 开机自启：任务计划程序 schtasks /Create /SC ONSTART /TN tg115bot /
#       TR "powershell -ExecutionPolicy Bypass -File <本项目>\scripts\service.ps1 start"
#   - 兼容 Windows PowerShell 5.1 与 PowerShell 7（stdout.log 读写均为显式 UTF-8，
#     生成的 .cmd 按系统 OEM 代码页写入）
#
#Requires -Version 5.1
# 不用 "Stop"：taskkill 对不存在的 PID 写 stderr，PS 5.1 下配 Stop 会被
# 包装成 NativeCommandError 直接炸（见 init.ps1 同位置注释）
$ErrorActionPreference = "Continue"

$Dir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Dir

$PidFile    = Join-Path $Dir "run\tg115bot.pid"
$StdoutLog  = Join-Path $Dir "logs\stdout.log"

function Info($m) { Write-Host "[+] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "[x] $m" -ForegroundColor Red; exit 1 }

# ── Python 解释器：优先 venv ─────────────────────────────────────────────
$Python = Join-Path $Dir ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Warn "未找到 .venv（$Python）"
    Die "先运行: powershell -ExecutionPolicy Bypass -File scripts\init.ps1"
}

foreach ($d in @("run", "logs")) {
    if (-not (Test-Path (Join-Path $Dir $d))) { New-Item -ItemType Directory -Path (Join-Path $Dir $d) | Out-Null }
}

# ── PID 管理 ─────────────────────────────────────────────────────────────
function Get-LivePid {
    # 返回 PID（Int）或 $null：读 PID 文件 + 校验进程存活且命令行含本项目路径
    # （防 PID 复用误杀，对齐 service.sh 的 /proc cmdline 校验）
    if (-not (Test-Path $PidFile)) { return $null }
    $raw = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    $pidVal = 0
    if (-not $raw -or -not [int]::TryParse("$raw".Trim(), [ref]$pidVal)) { return $null }
    $proc = Get-Process -Id $pidVal -ErrorAction SilentlyContinue
    if (-not $proc) { return $null }
    # 校验命令行含本项目路径（PID 记的是 cmd 包装进程，命令行为
    # cmd /c "<项目>\.venv\...\python.exe main.py >> <项目>\logs\stdout.log 2>&1"，
    # 含 $Dir 即可判明身份；CIM 不可用时退化为只验进程名 cmd/python）
    $cmd = $null
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $pidVal" -ErrorAction Stop).CommandLine
    } catch {
        $cmd = if ($proc.ProcessName -match "cmd|python") { "$Dir main.py" } else { $null }
    }
    if (-not $cmd -or ($cmd -notmatch [regex]::Escape($Dir))) { return $null }
    return $pidVal
}

function Do-Start {
    $live = Get-LivePid
    if ($live) {
        Warn "已在运行 (PID $live)，如需重启用: .\scripts\service.ps1 restart"
        return
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    if (-not (Test-Path config.yaml)) {
        Die "缺少 config.yaml（复制 config.yaml.example 为 config.yaml 后填写，或跑 init.ps1）"
    }

    # 后台启动，stdout/stderr 追加到同一日志。
    # 不用 Start-Process -RedirectStandard*：重定向文件句柄由子进程终身持有，
    # 运行中既删不掉也无法合并——日志会永远滞留在重定向文件里。
    # 方案：生成一次性 .cmd 批处理（@echo off + 追加重定向），避开
    # Start-Process -ArgumentList 对嵌套引号的不可控二次包装。
    # $proc 是 cmd 的 PID，python 是其子进程——stop 时 /T 结束整棵树。
    $runnerCmd = Join-Path $Dir "run\tg115bot-run.cmd"
    $redirects = "@echo off`r`n`"$Python`" main.py >> `"$StdoutLog`" 2>&1"
    # .cmd 由 cmd.exe 按系统 OEM 代码页解析；PS 5.1 的 Encoding.Default=ANSI(GBK)、
    # PS 7 恒为 UTF-8——显式取 OEM 代码页，两版写入一致（项目路径含中文也正确）
    $oem = [Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage
    [IO.File]::WriteAllText($runnerCmd, $redirects, [Text.Encoding]::GetEncoding($oem))
    $proc = Start-Process -FilePath "$env:ComSpec" -ArgumentList "/c", "`"$runnerCmd`"" `
        -WorkingDirectory $Dir -WindowStyle Hidden -PassThru
    $procId = $proc.Id
    "$procId" | Set-Content $PidFile
    Start-Sleep -Seconds 2
    if ($proc.HasExited) {
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        $tail = ""
        if (Test-Path $StdoutLog) {
            $tail = (Get-Content $StdoutLog -Encoding UTF8 -ErrorAction SilentlyContinue | Select-Object -Last 15) -join "`n"
        }
        Die "启动失败，最近日志：`n$tail"
    }
    Info "已启动 (PID $procId)"
    Info "日志: .\scripts\service.ps1 log"
    Write-Host "---- 启动日志 ----"
    if (Test-Path $StdoutLog) {
        Get-Content $StdoutLog -Tail 8 -Encoding UTF8 | ForEach-Object { "    $_" }
    }
}

function Do-Stop {
    $live = Get-LivePid
    if (-not $live) {
        Warn "未在运行"
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        return
    }
    Info "正在停止 (PID $live) …"
    # Windows 无 SIGTERM。taskkill（不带 /F）只对有窗口的进程发 WM_CLOSE，
    # 对 Hidden 窗口的 cmd/python 包装通常直接失败（exit 128）——失败就跳过
    # 10s 等待立即强杀，避免无谓卡顿。成功了才给优雅退出窗口。
    # /T 必须带：PID 文件记的是 cmd 包装进程，python 是其子进程。
    $graceful = $false
    $null = taskkill /T /PID $live 2>&1
    if ($LASTEXITCODE -eq 0) {
        $graceful = $true
        $deadline = (Get-Date).AddSeconds(10)
        while ((Get-Date) -lt $deadline) {
            if (-not (Get-Process -Id $live -ErrorAction SilentlyContinue)) { break }
            Start-Sleep -Milliseconds 500
        }
    }
    if (Get-Process -Id $live -ErrorAction SilentlyContinue) {
        if ($graceful) { Warn "优雅退出超时，强制结束" }
        $null = taskkill /F /T /PID $live 2>&1
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    Info "已停止"
}

function Do-Status {
    $live = Get-LivePid
    if (-not $live) {
        Warn "未在运行"
        exit 1
    }
    $proc = Get-Process -Id $live -ErrorAction Stop
    $mb = [math]::Round($proc.WorkingSet64 / 1MB)
    $uptime = (Get-Date) - $proc.StartTime
    $upStr = if ($uptime.Days -gt 0) { "{0}d {1:00}:{2:00}:{3:00}" -f $uptime.Days, $uptime.Hours, $uptime.Minutes, $uptime.Seconds }
             else { "{0:00}:{1:00}:{2:00}" -f $uptime.Hours, $uptime.Minutes, $uptime.Seconds }
    Info ("运行中 (PID {0})  内存 {1}MB  已运行 {2}" -f $live, $mb, $upStr)
}

function Do-Log([int]$Tail = 50) {
    if (-not (Test-Path $StdoutLog)) { Die "日志不存在: $StdoutLog" }
    Info "跟踪 $StdoutLog （Ctrl+C 退出）"
    # -Wait 即 Windows 版 tail -f（PowerShell 5.1 起）
    # stdout.log 由 main.py 强制写 UTF-8（见 _force_utf8_stdio）；PS 7 默认 UTF-8
    # 恰好一致，PS 5.1 默认 ANSI 会乱码 -> 显式指定，两版统一
    Get-Content $StdoutLog -Tail $Tail -Wait -Encoding UTF8
}

switch ($args[0]) {
    "start"   { Do-Start }
    "stop"    { Do-Stop }
    "restart" { Do-Stop; Do-Start }
    "status"  { Do-Status }
    "log"     {
        $n = 50
        if ($args.Count -ge 2 -and $args[1] -match '^\d+$') { $n = [int]$args[1] }
        Do-Log $n
    }
    default {
        Write-Host "用法: .\scripts\service.ps1 {start|stop|restart|status|log [N]}"
        Write-Host ""
        Write-Host "  start     后台启动（隐藏窗口，关终端不影响）"
        Write-Host "  stop      停止（10s 后强杀）"
        Write-Host "  restart   重启"
        Write-Host "  status    运行状态（PID/内存/时长）"
        Write-Host "  log [N]   跟踪日志（默认尾部 50 行）"
        exit 1
    }
}
