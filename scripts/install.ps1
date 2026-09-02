# install.ps1 - tg115bot 一键安装（Windows PowerShell 5.1+ / 7）
#
#   irm https://raw.githubusercontent.com/ivenlau/tg115bot/main/scripts/install.ps1 | iex
#
# 做四件事：
#   1. 克隆（或 git pull 更新）仓库到 %LOCALAPPDATA%\tg115bot（TG115BOT_HOME 可覆盖）
#   2. 检测 Python >= 3.12，建 .venv 并装依赖（失败走清华镜像）
#   3. 写 tb.cmd shim 到 WindowsApps（默认在用户 PATH，无需管理员）
#   4. 打印下一步（tb init -> tb start）
#
# 已有安装重复执行安全：代码更新 + 依赖补齐 + shim 重写，不动 config.yaml。
#
# 注意：这里故意不用 "Stop"——PS 5.1 下原生命令(git/pip)的 stderr 重定向会被
# 包装成 NativeCommandError 直接炸（见 init.ps1 同位置注释）
$ErrorActionPreference = "Continue"

$Repo = "https://github.com/ivenlau/tg115bot.git"
$InstallDir = if ($env:TG115BOT_HOME) { $env:TG115BOT_HOME } else { Join-Path $env:LOCALAPPDATA "tg115bot" }
$BinDir = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps"

function Info($m) { Write-Host "[+] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "[x] $m" -ForegroundColor Red; exit 1 }

# ---------- 前置：git ----------
git --version *> $null
if ($LASTEXITCODE -ne 0) { Die "缺少 git：先安装 https://git-scm.com/download/win" }

# ---------- Python >= 3.12（带参数的解释器必须拆 exe+args 数组调用） ----------
$PyExe = $null; $PyArgs = @()
foreach ($c in @(@("py", "3.12"), @("py", "3.13"), @("py", "3"), @("python"))) {
    $exe = $c[0]; $rest = if ($c.Count -gt 1) { $c[1..($c.Count - 1)] } else { @() }
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        & $exe @rest -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $PyExe = $exe; $PyArgs = $rest; break }
    }
}
if (-not $PyExe) { Die "需要 Python >= 3.12（https://python.org 勾选 Add to PATH 后重开终端）" }
Info "Python: $(& $PyExe @PyArgs --version 2>&1)"

# ---------- 代码 ----------
if (Test-Path (Join-Path $InstallDir ".git")) {
    Info "更新已有安装: $InstallDir"
    git -C $InstallDir pull --ff-only
    if ($LASTEXITCODE -ne 0) { Warn "git pull 失败（本地有改动？），沿用当前代码继续" }
} else {
    Info "克隆仓库 -> $InstallDir"
    git clone --depth 1 $Repo $InstallDir
    if ($LASTEXITCODE -ne 0) { Die "克隆失败（网络？）" }
}

# ---------- venv + 依赖（幂等） ----------
$VenvPy = Join-Path $InstallDir ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    & $PyExe @PyArgs -m venv (Join-Path $InstallDir ".venv")
    if ($LASTEXITCODE -ne 0) { Die "venv 创建失败" }
}
& $VenvPy -m pip install --quiet --upgrade pip wheel 2>$null
& $VenvPy -m pip install --quiet -r (Join-Path $InstallDir "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    Warn "直连 PyPI 失败，改用清华镜像重试..."
    & $VenvPy -m pip install --quiet -r (Join-Path $InstallDir "requirements.txt") -i https://pypi.tuna.tsinghua.edu.cn/simple
    if ($LASTEXITCODE -ne 0) { Die "依赖安装失败，检查网络" }
}
Info "依赖就绪"

# ---------- tb 命令（WindowsApps 默认在用户 PATH，写 shim 无需管理员） ----------
if (-not (Test-Path $BinDir)) { New-Item -ItemType Directory -Path $BinDir | Out-Null }
$Shim = Join-Path $BinDir "tb.cmd"
"@echo off`r`n`"$VenvPy`" -m tb %*" | Set-Content -Path $Shim -Encoding ASCII
Info "已安装命令: $Shim"

Write-Host ""
Info "================ 安装完成 ================"
Write-Host "  代码目录:   $InstallDir"
Write-Host "  下一步:"
Write-Host "    tb init      # 首次配置（交互式）"
Write-Host "    tb doctor    # 一键体检"
Write-Host "    tb start     # 启动服务；裸 tb 进交互界面"
Write-Host "    tb --help    # 全部命令"
Write-Host "=========================================="
