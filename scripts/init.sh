#!/usr/bin/env bash
#
# init.sh — tg115bot 全新环境交互式初始化
#
# 流程（每步可跳过，已就绪的自动跳过）:
#   [1/7] 系统依赖      python3.12 / venv / git 等（apt）
#   [2/7] Python 环境   .venv + pip install -r requirements.txt
#   [3/7] mihomo 代理   国内服务器必需（直连不通才引导；已装则读实际端口并穿代理实测）
#   [4/7] 配置文件      config.yaml（TG api_id/hash/bot_token 交互收集）
#   [5/7] 115 授权      先探活已有 token，失效则终端扫码（可跳过，之后 TG 里 /auth）
#   [6/7] 可选功能      AI 模式 / Web 台（全部可回车跳过）
#   [7/7] 完成提示      下一步命令
#
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

c_g=$'\e[32m'; c_y=$'\e[33m'; c_r=$'\e[31m'; c_b=$'\e[36m'; c_0=$'\e[0m'
info() { echo "${c_g}[+]${c_0} $*"; }
warn() { echo "${c_y}[!]${c_0} $*"; }
die()  { echo "${c_r}[x]${c_0} $*" >&2; exit 1; }
step() { echo; echo "${c_b}═══ $* ═══${c_0}"; }

ask() {  # ask <提示> <默认值> -> REPLY
  local prompt="$1" def="${2:-}"
  REPLY=""                      # 清空，避免复用上一问的残值
  if [[ -n "$def" ]]; then
    read -r -p "$prompt [$def]: " REPLY || REPLY="$def"
    [[ -n "$REPLY" ]] || REPLY="$def"
  else
    while [[ -z "$REPLY" ]]; do
      read -r -p "$prompt: " REPLY || die "输入中断"
    done
  fi
}

ask_yn() {  # ask_yn <提示> [默认y/n] -> REPLY=y/n；EOF/空行取默认
  local prompt="$1" def="${2:-y}" hint
  [[ "$def" == "y" ]] && hint="Y/n" || hint="y/N"
  REPLY="$def"
  read -r -p "$prompt ($hint) " REPLY || REPLY="$def"
  REPLY="${REPLY:-$def}"
  REPLY="${REPLY,,}"
  [[ "$REPLY" =~ ^[yYnN]$ ]] || { warn "无效输入，取默认 $def"; REPLY="$def"; }
}

echo "${c_b}╔══════════════════════════════════════╗${c_0}"
echo "${c_b}║   tg115bot 交互式初始化               ║${c_0}"
echo "${c_b}╚══════════════════════════════════════╝${c_0}"

[[ $EUID -eq 0 ]] || warn "建议用 root 运行（安装系统依赖需要）"

# ═══ [1/7] 系统依赖 ═══════════════════════════════════════════════════
step "[1/7] 系统依赖"

PY=""
for cand in python3.12 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
      PY="$(command -v "$cand")"; break
    fi
  fi
done

if [[ -z "$PY" ]]; then
  warn "未找到 Python >= 3.12，尝试安装（Debian/Ubuntu apt）…"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y
    apt-get install -y python3.12 python3.12-venv python3.12-dev 2>/dev/null \
      || apt-get install -y python3 python3-venv python3-dev
    for cand in python3.12 python3; do
      command -v "$cand" >/dev/null 2>&1 \
        && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' 2>/dev/null \
        && { PY="$(command -v "$cand")"; break; }
    done
    [[ -n "$PY" ]] || die "Python >= 3.12 安装失败，请手动安装后重跑"
  else
    die "非 apt 系统，请手动安装 Python >= 3.12 后重跑"
  fi
else
  info "Python 就绪: $PY ($("$PY" -V 2>&1))"
fi

command -v git >/dev/null 2>&1 || {
  command -v apt-get >/dev/null 2>&1 && apt-get install -y git || warn "缺少 git（不影响核心功能）"
}

# [3/7] 穿代理实测连通性要用 curl（setup-mihomo.sh 也会自动装，这里提前补齐）
command -v curl >/dev/null 2>&1 || {
  command -v apt-get >/dev/null 2>&1 && apt-get install -y curl || warn "缺少 curl（将跳过代理连通性实测）"
}

# ═══ [2/7] Python 环境 ════════════════════════════════════════════════
step "[2/7] Python 虚拟环境与依赖"

