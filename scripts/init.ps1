# init.ps1 — tg115bot Windows 交互式初始化
#
# 与 Linux 版 init.sh 对应，但不含代理部署步骤：
# Windows 上请自行安装系统代理软件（Clash/v2rayN 等），config.yaml 的
# telegram.proxy 填其本地端口（如 http://127.0.0.1:7890）即可。
#
# 流程（每步可跳过，已就绪的自动跳过）:
#   [1/6] Python 环境   检测 Python >= 3.12，创建 .venv + 安装依赖
#   [2/6] 配置文件      config.yaml（TG api_id/hash/bot_token 交互收集）
#   [3/6] 115 授权      终端二维码扫码（可跳过，之后 TG 里 /auth）
#   [4/6] 可选功能      AI 模式 / Web 台（全部可回车跳过）
#   [5/6] 快捷方式      （可选）桌面/开始菜单
#   [6/6] 完成提示      下一步命令
#
# 用法: 在项目目录打开 PowerShell 执行
#   powershell -ExecutionPolicy Bypass -File scripts\init.ps1
#   （Windows 自带 Windows PowerShell 5.1 即可，无需安装 pwsh）

#Requires -Version 5.1
# 注意：这里故意不用 "Stop"——PS 5.1 下原生命令(python/pip)的 stderr 被重定向
# (2>$null / 2>&1) 时会包装成 NativeCommandError，配 Stop 会中途炸掉。
# 本脚本每步都有显式 $LASTEXITCODE / Test-Path 校验，不依赖 EAP 兜底。
$ErrorActionPreference = "Continue"

$Dir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Dir

# ── 颜色输出（Write-Host 免管道，颜色对齐 bash 版）────────────────────────
function Info($m) { Write-Host "[+] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "[x] $m" -ForegroundColor Red; exit 1 }
function Step($m) {
    Write-Host ""
    Write-Host ("═══ {0} ═══" -f $m) -ForegroundColor Cyan
}

function Ask([string]$Prompt, [string]$Default = "") {
    # 对齐 bash 版 ask：空输入回退默认值；无默认则必须输入
    while ($true) {
        if ($Default) {
            $r = Read-Host ("{0} [{1}]" -f $Prompt, $Default)
            if (-not $r) { $r = $Default }
        } else {
            $r = Read-Host $Prompt
        }
        if ($r) { return $r }
        if ($Default) { return $Default }
    }
}

function AskYn([string]$Prompt, [string]$Default = "y") {
    $hint = if ($Default -eq "y") { "Y/n" } else { "y/N" }
    $r = Read-Host ("{0} ({1}) " -f $Prompt, $hint)
    if (-not $r) { $r = $Default }
    $r = $r.ToLower()
    if ($r -in @("y", "n")) { return $r }
    Warn "无效输入，取默认 $Default"
    return $Default
}

# PS 5.1 向原生命令传参时不转义内嵌双引号：Python 代码/JSON 里的 " 会被 CRT
# 参数解析吃掉（t["x"] 变 t[x]，-c 直接 SyntaxError）。规避：代码与参数都
# base64 化，命令行上只剩安全字符；引导行只用单引号（PS/CRT 均不特殊处理）。
# 代码内经 exec 执行，sys.argv[1]=代码本身，故参数固定从 sys.argv[2] 取。
function RunPy([string]$Code, [string]$Arg = "") {
    $codeB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Code))
    $argB64  = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Arg))
    $boot    = "import base64,sys;exec(base64.b64decode(sys.argv[1]).decode('utf-8'))"
    & $VenvPy -c $boot $codeB64 $argB64 2>&1
}

Write-Host "╔══════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║   tg115bot 交互式初始化 (Windows)     ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# ═══ [1/6] Python 环境 ════════════════════════════════════════════════
Step "[1/6] Python 环境与依赖"

# Python 解释器 = exe + 参数列表（py -3.12 / py -3 / python），后面用
# & $PyExe @PyArgs 方式调用，避免把 "py -3.12" 整串当命令名。
$PyExe = ""; $PyArgs = @()
$found = $false
# py 启动器是 Windows Python 官方安装器的标配，优先用它（可带版本号）
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($a in @(@("-3.12"), @("-3"))) {
        py $a[0] -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $PyExe = "py"; $PyArgs = $a; $found = $true; break }
    }
}
# 回退：PATH 里的 python
if (-not $found -and (Get-Command python -ErrorAction SilentlyContinue)) {
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { $PyExe = "python"; $found = $true }
}

if (-not $found) {
    Warn "未找到 Python >= 3.12"
    $r = AskYn "现在打开下载页面安装？（装完重跑本脚本）" "y"
    if ($r -eq "y") { Start-Process "https://www.python.org/downloads/windows/" }
    Die "请安装 Python 3.12+（勾选 Add python.exe to PATH）后重跑 scripts\init.ps1"
}
$PyDesc = (@($PyExe) + $PyArgs) -join " "
Info "Python 就绪: $PyDesc"

