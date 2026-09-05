"""日志滚动/保留测试：RotatingStdout 字节上限滚动 + cleanup_old_logs 保留期清理。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.logging import RotatingStdout, cleanup_old_logs  # noqa: E402


def test_rotating_stdout_rolls_at_max() -> None:
    """超过 max_bytes 滚动为 .1/.2，且不超过 backup_count 份数。"""
    tmp = Path(tempfile.mkdtemp(prefix="tg115_rot_"))
    try:
        w = RotatingStdout(tmp / "stdout.log", max_bytes=200, backup_count=2)
        for i in range(40):
            w.write(f"line-{i:03d} " + "x" * 20 + "\n")   # ~30B/行
        w.flush()
        assert (tmp / "stdout.log").stat().st_size <= 200 + 64, \
            "主文件应不超过上限（含单次写入余量）"
        assert (tmp / "stdout.log.1").exists()
        assert (tmp / "stdout.log.2").exists()
        assert not (tmp / "stdout.log.3").exists(), "backup_count=2 最多滚动到 .2"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cleanup_old_logs_removes_expired_keeps_fresh() -> None:
    """过期（mtime>保留期）删除，新文件与活跃主日志保留。"""
    tmp = Path(tempfile.mkdtemp(prefix="tg115_ret_"))
    try:
        old = [tmp / "tg115bot.log.3", tmp / "stdout.log.1"]
        fresh = [tmp / "tg115bot.log", tmp / "stdout.log", tmp / "stdout.log.2"]
        for f in old + fresh:
            f.write_text("x")
        expire = time.time() - 8 * 86400
        for f in old:
            os.utime(f, (expire, expire))
        removed = cleanup_old_logs(tmp, retention_days=7)
        assert removed == 2
        assert not any(f.exists() for f in old)
        assert all(f.exists() for f in fresh)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cmd_redirect_reopen_survives() -> None:
    """Windows service.ps1 形态回归：cmd `>>` 独占写句柄下 _open 回退继承句柄，不再炸。

    复刻 run/tg115bot-run.cmd 的「python 脚本 >> 同一 stdout.log 2>&1」+ 脚本内
    RotatingStdout 二次打开同一文件——修复前必 PermissionError（cmd 重定向句柄
    不共享写）。仅 Windows 跑真实形态；POSIX 无共享模式概念，直接跳过。
    """
    if os.name != "nt":
        return
    import subprocess

    tmp = Path(tempfile.mkdtemp(prefix="tg115_cmdred_"))
    try:
        logf = tmp / "stdout.log"
        inner = tmp / "inner.py"
        inner.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, r'{ROOT}')\n"
            "from utils.logging import RotatingStdout\n"
            f"w = RotatingStdout(Path(r'{logf}'), max_bytes=10**6, backup_count=2)\n"
            "w.write('from-rotating-stdout')\n"
            "w.flush()\n",
            encoding="utf-8")
        run_cmd = tmp / "run.cmd"      # 与 service.ps1 生成的 run.cmd 同构
        run_cmd.write_text(
            f'@echo off\r\n"{sys.executable}" "{inner}" >> "{logf}" 2>&1',
            encoding="ascii")
        r = subprocess.run(["cmd", "/c", str(run_cmd)],
                           capture_output=True, timeout=60)
        out = logf.read_text("utf-8", errors="replace")
        assert r.returncode == 0, f"子进程应存活，日志尾: {out[-300:]!r}"
        assert "from-rotating-stdout" in out, "经回退句柄的写入应落到同一文件"
        assert "PermissionError" not in out, "不应再出现共享违规"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