if [[ -x .venv/bin/python ]] && .venv/bin/python -V >/dev/null 2>&1; then
  info ".venv 已存在"
else
  "$PY" -m venv .venv || die "venv 创建失败（apt install python3-venv）"
  info ".venv 已创建"
fi

# 用 pip --dry-run 校验 requirements（而非硬编码包名探测），requirements 更新后能检测到缺包
# 注意：dry-run 缺包时退出码仍为 0，需 grep "Would install" 判断（pip 行为，非疏漏）
# 坑（Windows 实机踩过，同 init.ps1）：pip 自身失败（网络不通）时输出为空，
# 只 grep 会误判"已就绪"（venv 空着继续跑）——dry-run 失败也走真装暴露错误
dry_out=$(.venv/bin/pip install --dry-run -r requirements.txt 2>/dev/null)
dry_rc=$?
if [[ $dry_rc -eq 0 && -z "$(echo "$dry_out" | grep "Would install")" ]]; then
  info "依赖已就绪"
else
  info "安装/补齐依赖（首次较慢，含编译 TgCrypto）…"
  .venv/bin/pip install --upgrade pip wheel >/dev/null 2>&1 || true
  .venv/bin/pip install -r requirements.txt
  if [[ $? -ne 0 ]]; then
    # 国内直连 PyPI 常超时，清华镜像兜底重试一次
    warn "直连 PyPI 失败，改用清华镜像重试…"
    .venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple \
      || die "依赖安装失败，检查网络（可能需要代理）"
  fi
  info "依赖安装完成"
fi

# ═══ [3/7] mihomo 代理（可选） ═════════════════════════════════════════
step "[3/7] 代理（国内服务器需要）"

tg_direct_ok() {
  timeout 5 bash -c 'echo > /dev/tcp/149.154.167.51/443' 2>/dev/null
}

# 读 mihomo 实际监听端口（取法同 setup-mihomo.sh）——机场配置未必用 7890，
# 硬编码会往 config.yaml 写一个连不上的 proxy
mihomo_port() {
  local p
  p=$(grep -E '^(mixed-port|port|socks-port):' /etc/mihomo/config.yaml 2>/dev/null \
       | head -1 | awk '{print $2}' | tr -d "'\"" || true)
  echo "${p:-7890}"
}

# 安全校验：代理端口若监听公网，必须有源 IP 白名单；控制 API 只准本机。
# 背景：订阅配置常自带 allow-lan，公网服务器上等于开放代理，会被扫描器
# 滥用（刷流量/中转垃圾邮件，曾导致整机资源耗尽——2026-09 阿里云实案）
mihomo_security_check() {  # mihomo_security_check <端口>
  local cfg=/etc/mihomo/config.yaml port=$1 ctrl listen
  [[ -f $cfg ]] || return 0
  ctrl=$(grep -E '^external-controller:' "$cfg" | head -1 | awk '{print $2}' | tr -d "'\"" || true)
  if [[ -n $ctrl && $ctrl != 127.0.0.1:* && $ctrl != localhost:* ]]; then
    warn "mihomo 控制 API 绑定在 $ctrl —— 公网可远程改你的代理配置！"
    warn "修复: $cfg 里把 external-controller 改为 127.0.0.1:9090 后 systemctl restart mihomo"
  fi
  command -v ss >/dev/null 2>&1 || return 0
  listen=$(ss -tln 2>/dev/null | awk -v p=":$port"'$' '$4 ~ p {print $4}' | head -1) || true
  if [[ -n $listen && $listen != 127.0.0.1:* && $listen != localhost* && $listen != \[::1\]* ]]; then
    if ! grep -qE '^lan-allowed-ips:' "$cfg" \
       || grep -A5 -E '^lan-allowed-ips:' "$cfg" | grep -q '0\.0\.0\.0/0'; then
      warn "mihomo 代理端口监听 $listen 且缺源 IP 白名单 —— 公网开放代理，会被扫描滥用！"
      warn "修复: 重跑 sudo scripts/setup-mihomo.sh <订阅>（自动写入安全加固，不影响 Docker）"
    fi
  fi
}

