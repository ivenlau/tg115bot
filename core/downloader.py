"""TG 下载：Pyrogram ``stream_media`` —— 单路(内联 SHA1) 或 多路并行分片(seek 写盘)。

性能策略：
  - workers <= 1：顺序流式，边下边算 SHA1（零额外读盘）。
  - workers  > 1：把文件按 offset 切成 N 段，N 路 ``stream_media(offset, limit)`` 并发，
                  各自 seek 写到预分配文件的正确 offset；下完后做**一次顺序读**算整文件 SHA1
                  （SHA1 不可由乱序分片合并，故并行时无法边下边算；刚写的文件多在页缓存，代价低）。

参数 `offset` 单位为字节，`limit` 为分片**个数**（Pyrogram 语义）。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from pathlib import Path
from typing import Awaitable, Callable, Optional, Tuple

import aiofiles

from core.queue import TaskCancelled

log = logging.getLogger(__name__)

# Pyrogram stream_media 的 chunk_size 上限约 512KB，须为 4096 倍数
MAX_CHUNK = 524288

ProgressCb = Callable[[int, int], Awaitable[None]]


def _clamp_chunk(n: int) -> int:
    n = max(4096, min(n or MAX_CHUNK, MAX_CHUNK))
    return n - (n % 4096)


def media_info(message) -> Tuple[str, int]:
    """从 Pyrogram Message 提取 (filename, size)。"""
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
    chunk_size: int = MAX_CHUNK,
    on_progress: Optional[ProgressCb] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> Tuple[int, str]:
    """下载 message 媒体到 dest，返回 (写入字节数, SHA1 hex)。

    workers>1 且 size>0 时启用并行分片；否则走顺序内联 SHA1 路径。
    """
    chunk_size = _clamp_chunk(chunk_size)
    if workers > 1 and size > 0:
        return await _download_parallel(
            pyro_client, message, dest, size, workers, chunk_size, on_progress, cancel_event
        )
    return await _download_sequential(
        pyro_client, message, dest, size, chunk_size, on_progress, cancel_event
    )


async def _download_sequential(pyro_client, message, dest, size, chunk_size, on_progress, cancel_event):
    sha = hashlib.sha1()
    written = 0
    async with aiofiles.open(dest, "wb") as f:
        async for chunk in pyro_client.stream_media(message, chunk_size=chunk_size):
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


def _split_ranges(size: int, workers: int, chunk_size: int):
    """把 [0,size) 切成 workers 段，每段边界对齐 chunk_size（末段含余数）。返回 [(offset, length), ...]"""
    total_chunks = max(1, (size + chunk_size - 1) // chunk_size)
    n = max(1, min(workers, total_chunks))
    chunks_per = total_chunks // n
    ranges = []
    start = 0
    for i in range(n):
        if i == n - 1:
            c = total_chunks - (chunks_per * (n - 1))   # 末段吃余数
        else:
            c = chunks_per
        length = min(c * chunk_size, size - start)
        ranges.append((start, length))
        start += length
    return ranges


async def _download_parallel(pyro_client, message, dest, size, workers, chunk_size, on_progress, cancel_event):
    ranges = _split_ranges(size, workers, chunk_size)
    bytes_done = [0] * len(ranges)

    async def _worker(idx, offset, length):
        n_chunks = math.ceil(length / chunk_size) if length else 0
        async with aiofiles.open(dest, "r+b") as f:
            await f.seek(offset)
            async for chunk in pyro_client.stream_media(
                message, offset=offset, limit=n_chunks, chunk_size=chunk_size
            ):
                if cancel_event is not None and cancel_event.is_set():
                    raise TaskCancelled()
                if not chunk:
                    continue
                await f.write(chunk)
                bytes_done[idx] += len(chunk)
                if on_progress:
                    await on_progress(sum(bytes_done), size)

    tasks = [asyncio.create_task(_worker(i, o, l)) for i, (o, l) in enumerate(ranges)]
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
