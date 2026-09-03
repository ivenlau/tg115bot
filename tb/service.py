"""跨平台服务管理：替代 scripts/service.sh 与 scripts/service.ps1。

语义对齐两个老脚本：
- PID 文件防双启，校验进程命令行含安装目录（防 PID 复用误杀）
- 启动后 2s 存活检查，失败打印日志尾部
- 停止先优雅（POSIX SIGTERM / Windows taskkill 软尝试）10s 后强杀进程树
- log = 尾部 N 行 + follow（TTY）；管道/重定向只给尾部（cron 安全）

跨平台要点：Popen 直接把 stdout/stderr 接到日志文件（句柄由子进程持有，
与 shell `>>` 重定向等价），因此不再需要 service.ps1 的 .cmd 中转技巧；
Linux 用 start_new_session 脱离会话，Windows 用 CREATE_NO_WINDOW 无窗口后台。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from tb import INSTALL_DIR

PID_FILE = INSTALL_DIR / "run" / "tg115bot.pid"
STDOUT_LOG = INSTALL_DIR / "logs" / "stdout.log"

GRACEFUL_SECS = 10


def _norm(s: str) -> str:
    """路径归一（Windows 大小写/分隔符不敏感比对用）。"""
    return os.path.normcase(str(s))


def live_pid() -> int | None:
    """读 PID 文件并校验进程身份；无效/不存活返回 None。"""
    import psutil

    try:
        raw = PID_FILE.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        pid = int(raw[0]) if raw else 0
    except (OSError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        proc = psutil.Process(pid)
        cmd = _norm(" ".join(proc.cmdline()))
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None
    # 双条件：venv 的 python 路径本身就在安装目录下（argv[0] 必含 INSTALL_DIR），
    # 故再加 main.py 才认——避免把 manual.py / check115.py 等同 venv 进程误判为服务
    if _norm(INSTALL_DIR) not in cmd or "main.py" not in cmd:
        return None
    return pid


def _tail_text(n: int) -> str:
    try:
        data = STDOUT_LOG.read_bytes()
    except OSError:
        return ""
    lines = data.decode("utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])


def do_start() -> int:
    pid = live_pid()
    if pid:
        print(f"[!] 已在运行 (PID {pid})，如需重启用: tb restart")
        return 0
    if not (INSTALL_DIR / "config.yaml").exists():
        print("[x] 缺少 config.yaml（先跑: tb init）")
        return 1
    for d in ("run", "logs"):
        (INSTALL_DIR / d).mkdir(parents=True, exist_ok=True)
    PID_FILE.unlink(missing_ok=True)

    logf = open(STDOUT_LOG, "ab", buffering=0)
    try:
        kwargs: dict = dict(
            cwd=str(INSTALL_DIR),
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
        )
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen([sys.executable, "main.py"], **kwargs)
    finally:
        logf.close()  # 父进程关自己的副本，子进程持有的句柄不受影响
    PID_FILE.write_text(f"{proc.pid}\n", encoding="ascii")

    time.sleep(2)
    if proc.poll() is not None:
        PID_FILE.unlink(missing_ok=True)
        print(f"[x] 启动失败，最近日志：\n{_tail_text(15)}")
        return 1
    print(f"[+] 已启动 (PID {proc.pid})")
    print("[+] 日志: tb log")
    return 0


def _alive(pid: int) -> bool:
    import psutil

    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def do_stop() -> int:
    import psutil

    pid = live_pid()
    if not pid:
        print("[!] 未在运行")
        PID_FILE.unlink(missing_ok=True)
        return 0
    print(f"[+] 正在停止 (PID {pid}) …")
    graceful = False
    if os.name == "nt":
        # Windows 无 SIGTERM；taskkill 不带 /F 只对有窗口进程发 WM_CLOSE，
        # 对无窗口后台进程通常直接失败——失败了跳过优雅窗口立即强杀（与
        # service.ps1 行为一致，避免无谓卡顿）
        r = subprocess.run(["taskkill", "/T", "/PID", str(pid)],
                           capture_output=True, text=True)
        graceful = r.returncode == 0
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            graceful = True
        except (ProcessLookupError, PermissionError):
            pass
    if graceful:
        deadline = time.monotonic() + GRACEFUL_SECS
        while time.monotonic() < deadline:
            if not _alive(pid):
                break
            time.sleep(0.5)
    try:
        proc = psutil.Process(pid)
        targets = [proc, *proc.children(recursive=True)]
    except psutil.Error:
        # NoSuchProcess 竞态：SIGTERM 后进程恰在此间隙退出（children() 也会抛，
        # 之前的裸调用就是 TUI 点「重启」偶发崩溃面板的根因）——已死无需强杀
        targets = []
    if targets:
        if graceful:
            print("[!] 优雅退出超时，强制结束")
        for p in targets:
            try:
                p.kill()
            except psutil.Error:
                pass
    PID_FILE.unlink(missing_ok=True)
    print("[+] 已停止")
    return 0


def do_restart() -> int:
    rc = do_stop()
    if rc != 0:
        return rc
    return do_start()


def _fmt_uptime(secs: float) -> str:
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return f"{d}d {h:02d}:{m:02d}:{s:02d}" if d else f"{h:02d}:{m:02d}:{s:02d}"


def do_status() -> int:
    pid = live_pid()
    if not pid:
        print("[!] 未在运行")
        return 1
    import psutil

    try:
        proc = psutil.Process(pid)
        mb = proc.memory_info().rss / 1048576
        up = time.time() - proc.create_time()
    except psutil.Error:      # 身份校验与取数之间退出的竞态：按未运行处理
        print("[!] 未在运行（进程刚退出）")
        return 1
    print(f"[+] 运行中 (PID {pid})  内存 {mb:.0f}MB  已运行 {_fmt_uptime(up)}")
    print(f"    安装目录: {INSTALL_DIR}")
    return 0


def do_log(tail: int = 50) -> int:
    if not STDOUT_LOG.exists():
        print(f"[x] 日志不存在: {STDOUT_LOG}")
        return 1
    print(_tail_text(tail))
    if not sys.stdout.isatty():
        return 0  # 管道/重定向：只给尾部，不挂 follow（cron 安全）
    print("---- 跟踪中（Ctrl+C 退出）----")
    try:
        with open(STDOUT_LOG, "rb") as f:
            f.seek(0, 2)  # 跳到末尾，只看新增
            while True:
                chunk = f.read(4096)
                if chunk:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.flush()
                else:
                    time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
