"""HTTP 直链中转上传：本地流式下载 -> 复用现有上传管线 -> 115。

与 115 离线互补：离线在 115 服务器下载（不占本地带宽），但部分站点对服务器
IP 风控/限速或无 BT；此路径本地 aiohttp 下载后走 秒传/OSS 上传管线。

入口：/dl <直链>（或未来由 RSS 兜底）。走 telegram.proxy（可选，默认走）。
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import aiofiles
import aiohttp

from core.app import state
from core.progress import human_bytes
from core.queue import Task, TaskCancelled

log = logging.getLogger(__name__)

_READ = 1024 * 1024
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def url_filename(url: str) -> str:
    """从 URL 提取文件名（去 query、解码；失败用 host+时间戳兜底）。"""
    path = urlparse(url).path or ""
    name = unquote(path.rstrip("/").rsplit("/", 1)[-1]) if path else ""
    if name and "." in name and len(name) <= 200:
        return re.sub(r'[\\/:*?"<>|]', "_", name)
    import time as _t
    host = urlparse(url).netloc.replace(".", "_") or "file"
    return f"{host}_{int(_t.time())}.bin"


async def direct_download(
    url: str,
    dest: Path,
    *,
    use_proxy: bool = True,
    on_progress=None,
    cancel_event: Optional[asyncio.Event] = None,
) -> tuple[int, str]:
    """流式下载直链到 dest，返回 (字节数, sha1)。"""
    import hashlib

    cfg = state.config
    proxy = None
    if use_proxy and cfg and cfg.telegram.proxy:
        proxy = cfg.telegram.proxy
    sha = hashlib.sha1()
    written = 0
    timeout = aiohttp.ClientTimeout(total=None, connect=15)
    async with aiohttp.ClientSession(timeout=timeout,
                                     headers={"User-Agent": UA}) as s:
        async with s.get(url, proxy=proxy) as r:
            if r.status >= 400:
                body = (await r.text())[:200]
                raise RuntimeError(f"HTTP {r.status}: {body}")
            total = int(r.headers.get("Content-Length") or 0)
            async with aiofiles.open(dest, "wb") as f:
                async for chunk in r.content.iter_chunked(_READ):
                    if cancel_event is not None and cancel_event.is_set():
                        raise TaskCancelled()
                    if not chunk:
                        continue
                    await f.write(chunk)
                    sha.update(chunk)
                    written += len(chunk)
                    if on_progress and (written % (4 * _READ) < _READ):
                        await on_progress(written, total or written)
    if on_progress:
        await on_progress(written, written)
    return written, sha.hexdigest()


async def run_direct_task(task: Task, tmp: Path) -> None:
    """直接下载型任务的 pipeline 替身：下载直链 -> 上传。"""
    from core.progress import ProgressReporter
    from core.uploader import upload_to_dir

    cfg = state.config
    ws = state.workspace
    ws.delete_after_upload = cfg.upload.delete_after_upload
    reporter = ProgressReporter(
        state.pyro_bot, task.tracking_chat_id, task.tracking_message_id,
        task_id=task.task_id, filename=task.filename, source="direct",
    )
    succeeded = False
    try:
        await reporter.set_stage("🌐 直链下载中")
        size, sha1 = await direct_download(
            task.message, tmp, on_progress=reporter.on_progress,
            cancel_event=task.cancel_event,
        )
        await reporter.set_stage("⬆️ 上传到 115")
        cloud = await state.accounts.get()
        result = await upload_to_dir(
            cloud, tmp, size, sha1, task.target_dir, task.filename,
            oss_concurrency=cfg.upload.oss_concurrency,
            on_progress=reporter.on_progress, cancel_event=task.cancel_event,
        )
        succeeded = True
        await reporter.final_text(
            f"✅ 完成\n📄 {task.filename}\n📦 {human_bytes(size)}\n"
            f"📁 {task.target_dir}\n⚡ {result.method}"
            + (f"\n💾 本地副本: {ws.root / 'copies'}" if ws.keep_local else "")
        )
    except TaskCancelled:
        await reporter.final_text(f"🚫 已取消\n📄 {task.filename}")
    except Exception as e:  # noqa: BLE001
        log.exception("直链任务失败: %s", task.filename)
        await reporter.final_text(f"❌ 失败: {task.filename}\n原因: {e}")
    finally:
        if succeeded and ws.keep_local:
            if ws.keep_copy(tmp, task.filename) is not None:
                return
        if ws.delete_after_upload:
            ws.cleanup(tmp)
