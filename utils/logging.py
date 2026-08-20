"""日志体系：控制台 + 轮转文件 + SQLite 缓冲（供 Web 台查看）。

- ``setup_logging(cfg)`` 配置 root logger，返回 DBLogHandler（若有）供 main 启动落盘协程。
- ``DBLogHandler.emit`` 仅写入内存 deque（线程安全），不阻塞事件循环；
  ``log_drainer`` 协程周期性把缓冲落库。
"""
from __future__ import annotations

import asyncio
import collections
import logging
import logging.handlers
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