# 穿代理实测 TG 可达：拿到任意 HTTP 状态码即证明隧道通（000 = 连不上/超时）。
# 只看"服务在跑"会漏掉订阅过期、节点全挂——那种 proxy 写进去 TG 也连不上
tg_proxy_ok() {  # tg_proxy_ok <proxy-url>
  local code
  code=$(curl -s -x "$1" --connect-timeout 10 --max-time 20 \
         -o /dev/null -w '%{http_code}' https://api.telegram.org/ 2>/dev/null) || true
  [[ -n $code && $code != 000 ]]
}

deploy_mihomo() {  # 交互收集订阅并部署，成功后按实际端口设 TG_PROXY
  ask "请输入机场订阅地址（Clash 订阅链接）"
  bash scripts/setup-mihomo.sh "$REPLY" || die "mihomo 部署失败，可稍后手动: sudo scripts/setup-mihomo.sh <订阅>"
  TG_PROXY="http://127.0.0.1:$(mihomo_port)"
}

if tg_direct_ok; then
  info "可直连 Telegram，跳过代理配置"
  TG_PROXY=""
elif command -v mihomo >/dev/null 2>&1 && systemctl is-active --quiet mihomo 2>/dev/null; then
  PORT=$(mihomo_port)
  TG_PROXY="http://127.0.0.1:$PORT"
  info "mihomo 已在运行（端口 $PORT）"
  mihomo_security_check "$PORT"
  if ! command -v curl >/dev/null 2>&1; then
    warn "缺少 curl，跳过穿代理实测"
  elif tg_proxy_ok "$TG_PROXY"; then
    info "穿代理实测 TG 可达 ✓"
  else
    warn "mihomo 在跑，但穿代理访问 TG 失败（订阅过期/节点异常常见）"
    ask_yn "重新部署 mihomo 更新订阅？" "n"
    if [[ "$REPLY" == "y" ]]; then
      deploy_mihomo
    else
      warn "仍按 $TG_PROXY 写入——节点不通时 TG 将无法连接"
    fi
  fi
else
  warn "无法直连 Telegram（国内服务器常见）"
  ask_yn "现在部署 mihomo 代理？需要订阅地址" "y"
  if [[ "$REPLY" == "y" ]]; then
    deploy_mihomo
  else
    TG_PROXY=""
    warn "跳过代理——TG 将无法连接。可稍后: sudo scripts/setup-mihomo.sh <订阅>"
  fi
fi

# ═══ [4/7] 配置文件 ═══════════════════════════════════════════════════
step "[4/7] 配置文件 config.yaml"

if [[ -f config.yaml ]]; then
  info "config.yaml 已存在，跳过（如需重配请手动编辑）"
else
  [[ -f config.yaml.example ]] || die "缺少 config.yaml.example"
  cp config.yaml.example config.yaml

  echo "需要 3 项 Telegram 信息（参考 README）:"
  echo "  api_id/api_hash: https://my.telegram.org -> API development tools"
  echo "  bot_token:      @BotFather 创建 bot 获得"
  echo

  ask "telegram.api_id（纯数字）"
  sed -i "s|^  api_id: .*|  api_id: ${REPLY}|" config.yaml

  ask "telegram.api_hash（32位字符串）"
  sed -i "s|^  api_hash: .*|  api_hash: \"${REPLY}\"|" config.yaml

  ask "telegram.bot_token"
  sed -i "s|^  bot_token: .*|  bot_token: \"${REPLY}\"|" config.yaml

  if [[ -n "$TG_PROXY" ]]; then
    sed -i "s|^  proxy: .*|  proxy: \"${TG_PROXY}\"|" config.yaml
    info "proxy 已写入: $TG_PROXY"
  else
    sed -i 's|^  proxy: .*|  proxy: ""|' config.yaml
  fi

  ask_yn "限制仅自己使用？（推荐，填 allowed_users 白名单）" "y"
  if [[ "$REPLY" == "y" ]]; then
    echo "你的 TG 数字 user_id 可在 TG 里向 @userinfobot 发消息获取"
    ask "你的 user_id"
    sed -i "s|^  allowed_users: \[\]|  allowed_users: [${REPLY}]|" config.yaml
  fi

  info "config.yaml 已生成"
fi

# ═══ [5/7] 115 授权 ═══════════════════════════════════════════════════
step "[5/7] 115 账号授权（扫码）"

# token 文件存在 ≠ 有效：授权被解除/refresh_token 失效只有真发请求才暴露
# （access_token 过期会自动刷新，不误报）——与其等运行时炸，不如 init 探活一次
if .venv/bin/python scripts/check115.py --probe; then
  info "115 授权有效，跳过"
else
  ask_yn "现在扫码授权 115？（也可稍后在 TG 里发 /auth）" "y"
  if [[ "$REPLY" == "y" ]]; then
    .venv/bin/python scripts/check115.py --auth || warn "授权未完成——启动后在 TG 里发 /auth 也可以"
  else
    warn "跳过。启动后向 bot 发 /auth 扫码授权"
  fi
fi

# ═══ [6/7] 可选功能 ═══════════════════════════════════════════════════
step "[6/7] 可选功能"

if [[ -f config.yaml ]]; then
  # 已配置（api_key 与 model 均非空 = AiCfg.enabled 判定，见 config.py）则跳过，
  # 与其他步骤"已就绪自动跳过"的幂等语义一致
  if .venv/bin/python -c "
from config import load_config
import sys
try:
    sys.exit(0 if load_config().ai.enabled else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
    info "AI 助手已配置，跳过"
  else
  ask_yn "启用 AI 助手模式？（需要 OpenAI 兼容 API key，如 DeepSeek）" "n"
  if [[ "$REPLY" == "y" ]]; then
    ask "API base_url" "https://api.deepseek.com/v1"; AI_BASE="$REPLY"
    ask "api_key"; AI_KEY="$REPLY"
    ask "model" "deepseek-chat"; AI_MODEL="$REPLY"
    # ai: 段存在才改（避免误伤其他段的同名键）
    if grep -q "^ai:" config.yaml; then
      .venv/bin/python - "$AI_BASE" "$AI_KEY" "$AI_MODEL" <<'PYEOF'
import sys
base, key, model = sys.argv[1:4]
NL = chr(10)
p = open("config.yaml", encoding="utf-8").read()
# 逐行状态机：仅在 ^ai: 段内替换（到下一个非缩进行），零正则
lines = p.split(NL)
in_ai = False
out = []
for ln in lines:
    if ln.startswith("ai:"):
        in_ai = True
    elif in_ai and ln and not ln[0] in (" ", "\t"):
        in_ai = False
    if in_ai:
        if ln.lstrip().startswith("base_url:"):
            ln = "  base_url: \"" + base + "\""
        elif ln.lstrip().startswith("api_key:"):
            ln = "  api_key: \"" + key + "\""
        elif ln.lstrip().startswith("model:"):
            ln = "  model: \"" + model + "\""
    out.append(ln)
open("config.yaml", "w", encoding="utf-8").write(NL.join(out))
PYEOF
      info "AI 模式已配置"
    else
      warn "config.yaml 无 ai: 段，请手动配置"
    fi
  fi
  fi  # 已配置跳过分支闭合

  # Web 台同理：enable: true 则跳过（与 AI 的幂等语义对齐）
  if grep -q "^web:" config.yaml && grep -A5 "^web:" config.yaml | grep -q "enable: true"; then
    info "Web 管理台已启用，跳过"
  else
  ask_yn "启用 Web 管理台？（浏览器查看任务/日志）" "n"
  if [[ "$REPLY" == "y" ]]; then
    sed -i 's|^  enable: false|  enable: true|' config.yaml
    ask "Web 密码（默认 changeme，务必修改）" "changeme"
    sed -i "s|^  password: \"changeme\"|  password: \"${REPLY}\"|" config.yaml
    info "Web 台将监听 :8080"
  fi
  fi  # Web 已启用跳过分支闭合
fi

# ═══ [7/7] 完成 ═══════════════════════════════════════════════════════
step "[7/7] 初始化完成 🎉"

echo
info "全部就绪！启动服务："
echo "    ./scripts/service.sh start"
echo
echo "常用命令："
echo "    ./scripts/service.sh status   查看状态"
echo "    ./scripts/service.sh log      跟踪日志"
echo "    ./scripts/service.sh restart  重启（改配置后）"
echo
echo "提升下载额度/防 FloodWait 可配 user session（Premium user 可下 4GB）："
echo "    .venv/bin/python scripts/make_session.py   然后填 config.yaml 的 user_session"
