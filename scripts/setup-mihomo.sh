#!/usr/bin/env bash
#
# setup-mihomo.sh — 一键在全新 Linux 环境部署 mihomo (Clash Meta 内核)
#
# 功能:
#   1. 自动探测 CPU 架构，从 GitHub 下载对应的 mihomo（断点续传）
#   2. 用 Clash UA 下载订阅配置（自动识别 base64 假订阅）
#   3. 自动从 jsDelivr 镜像补齐 GeoIP/GeoSite 数据库（绕开 GitHub 直连超时）
#   4. 校验配置 → 启动 systemd 服务 → 实测代理连通性
#
# 用法:
#   sudo ./setup-mihomo.sh <订阅地址>            # 下载订阅并部署
#   sudo ./setup-mihomo.sh /path/config.yaml     # 使用本地配置文件
#   sudo ./setup-mihomo.sh                       # 交互式输入订阅地址
#
# 环境变量:
#   MIHOMO_SUB_URL   订阅地址（与位置参数等价）
#   MIHOMO_VERSION   指定版本，默认 latest
#   MIHOMO_FORCE=1   强制重装二进制（默认已安装则跳过）
#
set -euo pipefail

GITHUB="https://github.com"
GEOMIRROR="https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release"
CONFIG_DIR="/etc/mihomo"
FALLBACK_VERSION="v1.19.30"

# ---------- 输出 ----------
if [[ -t 1 ]]; then
  C_G=$'\e[32m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_0=$'\e[0m'
else
  C_G=""; C_Y=""; C_R=""; C_0=""
fi
info() { echo "${C_G}[+]${C_0} $*"; }
warn() { echo "${C_Y}[!]${C_0} $*"; }
die()  { echo "${C_R}[x]${C_0} $*" >&2; exit 1; }
trap 'echo "${C_R}[x]${C_0} 脚本在第 $LINENO 行失败，请检查上方报错" >&2' ERR

# ---------- 前置检查 ----------
[[ $EUID -eq 0 ]] || die "请用 root 运行: sudo $0"
command -v curl >/dev/null 2>&1 \
  || { apt-get update -y >/dev/null 2>&1 && apt-get install -y curl >/dev/null 2>&1; } \
  || die "缺少 curl 且自动安装失败，请手动安装后重试"
command -v systemctl >/dev/null 2>&1 || die "此脚本需要 systemd"

# ---------- 参数 ----------
SUB_SRC="${1:-${MIHOMO_SUB_URL:-}}"
if [[ -z $SUB_SRC ]]; then
  [[ -t 0 ]] || die "未提供订阅地址。用法: $0 <订阅地址>"
  read -rp "请输入订阅地址（或本地 config.yaml 路径）: " SUB_SRC
  [[ -n $SUB_SRC ]] || die "订阅地址为空"
fi

# ---------- 断点续传下载: fetch <url> <输出> [重试次数] ----------
fetch() {
  local url=$1 out=$2 tries=${3:-6} i
  mkdir -p "$(dirname "$out")"
  for ((i = 1; i <= tries; i++)); do
    curl -fL --connect-timeout 15 --max-time 300 -C - -o "$out" "$url" && return 0
    warn "下载中断，第 $i/$tries 次续传重试: $(basename "$out")"
    sleep 2
  done
  return 1
}

# ---------- 解析版本 ----------
VERSION="${MIHOMO_VERSION:-latest}"
if [[ $VERSION == latest ]]; then
  info "正在获取 mihomo 最新版本号..."
  VERSION=$(curl -fsSL --max-time 30 https://api.github.com/repos/MetaCubeX/mihomo/releases/latest \
            | grep -o '"tag_name": *"[^"]*"' | head -1 | cut -d'"' -f4) || VERSION=""
  if [[ -z $VERSION ]]; then
    warn "获取最新版本失败，回退到 $FALLBACK_VERSION"
    VERSION=$FALLBACK_VERSION
  fi
fi
info "目标版本: $VERSION"

# ---------- 探测架构 ----------
SUFFIX=
case "$(uname -m)" in
  x86_64)
    if   grep -qm1 avx2   /proc/cpuinfo; then SUFFIX=amd64-v3
    elif grep -qm1 sse4_2 /proc/cpuinfo; then SUFFIX=amd64-v2
    else SUFFIX=amd64-compatible; fi
    # 老旧系统（如 CentOS 7，glibc<2.28）需用 go120 兼容构建
    GLIBC=$(ldd --version 2>/dev/null | head -1 | grep -o '[0-9.]*$') || GLIBC=99
    if awk -v v="$GLIBC" 'BEGIN{exit !(v<2.28)}'; then
      SUFFIX=amd64-v1-go120
      warn "检测到 glibc $GLIBC < 2.28，改用 go120 兼容构建"
    fi
    ;;
  aarch64|arm64) SUFFIX=arm64 ;;
  armv7l|armv8l) SUFFIX=armv7 ;;
  *) die "不支持的架构: $(uname -m)，请到 $GITHUB/MetaCubeX/mihomo/releases 手动选择" ;;
