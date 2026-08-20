"""115 上传编排：fast_upload（秒传 + OSS 直传全链路）。

旧版双层兜底已简化：手写实现就是主路径本身，无 fs.upload 备胎。
失败由 pipeline 的账号冷却 + 队列重试机制兜底。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from cloud115.filesystem import mkdir_p
from cloud115.oss import fast_upload
from core.queue import TaskCancelled
from utils.rate import with_backoff

log = logging.getLogger(__name__)

ProgressCb = Callable[[int, int], Awaitable[None]]


@dataclass
class UploadResult:
    method: str = ""     # "秒传" | "oss"
    cid: str = ""


async def upload_to_dir(
    cloud,
    local_path: Path,
    size: int,
    sha1: str,
    target_dir: str,
    filename: str,
    *,
    oss_concurrency: int = 8,
    on_progress: Optional[ProgressCb] = None,
    cancel_event=None,
) -> UploadResult:
    """上传到 115 的 target_dir/filename，返回上传方式。"""
    cid = await mkdir_p(cloud, target_dir)

    result = await with_backoff(
        lambda: fast_upload(
            cloud, local_path, size, sha1, cid, filename,
            concurrency=oss_concurrency, on_progress=on_progress,
            cancel_event=cancel_event,
        ),
        base=2.0, max_retries=3, no_retry=(TaskCancelled,),
    )
    if result is None:               # 取消路径
        raise TaskCancelled()
    log.info("上传完成（%s）: %s -> cid=%s", result.method, filename, cid)
    return UploadResult(method=result.method, cid=cid)