$VenvPy = Join-Path $Dir ".venv\Scripts\python.exe"
if (Test-Path $VenvPy) {
    Info ".venv 已存在"
} else {
    Info "创建虚拟环境 .venv …"
    & $PyExe @PyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPy)) { Die "venv 创建失败" }
    Info ".venv 已创建"
}

# 对齐 bash 版：用 pip --dry-run 校验 requirements（缺包时退出码仍为 0，需看 "Would install"）
# 坑（实机踩过）：pip 自身失败（网络不通）时 stderr 被吞、$dry 为空，只看
# "Would install" 会误判"已就绪"（venv 空着继续跑，到 config 写入才炸
# No module named 'yaml'）——dry-run 失败也走真装，让真实错误暴露出来
Info "检查依赖（首次较慢，TgCrypto 无预编译时会纯 Python 回退安装）…"
$dry = & $VenvPy -m pip install --dry-run -r requirements.txt 2>$null | Out-String
if ($LASTEXITCODE -ne 0 -or $dry -match "Would install") {
    & $VenvPy -m pip install --upgrade pip wheel 2>$null | Out-Null
    & $VenvPy -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        # 国内直连 PyPI 常超时；Clash 等未开系统代理时 pip 不走代理，镜像兜底重试
        Warn "直连 PyPI 失败，改用清华镜像重试…"
        & $VenvPy -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
        if ($LASTEXITCODE -ne 0) { Die "依赖安装失败，检查网络（公司网络可能需要设代理）" }
    }
    Info "依赖安装完成"
} else {
    Info "依赖已就绪"
}

# ═══ [2/6] 配置文件 ════════════════════════════════════════════════════
Step "[2/6] 配置文件 config.yaml"

if (Test-Path config.yaml) {
    Info "config.yaml 已存在，跳过（如需重配请手动编辑）"
} else {
    if (-not (Test-Path config.yaml.example)) { Die "缺少 config.yaml.example" }
    Copy-Item config.yaml.example config.yaml

    Write-Host "需要 3 项 Telegram 信息（参考 README）:"
    Write-Host "  api_id/api_hash: https://my.telegram.org -> API development tools"
    Write-Host "  bot_token:      @BotFather 创建 bot 获得"
    Write-Host ""

    $apiId   = Ask "telegram.api_id（纯数字）"
    $apiHash = Ask "telegram.api_hash（32位字符串）"
    $token   = Ask "telegram.bot_token"

    # Windows 上多数用户自带系统代理软件（Clash/v2rayN 等）；
    # 不替用户装，只问地址写进 proxy，直连用户直接回车留空。
    Write-Host "国内网络访问 TG 需要代理。Windows 用户通常已装 Clash/v2rayN 等，"
    Write-Host "填其本地监听地址即可（混合端口示例 http://127.0.0.1:7890）；能直连则回车留空。"
    $proxy = Read-Host "telegram.proxy"

    $useWl = AskYn "限制仅自己使用？（推荐，填 allowed_users 白名单）" "y"
    $allowed = ""
    if ($useWl -eq "y") {
        Write-Host "你的 TG 数字 user_id 可在 TG 里向 @userinfobot 发消息获取"
        $allowed = Ask "你的 user_id"
    }

    # 配置写入交给内嵌 Python（yaml 往返，键名精确）：
    # PowerShell 的 -replace 会把其他段的同名键（如 api_key）一起误伤，bash 版用
    # sed 也有同样问题所以专门写了逐行状态机——这里直接复用该思路走 yaml 安全路径。
    $cfgPy = @'
import base64, json, sys
from pathlib import Path
vals = json.loads(base64.b64decode(sys.argv[2]).decode("utf-8"))
import yaml
p = Path("config.yaml")
cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
t = cfg["telegram"]
t["api_id"] = int(vals["api_id"])
t["api_hash"] = vals["api_hash"]
t["bot_token"] = vals["bot_token"]
t["proxy"] = vals["proxy"]
if vals.get("allowed"):
    t["allowed_users"] = [int(x) for x in vals["allowed"].replace(",", " ").split()]
p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
print("ok")
'@
    $payload = @{
        api_id      = $apiId
        api_hash    = $apiHash
        bot_token   = $token
        proxy       = $proxy
        allowed     = $allowed
    } | ConvertTo-Json -Compress
    # JSON 经 base64 传递（见 RunPy 注释：PS 5.1 会吃掉参数里的双引号）
    $out = RunPy $cfgPy $payload
    if ($LASTEXITCODE -ne 0 -or "$out" -notmatch "ok") {
        # 删掉 example 副本，否则重跑会命中"已存在，跳过"卡住重新收集
        Remove-Item config.yaml -ErrorAction SilentlyContinue
        Die "config.yaml 写入失败: $out"
    }
    Info "config.yaml 已生成"
}

