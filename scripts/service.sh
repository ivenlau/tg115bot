#!/usr/bin/env bash
#
# service.sh — tg115bot 服务管理（后台运行 + PID 文件 + 日志轮转由程序自身负责）
#
# 用法:
#   ./scripts/service.sh start     启动（已在运行则提示）
#   ./scripts/service.sh stop      优雅停止（SIGTERM，10s 后强杀）
#   ./scripts/service.sh restart   重启（更新代码/配置后用这个）
#   ./scripts/service.sh status    查看运行状态
#   ./scripts/service.sh log [N]   跟踪运行日志（tail -f，N=尾部行数默认50）
#
# 说明:
#   - 用 nohup 后台运行，SSH 断开不影响
#   - PID 记录在 run/tg115bot.pid，防重复启动
#   - 标准输出/错误追加到 logs/stdout.log（业务日志另在 logs/tg115bot.log 轮转）
#   - 自动使用项目目录下的 .venv（不存在则回退系统 python3）
#
set -euo pipefail

# ── 定位项目根目录（脚本在 scripts/ 下）───────────────────────────────────
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

PID_FILE="$DIR/run/tg115bot.pid"
STDOUT_LOG="$DIR/logs/stdout.log"

# ── Python 解释器：优先 venv ─────────────────────────────────────────────
if [[ -x "$DIR/.venv/bin/python" ]]; then
  PYTHON="$DIR/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
  echo "[!] 未找到 .venv，使用系统 $PYTHON（建议: python3.12 -m venv .venv）"
fi

mkdir -p "$DIR/run" "$DIR/logs"

c_g=$'\e[32m'; c_y=$'\e[33m'; c_r=$'\e[31m'; c_0=$'\e[0m'
info() { echo "${c_g}[+]${c_0} $*"; }
warn() { echo "${c_y}[!]${c_0} $*"; }
die()  { echo "${c_r}[x]${c_0} $*" >&2; exit 1; }

# ── PID 管理 ────────────────────────────────────────────────────────────
pid_alive() {  # 检查 PID 文件指向的进程是否存活且是我们的 python
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  # 确认是本项目的 python 进程（防止 PID 复用误杀）
  grep -q "tg115bot" "/proc/$pid/cmdline" 2>/dev/null \
    || tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "main\.py" || return 1
  REPLY_PID="$pid"
}

do_start() {
  if pid_alive; then
    warn "已在运行 (PID $REPLY_PID)，如需重启用: $0 restart"
    exit 0
  fi
  rm -f "$PID_FILE"
  [[ -f config.yaml ]] || die "缺少 config.yaml（cp config.yaml.example config.yaml 后填写）"

  nohup "$PYTHON" main.py >> "$STDOUT_LOG" 2>&1 &
  local pid=$!
  echo "$pid" > "$PID_FILE"
  sleep 2
  if kill -0 "$pid" 2>/dev/null; then
    info "已启动 (PID $pid)"
    info "日志: tail -f $STDOUT_LOG   （或 $0 log）"
    # 打印启动段日志帮助确认
    echo "---- 启动日志 ----"
    tail -n 8 "$STDOUT_LOG" | sed 's/^/    /'
  else
    rm -f "$PID_FILE"
    die "启动失败，最近日志："
  fi
}

do_stop() {
  if ! pid_alive; then
    warn "未在运行"
    rm -f "$PID_FILE"
    exit 0
  fi
  local pid="$REPLY_PID"
  info "正在停止 (PID $pid) …"
  kill "$pid" 2>/dev/null || true
  # 等优雅退出（进行中的上传会收尾），最多 10s
  for i in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$pid" 2>/dev/null; then
    warn "优雅退出超时，强制结束"
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  info "已停止"
}

do_status() {
  if pid_alive; then
    local pid="$REPLY_PID"
    local rss uptime
    rss="$(awk '/VmRSS/{print $2}' "/proc/$pid/status" 2>/dev/null || echo '?')"
    uptime="$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ' || echo '?')"
    info "运行中 (PID $pid)  内存 $(( ${rss:-0} / 1024 ))MB  已运行 $uptime"
  else
    warn "未在运行"
    exit 1
  fi
}

do_log() {
  local n="${1:-50}"
  [[ -f "$STDOUT_LOG" ]] || die "日志不存在: $STDOUT_LOG"
  info "跟踪 $STDOUT_LOG （Ctrl+C 退出）"
  tail -n "$n" -f "$STDOUT_LOG"
}

case "${1:-}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; do_start ;;
  status)  do_status ;;
  log)     shift; do_log "$@" ;;
  *)
    cat <<EOF
用法: $0 {start|stop|restart|status|log [N]}

  start     后台启动（nohup，SSH 断开不影响）
  stop      优雅停止（10s 后强杀）
  restart   重启
  status    运行状态（PID/内存/时长）
  log [N]   跟踪日志（默认尾部 50 行）
EOF
    exit 1
    ;;
esac
