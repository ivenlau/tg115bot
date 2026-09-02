#!/usr/bin/env bash
#
# install.sh — tg115bot 一键安装（Linux / macOS）
#
#   curl -fsSL https://raw.githubusercontent.com/ivenlau/tg115bot/main/scripts/install.sh | bash
#
# 做四件事：
#   1. 克隆（或 git pull 更新）仓库到 ~/.tg115bot（TG115BOT_HOME 可覆盖）
#   2. 检测 Python >= 3.12，建 .venv 并装依赖（失败走清华镜像）
#   3. 写 tb 命令 shim 到 ~/.local/bin（TG115BOT_BIN 可覆盖）
#   4. 打印下一步（tb init → tb start）
#
# 已有安装重复执行安全：代码更新 + 依赖补齐 + shim 重写，不动 config.yaml。
#
set -euo pipefail

REPO="https://github.com/ivenlau/tg115bot.git"
INSTALL_DIR="${TG115BOT_HOME:-$HOME/.tg115bot}"
BIN_DIR="${TG115BOT_BIN:-$HOME/.local/bin}"

if [[ -t 1 ]]; then
  C_G=$'\e[32m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_0=$'\e[0m'
else
  C_G=""; C_Y=""; C_R=""; C_0=""
fi
info() { echo "${C_G}[+]${C_0} $*"; }
warn() { echo "${C_Y}[!]${C_0} $*"; }
die()  { echo "${C_R}[x]${C_0} $*" >&2; exit 1; }

# ---------- 前置 ----------
command -v git >/dev/null 2>&1 || die "缺少 git（apt install git / brew install git）"

# ---------- Python >= 3.12 ----------
PY=""
for c in python3.12 python3.13 python3; do
  if command -v "$c" >/dev/null 2>&1 \
     && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
[[ -n $PY ]] || die "需要 Python >= 3.12（apt install python3.12 或 https://python.org）"
info "Python: $($PY --version 2>&1)"

# ---------- 代码 ----------
if [[ -d $INSTALL_DIR/.git ]]; then
  info "更新已有安装: $INSTALL_DIR"
  git -C "$INSTALL_DIR" pull --ff-only || warn "git pull 失败（本地有改动？），沿用当前代码继续"
else
  info "克隆仓库 -> $INSTALL_DIR"
  git clone --depth 1 "$REPO" "$INSTALL_DIR" || die "克隆失败（网络？可设 https_proxy 后重试）"
fi

# ---------- venv + 依赖（幂等） ----------
VENV_PY="$INSTALL_DIR/.venv/bin/python"
if [[ ! -x $VENV_PY ]] || ! "$VENV_PY" -c 'import sys' 2>/dev/null; then
  "$PY" -m venv "$INSTALL_DIR/.venv" || die "venv 创建失败（apt install python3-venv）"
fi
"$VENV_PY" -m pip install -q --upgrade pip wheel 2>/dev/null || true
if ! "$VENV_PY" -m pip install -q -r "$INSTALL_DIR/requirements.txt"; then
  warn "直连 PyPI 失败，改用清华镜像重试…"
  "$VENV_PY" -m pip install -q -r "$INSTALL_DIR/requirements.txt" \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    || die "依赖安装失败，检查网络（可能需要代理）"
fi
info "依赖就绪"

# ---------- tb 命令 ----------
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/tb" <<SHIM
#!/usr/bin/env bash
exec "$VENV_PY" -m tb "\$@"
SHIM
chmod +x "$BIN_DIR/tb"
info "已安装命令: $BIN_DIR/tb"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR 不在 PATH。执行: export PATH=\"$BIN_DIR:\$PATH\"（并把这行写进 ~/.bashrc）" ;;
esac

echo
info "================ 安装完成 ================"
echo "  代码目录:   $INSTALL_DIR"
echo "  下一步:"
echo "    tb init      # 首次配置（依赖/代理/扫码授权，交互式）"
echo "    tb doctor    # 一键体检"
echo "    tb start     # 启动服务；裸 tb 进交互界面"
echo "    tb --help    # 全部命令"
echo "=========================================="