# ═══ [3/6] 115 授权 ════════════════════════════════════════════════════
Step "[3/6] 115 账号授权（扫码）"

$hasToken = Get-ChildItem -Path (Join-Path $Dir "config") -Filter "open_token_*.json" -ErrorAction SilentlyContinue
if ($hasToken) {
    Info "已有 115 token，跳过"
} else {
    $r = AskYn "现在扫码授权 115？（也可稍后在 TG 里发 /auth）" "y"
    if ($r -eq "y") {
        # Windows Terminal / PowerShell 5.1 对 print_ascii 的半块字符渲染良好
        & $VenvPy scripts\check115.py --auth
        if ($LASTEXITCODE -ne 0) { Warn "授权未完成——启动后在 TG 里发 /auth 也可以" }
    } else {
        Warn "跳过。启动后向 bot 发 /auth 扫码授权"
    }
}

# ═══ [4/6] 可选功能 ════════════════════════════════════════════════════
Step "[4/6] 可选功能"

if (Test-Path config.yaml) {
    $r = AskYn "启用 AI 助手模式？（需要 OpenAI 兼容 API key，如 DeepSeek）" "n"
    if ($r -eq "y") {
        $aiBase  = Ask "API base_url" "https://api.deepseek.com/v1"
        $aiKey   = Ask "api_key"
        $aiModel = Ask "model" "deepseek-chat"
        # 同样走 yaml 安全路径，只动 ai: 段，不碰其他段同名键
        $aiPy = @'
import base64, json, sys
from pathlib import Path
vals = json.loads(base64.b64decode(sys.argv[2]).decode("utf-8"))
import yaml
p = Path("config.yaml")
cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
cfg.setdefault("ai", {})
cfg["ai"]["base_url"] = vals["base_url"]
cfg["ai"]["api_key"] = vals["api_key"]
cfg["ai"]["model"] = vals["model"]
p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
print("ok")
'@
        $payload = @{ base_url = $aiBase; api_key = $aiKey; model = $aiModel } | ConvertTo-Json -Compress
        $out = RunPy $aiPy $payload
        if ($LASTEXITCODE -ne 0 -or "$out" -notmatch "ok") {
            Warn "AI 段写入失败: $out（请手动编辑 config.yaml 的 ai: 段）"
        } else {
            Info "AI 模式已配置"
        }
    }

    $r = AskYn "启用 Web 管理台？（浏览器查看任务/日志）" "n"
    if ($r -eq "y") {
        $webPwd = Ask "Web 密码（默认 changeme，务必修改）" "changeme"
        $webPy = @'
import base64, sys
from pathlib import Path
import yaml
p = Path("config.yaml")
cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
cfg.setdefault("web", {})
cfg["web"]["enable"] = True
cfg["web"]["password"] = base64.b64decode(sys.argv[2]).decode("utf-8")
p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
print("ok")
'@
        $out = RunPy $webPy $webPwd
        if ($LASTEXITCODE -ne 0 -or "$out" -notmatch "ok") {
            Warn "Web 段写入失败: $out（请手动编辑 config.yaml 的 web: 段）"
        } else {
            Info "Web 台将监听 :8080"
        }
    }
}

# ═══ [5/6] 快捷方式（可选）═════════════════════════════════════════════
Step "[5/6] 快捷方式（可选）"

$r = AskYn "创建「tg115bot 控制台」开始菜单快捷方式？（双击即进入管理）" "n"
if ($r -eq "y") {
    # 快捷方式启动一个固定到项目目录的 PowerShell，进入 service 菜单
    $menuPs1 = Join-Path $Dir "scripts\menu.ps1"
    $WshShell = New-Object -ComObject WScript.Shell
    $lnkPath  = Join-Path ([Environment]::GetFolderPath("Programs")) "tg115bot.lnk"
    $lnk = $WshShell.CreateShortcut($lnkPath)
    $lnk.TargetPath  = "powershell.exe"
    $lnk.Arguments   = "-NoExit -ExecutionPolicy Bypass -File `"$menuPs1`""
    $lnk.WorkingDirectory = $Dir
    $lnk.Description = "tg115bot 服务管理"
    $lnk.Save()
    Info "已创建: $lnkPath"
}

# ═══ [6/6] 完成 ════════════════════════════════════════════════════════
Step "[6/6] 初始化完成 🎉"

Write-Host ""
Info "全部就绪！启动服务："
Write-Host "    .\scripts\service.ps1 start"
Write-Host ""
Write-Host "常用命令："
Write-Host "    .\scripts\service.ps1 status   查看状态"
Write-Host "    .\scripts\service.ps1 log      跟踪日志"
Write-Host "    .\scripts\service.ps1 restart  重启（改配置后）"
Write-Host ""
Write-Host "提升下载额度/防 FloodWait 可配 user session（Premium user 可下 4GB）："
Write-Host "    .venv\Scripts\python.exe scripts\make_session.py   然后填 config.yaml 的 user_session"
