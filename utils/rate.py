"""限速与退避工具：115 请求最小间隔 + 抖动 + 指数退避重试。"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")


class RateLimiter:
    """异步最小间隔限速器（带抖动），串行化关键 115 请求以降低风控。"""

    def __init__(self, min_interval: float = 0.3, jitter: float = 0.2):
        self.min_interval = min_interval
        self.jitter = jitter
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self.min_interval - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait + random.uniform(0, self.jitter))
            self._last = time.monotonic()


async def with_backoff(
    fn: Callable[[], Awaitable[T]],
    *,
    base: float = 2.0,
    max_retries: int = 5,
    no_retry: tuple = (),
    on_retry: Optional[Callable[[int, float, BaseException], None]] = None,
) -> T:
    """指数退避重试。遇到 -1/超时类错误自动重试，达到上限抛出。"""
    last_exc: Optional[BaseException] = None
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise  # 取消不重试，立即生效
        except no_retry:
            raise  # 调用方声明的不可重试异常（如 TaskCancelled）
        except Exception as e:  # noqa: BLE001 —— 115 风控/网络错误都需重试
            last_exc = e
            if attempt == max_retries:
                raise
            delay = base * (2 ** attempt) + random.uniform(0, 1)
            if on_retry:
                on_retry(attempt, delay, e)
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


def flood_wait_secs(exc: BaseException) -> Optional[float]:
    """若异常是 TG FloodWait，返回需等待秒数；否则 None。

    两条识别路径，任一命中即可：
      1. pyrogram 可导入时用 isinstance（最准）
      2. 兜底按异常类名 + value 属性（兼容未装/pyrogram 版本差异/telethon 等）
    """
    try:
        from pyrogram.errors import FloodWait  # type: ignore
        if isinstance(exc, FloodWait):
            return float(getattr(exc, "value", 1) or 1)
    except Exception:  # noqa: BLE001 -- pyrogram 未装时正常
        pass
    if type(exc).__name__ == "FloodWait" and hasattr(exc, "value"):
        return float(getattr(exc, "value", 1) or 1)
    return None


async def with_flood_wait(
    fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 3,
) -> T:
    """执行返回协程的工厂 ``fn``，遇 FloodWait 睡眠后重试；其它异常直接抛。"""
    last_exc: Optional[BaseException] = None
    for _ in range(max_retries + 1):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            secs = flood_wait_secs(e)
            if secs is None:
                raise
            last_exc = e
            await asyncio.sleep(secs + 0.5)
    assert last_exc is not None
    raise last_exc
