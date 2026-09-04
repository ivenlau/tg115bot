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
