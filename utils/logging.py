"""日志体系：控制台 + 轮转文件 + SQLite 缓冲（供 Web 台查看）。

- ``setup_logging(cfg)`` 配置 root logger，返回 DBLogHandler（若有）供 main 启动落盘协程。
- ``DBLogHandler.emit`` 仅写入内存 deque（线程安全），不阻塞事件循环；
  ``log_drainer`` 协程周期性把缓冲落库。
- ``install_rotating_stdout(cfg)`` 接管 stdout/stderr：service 只把 fd 追加到
  stdout.log 无法限长，这里在 Python 层超限滚动（与 tg115bot.log 同规则）。
- ``log_retention_loop(cfg)`` 每小时清理 logs/ 下超过 retention_days 的 *.log*。
"""
from __future__ import annotations

import asyncio
import collections
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import Optional

from persistence.models import LogRow

log = logging.getLogger(__name__)

_FMT = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")


class DBLogHandler(logging.Handler):
    """把日志缓冲到内存 deque，由后台协程落库。"""

    def __init__(self, buffer_size: int = 200):
        super().__init__()
        self._buf: collections.deque = collections.deque(maxlen=max(10, buffer_size))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buf.append(LogRow(
                ts=record.created, level=record.levelname,
                logger=record.name, message=self.format(record),
            ))
        except Exception:  # noqa: BLE001 -- 日志失败绝不能抛
            pass

    def drain(self) -> list:
        out = []
        while self._buf:
            out.append(self._buf.popleft())
        return out


def setup_logging(cfg) -> Optional[DBLogHandler]:
    """配置 root logger：控制台 + 轮转文件 +（可选）DB 缓冲。返回 DBLogHandler 或 None。"""
    root = logging.getLogger()
    root.setLevel(cfg.logging.level.upper())
    # 清理可能存在的旧 handler（热重载/测试场景）
    for h in list(root.handlers):
        root.removeHandler(h)

    sh = logging.StreamHandler()
    sh.setFormatter(_FMT)
    root.addHandler(sh)

    try:
        Path(cfg.logging.file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            cfg.logging.file, maxBytes=cfg.logging.max_bytes,
            backupCount=cfg.logging.backup_count, encoding="utf-8",
        )
        fh.setFormatter(_FMT)
        root.addHandler(fh)
    except OSError as e:  # noqa: BLE001 -- 日志目录不可写不应阻断启动
        log.warning("无法创建日志文件 %s: %r（仅控制台输出）", cfg.logging.file, e)

    db_handler: Optional[DBLogHandler] = None
    if cfg.logging.db_buffer > 0:
        db_handler = DBLogHandler(cfg.logging.db_buffer)
        db_handler.setFormatter(_FMT)
        root.addHandler(db_handler)
    return db_handler


async def log_drainer(db, handler: DBLogHandler, interval: float = 2.0) -> None:
    """周期性把 DBLogHandler 缓冲落库。main.py 作为后台任务启动。"""
    if db is None:
        return
    while True:
        try:
            await asyncio.sleep(interval)
            entries = handler.drain()
            if entries:
                await db.insert_logs(entries)
        except asyncio.CancelledError:
            # 退出前冲一次
            entries = handler.drain()
            if entries:
                try:
                    await db.insert_logs(entries)
                except Exception:  # noqa: BLE001
                    pass
            raise
        except Exception:  # noqa: BLE001 -- 落盘异常不应终止 drainer
            log.debug("log_drainer 落盘异常", exc_info=True)


class RotatingStdout:
    """接管 sys.stdout/stderr 的轮转写入器（print/traceback/第三方输出统一走这）。

    service.py 把子进程 fd 指到 stdout.log 后无法限长（fd 级 rename 子进程无感知），
    在 Python 层接管：超 max_bytes 滚动为 .1/.2/…，与 tg115bot.log 的
    RotatingFileHandler 同规则。二进制追加写，按真实字节数计。
    """

    def __init__(self, path: Path, max_bytes: int, backup_count: int):
        self._path = Path(path)
        self._max = max_bytes if max_bytes > 0 else 10 * 1024 * 1024
        self._count = max(1, backup_count)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._open()

    def _open(self) -> None:
        self._f = open(self._path, "ab")
        try:
            self._size = self._path.stat().st_size
        except OSError:
            self._size = 0

    def _rotate(self) -> None:
        try:
            self._f.close()
            for i in range(self._count - 1, 0, -1):
                src = Path(f"{self._path}.{i}")
                if src.exists():
                    os.replace(src, Path(f"{self._path}.{i + 1}"))
            if self._path.exists():
                os.replace(self._path, Path(f"{self._path}.1"))
        finally:
            self._open()      # 滚动失败也要保证流可用

    def write(self, s) -> int:
        data = s.encode("utf-8", errors="replace") if isinstance(s, str) else s
        self._f.write(data)
        self._size += len(data)
        if self._size >= self._max:
            try:
                self._rotate()
            except OSError:   # noqa: BLE001 -- 轮转失败不影响输出
                pass
        return len(data)

    def flush(self) -> None:
        self._f.flush()

    def fileno(self) -> int:
        return self._f.fileno()

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True


def install_rotating_stdout(cfg) -> RotatingStdout:
    """接管 stdout/stderr 到 <logs>/stdout.log（10M×backup_count 滚动）。尽早调用。"""
    w = RotatingStdout(Path(cfg.logging.file).parent / "stdout.log",
                       cfg.logging.max_bytes, cfg.logging.backup_count)
    sys.stdout = w        # type: ignore[assignment]
    sys.stderr = w        # type: ignore[assignment]
    return w


def cleanup_old_logs(logs_dir: Path, retention_days: int) -> int:
    """删除 logs_dir 下 mtime 超过保留期的 *.log*（主日志+拆分件统一治理）。

    当前活跃文件 mtime 恒新不会被删。返回删除数。
    """
    cutoff = time.time() - max(0, retention_days) * 86400
    removed = 0
    try:
        files = list(Path(logs_dir).glob("*.log*"))
    except OSError:
        return 0
    for f in files:
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


async def log_retention_loop(cfg, interval: float = 3600.0) -> None:
    """每小时清理过期日志（启动时先清一次）。main.py 作为后台任务启动。"""
    logs_dir = Path(cfg.logging.file).parent
    while True:
        removed = cleanup_old_logs(logs_dir, cfg.logging.retention_days)
        if removed:
            log.info("日志保留清理：删除 %d 个超过 %d 天的文件", removed,
                     cfg.logging.retention_days)
        await asyncio.sleep(interval)
