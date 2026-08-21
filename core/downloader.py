"""TG 下载：Pyrogram ``stream_media`` —— 单路(内联 SHA1) 或 多路并行分片(seek 写盘)。

stream_media 分片语义（对照本机 pyrofork 2.3.x 源码 stream_media.py / client.get_file）：
  - 签名 ``stream_media(message, limit=0, offset=0)``，**无 chunk_size 参数**；
    每片固定 1 MiB。原版 pyrogram 2.x 有 chunk_size（字节），经 ``_stream_kwargs``
    兼容垫片自适应。
  - ``offset``/``limit`` 单位是**片的个数**（1 MiB 一片），不是字节。

性能策略：
  - workers <= 1：顺序流式，边下边算 SHA1（零额外读盘）。
  - workers  > 1：把文件按 1 MiB 片切成 N 段，N 路 ``stream_media(offset, limit)``
                  并发，各自 seek 写到预分配文件的正确字节位置；下完后做一次
                  顺序读算整文件 SHA1（乱序分片无法合并 SHA1；刚写的文件多在页缓存）。
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import math
from pathlib import Path
from typing import Awaitable, Callable, Optional, Tuple

import aiofiles

from core.queue import TaskCancelled

log = logging.getLogger(__name__)

# stream_media 的固定分片单元（pyrofork 硬约定 1 MiB）
STREAM_UNIT = 1024 * 1024

ProgressCb = Callable[[int, int], Awaitable[None]]


def _stream_kwargs(pyro_client) -> dict:
    """兼容垫片：原版 pyrogram 的 stream_media 接受 chunk_size（字节），pyrofork 不接受。
    传 1 MiB 使两个实现的 offset/limit 单位一致（均为 1 MiB 片数）。"""
    try:
        params = inspect.signature(pyro_client.stream_media).parameters
    except (TypeError, ValueError):
        return {}
    if "chunk_size" in params:
        return {"chunk_size": STREAM_UNIT}
    return {}


def media_info(message) -> Tuple[str, int]:
    """从 Pyrogram Message 提取 (filename, size)。含 photo（无文件名，按日期命名）。"""
    photo = getattr(message, "photo", None)
    if photo:
        from datetime import datetime as _dt
        d = photo.date
        if isinstance(d, _dt):                      # pyrogram 直接给 datetime 对象
            ts = d.strftime("%Y%m%d_%H%M%S")
        elif d:                                     # 兼容 int 时间戳
            ts = _dt.fromtimestamp(d).strftime("%Y%m%d_%H%M%S")
        else:
            ts = "photo"
        return f"photo_{ts}.jpg", photo.file_size or 0
    for attr in ("video", "animation", "audio", "voice", "video_note", "document"):
        m = getattr(message, attr, None)
        if m:
            name = getattr(m, "file_name", None) or ""
            size = getattr(m, "file_size", 0) or 0
            if not name:
                ext = {
                    "video": ".mp4", "animation": ".mp4", "audio": ".mp3",
                    "voice": ".ogg", "video_note": ".mp4", "document": "",
                }.get(attr, "")
                name = f"{attr}_{getattr(m, 'file_id', 'file')[-8:]}{ext}"
            return name, size
    return "media", 0


async def compute_sha1(path: Path, cancel_event: Optional[asyncio.Event] = None) -> str:
    """顺序读文件算整文件 SHA1（并行下载后用）。"""
    sha = hashlib.sha1()
    async with aiofiles.open(path, "rb") as f:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise TaskCancelled()
            chunk = await f.read(1024 * 1024)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


async def download(
    pyro_client,
    message,
    dest: Path,
    *,
    size: int = 0,
    workers: int = 1,
    chunk_size: int = STREAM_UNIT,   # 兼容旧签名；实际分片单元由库决定（1 MiB）
    on_progress: Optional[ProgressCb] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> Tuple[int, str]:
    """下载 message 媒体到 dest，返回 (写入字节数, SHA1 hex)。

    workers>1 且 size>0 时启用并行分片；否则走顺序内联 SHA1 路径。
    """
    if workers > 1 and size > 0:
        return await _download_parallel(
            pyro_client, message, dest, size, workers, on_progress, cancel_event
        )
    return await _download_sequential(
        pyro_client, message, dest, size, on_progress, cancel_event
    )


async def _download_sequential(pyro_client, message, dest, size, on_progress, cancel_event):
    extra = _stream_kwargs(pyro_client)
    sha = hashlib.sha1()
    written = 0
    async with aiofiles.open(dest, "wb") as f:
        async for chunk in pyro_client.stream_media(message, **extra):
            if cancel_event is not None and cancel_event.is_set():
                raise TaskCancelled()
            if not chunk:
                continue
            await f.write(chunk)
            sha.update(chunk)
            written += len(chunk)
            if on_progress:
                await on_progress(written, size)
    return written, sha.hexdigest()


def _split_ranges(size: int, workers: int):
    """把 [0,size) 按 STREAM_UNIT 片切成 workers 段（连续整片，末段含余量）。

    返回 [(首片序号, 片数, 字节偏移, 字节长度), ...]；offset/limit 给 stream_media，
    字节偏移/长度用于本地 seek 写盘。
    """
    total_units = max(1, math.ceil(size / STREAM_UNIT))
    n = max(1, min(workers, total_units))
    units_per = total_units // n
    ranges = []
    for i in range(n):
        count = units_per if i < n - 1 else total_units - units_per * (n - 1)
        first = i * units_per
        byte_off = first * STREAM_UNIT
        byte_len = min(count * STREAM_UNIT, size - byte_off)
        ranges.append((first, count, byte_off, byte_len))
    return ranges


async def _download_parallel(pyro_client, message, dest, size, workers, on_progress, cancel_event):
    extra = _stream_kwargs(pyro_client)
    ranges = _split_ranges(size, workers)
    bytes_done = [0] * len(ranges)

    async def _worker(idx, first_unit, n_units, byte_off, byte_len):
        async with aiofiles.open(dest, "r+b") as f:
            await f.seek(byte_off)
            async for chunk in pyro_client.stream_media(
                message, offset=first_unit, limit=n_units, **extra
            ):
                if cancel_event is not None and cancel_event.is_set():
                    raise TaskCancelled()
                if not chunk:
                    continue
                await f.write(chunk)
                bytes_done[idx] += len(chunk)
                if on_progress:
                    await on_progress(sum(bytes_done), size)
        if bytes_done[idx] != byte_len:
            log.warning("分片#%d 字节数不符: 期望 %d 实得 %d（可能流提前结束）",
                        idx, byte_len, bytes_done[idx])

    tasks = [asyncio.create_task(_worker(i, *r)) for i, r in enumerate(ranges)]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    written = sum(bytes_done)
    if on_progress:
        await on_progress(written, size)
    sha = await compute_sha1(dest, cancel_event)
    return written, sha
