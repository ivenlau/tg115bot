"""任务队列 + worker。

Phase 1：单 worker 串行处理（concurrency=1），保证 115 风控安全、行为可预测。
Phase 2：提升 concurrency 并发处理多任务。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

log = logging.getLogger(__name__)


class TaskCancelled(Exception):
    """任务被用户取消（/cancel）。"""


@dataclass
class Task:
    user_id: int
    message: Any                 # pyrogram.types.Message
    filename: str
    size: int
    target_dir: str
    tracking_chat_id: int
    tracking_message_id: int
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    source: str = "manual"       # "manual" | "channel"
    channel_id: int = 0          # 频道监控来源时记录频道 id


class TaskQueue:
    def __init__(self, concurrency: int, runner: Callable[[Task], Awaitable[None]]):
        self._q: asyncio.Queue[Task] = asyncio.Queue()
        self.concurrency = concurrency
        self._runner = runner
        self._workers: list[asyncio.Task] = []

    async def put(self, task: Task) -> None:
        await self._q.put(task)

    def qsize(self) -> int:
        return self._q.qsize()

    async def start(self) -> None:
        for i in range(self.concurrency):
            self._workers.append(asyncio.create_task(self._worker(i), name=f"tg115-worker-{i}"))
        log.info("任务队列已启动，并发=%d", self.concurrency)

    async def _worker(self, idx: int) -> None:
        while True:
            task = await self._q.get()
            try:
                await self._runner(task)
            except Exception:  # noqa: BLE001
                log.exception("worker-%d 任务异常: %s", idx, task.filename)
            finally:
                self._q.task_done()

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