esac
info "架构: $(uname -m) → linux-$SUFFIX"

# ---------- 安装二进制 ----------
install_from_deb() {
  local asset="mihomo-linux-$SUFFIX-$VERSION.deb"
  local url="$GITHUB/MetaCubeX/mihomo/releases/download/$VERSION/$asset"
  local tmp; tmp=$(mktemp -d)
  info "下载 $asset（GitHub 直连可能较慢，自动断点续传）..."
  fetch "$url" "$tmp/$asset" || die "下载失败: $url"
  dpkg -i "$tmp/$asset" >/dev/null || apt-get -f install -y >/dev/null
  rm -rf "$tmp"
}

install_from_gz() {
  local asset="mihomo-linux-$SUFFIX-$VERSION.gz"
  local url="$GITHUB/MetaCubeX/mihomo/releases/download/$VERSION/$asset"
  local tmp; tmp=$(mktemp -d)
  info "下载 $asset..."
  fetch "$url" "$tmp/mihomo.gz" || die "下载失败: $url"
  gzip -dc "$tmp/mihomo.gz" > /usr/local/bin/mihomo
  chmod +x /usr/local/bin/mihomo
  cat > /etc/systemd/system/mihomo.service <<'UNIT'
[Unit]
Description=mihomo Daemon, Another Clash Kernel.
After=network.target

[Service]
Type=simple
LimitNPROC=500
LimitNOFILE=1000000
ExecStart=/usr/local/bin/mihomo -d /etc/mihomo
Restart=on-failure
RestartForceExitStatus=SIGKILL

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  rm -rf "$tmp"
}

if [[ ${MIHOMO_FORCE:-0} == 1 ]] || ! command -v mihomo >/dev/null 2>&1; then
  if command -v dpkg >/dev/null 2>&1; then install_from_deb; else install_from_gz; fi
  info "已安装: $(mihomo -v 2>/dev/null | head -1 || true)"
else
  info "mihomo 已存在: $(mihomo -v 2>/dev/null | head -1 || true)，跳过安装（MIHOMO_FORCE=1 可强制重装）"
fi

# ---------- 获取订阅配置 ----------
mkdir -p "$CONFIG_DIR"
CONFIG="$CONFIG_DIR/config.yaml"
NEW="$CONFIG_DIR/config.yaml.new"

looks_like_yaml() { grep -qE '^(proxies|proxy-groups|mixed-port|socks-port|port|rules|mode):' "$1"; }

if [[ -f $SUB_SRC ]]; then
  info "使用本地配置文件: $SUB_SRC"
  cp "$SUB_SRC" "$NEW"
else
  info "下载订阅配置..."
  ok=0
  # 机场按 User-Agent 返回格式，依次尝试常见 Clash UA
  for ua in clash.meta mihomo clash-verge/v2.0 ClashforWindows/0.20 clash; do
    if curl -fsSL --connect-timeout 15 --max-time 120 -A "$ua" -o "$NEW" "$SUB_SRC" \
       && looks_like_yaml "$NEW"; then
      ok=1; info "订阅返回 Clash 格式 (UA=$ua)"; break
    fi
  done
  if [[ $ok != 1 ]]; then
    if [[ -f $NEW ]] && base64 -d "$NEW" 2>/dev/null | head -1 | grep -qE '^(trojan|vmess|vless|ss|ssr|hysteria)://'; then
      die "订阅返回的是 base64 分享链接而非 Clash 配置。请使用机场提供的 Clash 专用订阅地址，或在地址后尝试加 ?flag=clash"
    fi
    die "无法获取 Clash 格式配置，请检查订阅地址是否正确/过期"
  fi
