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
    # 校验是本项目的 python（CommandLine 含项目目录下的 main.py）
    $cmd = $null
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $pidVal" -ErrorAction Stop).CommandLine
    } catch {
        # Get-CimInstance 不可用时退化为只验进程名（Get-Process 拿不到父目录信息）
        $cmd = if ($proc.ProcessName -match "python") { "$Dir main.py" } else { $null }
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

    # 后台启动：stdout/stderr 追加到同一日志
    # （PowerShell 5.1 的 Start-Process -RedirectStandardError 与 -RedirectStandardOutput
    #   不能指向同一文件，故先中转 cmd /c 的 2>&1 再追加 —— 与 bash 的 >> log 2>&1 等价）
    $proc = Start-Process -FilePath $Python -ArgumentList "main.py" `
        -WorkingDirectory $Dir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput "$StdoutLog.new" -RedirectStandardError "$StdoutLog.err"
    $procId = $proc.Id
    "$procId" | Set-Content $PidFile
    # 合并 .err 进主日志后清理中转文件（保留 .new 改名语义：追加进主日志）
    Start-Sleep -Seconds 2
    if ($proc.HasExited) {
        # 启动即退：把两份中转日志拼出来给用户看
        $tail = ""
        foreach ($f in @("$StdoutLog.err", "$StdoutLog.new")) {
            if (Test-Path $f) { $tail += (Get-Content $f -ErrorAction SilentlyContinue | Select-Object -Last 15) -join "`n" }
        }
        Remove-Item "$StdoutLog.new", "$StdoutLog.err" -ErrorAction SilentlyContinue
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        Die "启动失败，最近日志：`n$tail"
    }
    foreach ($f in @("$StdoutLog.err", "$StdoutLog.new")) {
        if (Test-Path $f) {
            Add-Content $StdoutLog (Get-Content $f)
            Remove-Item $f
        }
    }
    Info "已启动 (PID $procId)"
    Info "日志: .\scripts\service.ps1 log"
    Write-Host "---- 启动日志 ----"
    if (Test-Path $StdoutLog) {
        Get-Content $StdoutLog -Tail 8 | ForEach-Object { "    $_" }
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
    # 对 -WindowStyle Hidden 的控制台 python 通常直接失败（exit 128）——
    # 那就跳过 10s 等待立即强杀，避免无谓卡顿。成功了才给优雅退出窗口。
    $graceful = $false
    $null = taskkill /PID $live 2>&1
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
    Get-Content $StdoutLog -Tail $Tail -Wait
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
