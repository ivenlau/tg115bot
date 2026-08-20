"""进度回写：聚合 + 节流（默认 2s 一次），避免 TG FloodWait。

若传入 ``task_id``，则同时把实时快照写入 ``state.task_progress`` 供 Web 台展示。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


class ProgressReporter:
    def __init__(self, client, chat_id: int, message_id: int,
                 min_interval: float = 2.0, task_id: Optional[str] = None,
                 filename: str = "", source: str = "manual"):
        self.client = client
        self.chat_id = chat_id
        self.message_id = message_id
        self.min_interval = min_interval
        self._last = 0.0
        self._stage = "处理中"
        self._total = 0
        self._current = 0
        self.task_id = task_id
        self._filename = filename
        self._source = source
        self._started = time.time()
        if task_id:
            from core.app import state
            state.task_progress[task_id] = {
                "task_id": task_id, "filename": filename, "source": source,
                "stage": self._stage, "current": 0, "total": 0,
                "started_at": self._started,
            }

    def set_total(self, total: int) -> None:
        self._total = total or 0
        self._snap(total=self._total)

    async def set_stage(self, stage: str) -> None:
        self._stage = stage
        self._snap(stage=stage)
        await self._edit(f"{stage}\n...")

    async def on_progress(self, current: int, total: int) -> None:
        if total:
            self._total = total
        self._current = current
        self._snap(current=current, total=self._total, stage=self._stage)
        now = time.monotonic()
        # 最后一帧必须发出，中间按节流
        if now - self._last < self.min_interval and self._total and current < self._total:
            return
        self._last = now
        pct = (current * 100 / self._total) if self._total else 0
        await self._edit(
            f"{self._stage}\n{human_bytes(current)} / {human_bytes(self._total)} ({pct:.1f}%)"
        )

    async def final_text(self, text: str) -> None:
        await self._edit(text, final=True)
        self._remove_snap()

    async def _edit(self, text: str, final: bool = False) -> None:
        from utils.rate import with_flood_wait
        try:
            await with_flood_wait(
                lambda: self.client.edit_message_text(self.chat_id, self.message_id, text)
            )
        except Exception as e:  # noqa: BLE001 -- edit 偶发失败不应影响主流程
            log.debug("edit_message_text 失败: %r", e)

    def _snap(self, **kw) -> None:
        if not self.task_id:
            return
        from core.app import state
        entry = state.task_progress.get(self.task_id)
        if entry is None:
            entry = {"task_id": self.task_id, "filename": self._filename,
                     "source": self._source, "stage": self._stage, "current": 0,
                     "total": 0, "started_at": self._started}
            state.task_progress[self.task_id] = entry
        entry.update(kw)

    def _remove_snap(self) -> None:
        if self.task_id:
            from core.app import state
            state.task_progress.pop(self.task_id, None)