fi

if [[ -f $CONFIG ]]; then
  cp "$CONFIG" "$CONFIG.bak.$(date +%Y%m%d%H%M%S)"
  info "已备份原配置"
fi
mv "$NEW" "$CONFIG"
chmod 600 "$CONFIG"
info "配置已就位: $CONFIG"

# ---------- GeoIP / GeoSite 数据库 ----------
# GitHub 直连常超时，优先走 jsDelivr 镜像
geo_fetch() {  # geo_fetch <文件名>
  local f=$1
  if [[ -s $CONFIG_DIR/$f ]]; then info "$f 已存在，跳过"; return 0; fi
  fetch "$GEOMIRROR/$f" "$CONFIG_DIR/$f" 3 \
    || fetch "$GITHUB/MetaCubeX/meta-rules-dat/releases/latest/download/$f" "$CONFIG_DIR/$f" 3 \
    || { warn "$f 下载失败，继续（若后续校验报 GeoIP 错误需手动补齐）"; return 0; }
  info "已下载 $f ($(du -h "$CONFIG_DIR/$f" | cut -f1))"
}

geo_fetch geoip.metadb
if grep -q 'GEOSITE' "$CONFIG";            then geo_fetch geosite.dat; fi
if grep -qE 'geodata-mode: *true' "$CONFIG"; then geo_fetch geoip.dat; fi

# ---------- 校验并启动 ----------
info "校验配置..."
if ! mihomo -t -d "$CONFIG_DIR" >/tmp/mihomo-test.log 2>&1; then
  if grep -q 'MMDB' /tmp/mihomo-test.log; then
    warn "缺少 MMDB，尝试补齐 country.mmdb 后重试..."
    geo_fetch country.mmdb
  fi
  mihomo -t -d "$CONFIG_DIR" >/tmp/mihomo-test.log 2>&1 \
    || { tail -5 /tmp/mihomo-test.log; die "配置校验失败"; }
fi
info "配置校验通过"

systemctl enable mihomo >/dev/null 2>&1
systemctl restart mihomo
sleep 2
systemctl is-active --quiet mihomo \
  || { journalctl -u mihomo -n 20 --no-pager; die "mihomo 启动失败"; }
info "服务已启动并设为开机自启"

# ---------- 验证连通性 ----------
PORT=$(grep -E '^(mixed-port|port|socks-port):' "$CONFIG" | head -1 | awk '{print $2}' | tr -d "'\"")
PORT=${PORT:-7890}
CODE=$(curl -s -x "http://127.0.0.1:$PORT" --max-time 20 -o /dev/null -w '%{http_code}' https://www.google.com/generate_204 || true)
EXIT_IP=$(curl -s -x "http://127.0.0.1:$PORT" --max-time 20 https://api.ip.sb/ip || true)
CTRL=$(grep -E '^external-controller:' "$CONFIG" | head -1 | awk '{print $2}' | tr -d "'\"")

echo
info "================ 部署完成 ================"
echo "  代理端口(HTTP/SOCKS): $PORT"
echo "  终端使用代理: export http_proxy=http://127.0.0.1:$PORT https_proxy=http://127.0.0.1:$PORT"
if [[ $CODE == 204 || $CODE == 200 ]]; then
  echo "  连通性: ✓ 正常 (google HTTP $CODE)"
else
  warn "连通性: 未通过 (HTTP $CODE)，服务已启动，可稍后手动测试"
fi
[[ -n $EXIT_IP ]] && echo "  出口 IP: $EXIT_IP"
[[ -n $CTRL ]] && echo "  管理面板: 浏览器打开 https://d.metacubex.one ，填入 $CTRL"
echo "  配置文件: $CONFIG"
echo "=========================================="
