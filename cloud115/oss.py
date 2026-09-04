"""115 快速上传（开放平台全链路，零 p115 依赖）：秒传 -> 二次区间校验 -> OSS 直传。

流程（对照 telegram-115bot upload_file + p115oss 协议，字段级一致）：
  1) upload/init {file_name, file_size, target=U_1_{cid}, fileid=sha1大写}
     data.status == 2            -> 秒传命中，直接返回（服务端已登记文件）
     data.sign_key + sign_check  -> 二次校验：对 [start,end] 区间算 SHA1 大写，
                                    带 sign_key/sign_val 重调 init（仅一次）
  2) 未命中 -> data 含 bucket/object/callback/pick_code
     GET upload/get_token -> STS {AccessKeyId, AccessKeySecret, SecurityToken, endpoint}
     OSS 直传（cloud115/oss_upload.upload_to_oss：单 PUT 或 multipart 并发 + callback）
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiofiles

from cloud115.oss_upload import upload_to_oss
from core.queue import TaskCancelled  # noqa: F401

log = logging.getLogger(__name__)

ProgressCb = Callable[[int, int], Awaitable[None]]
_READ = 1024 * 1024


@dataclass
class FastResult:
    method: str       # "秒传" | "oss"
    detail: Any = None


async def _sha1_range(path: Path, start: int, end: int) -> str:
    """对 [start, end]（含端点）区间算 SHA1（二次校验用，对照 file_sha1_by_range）。"""
    sha = hashlib.sha1()
    async with aiofiles.open(path, "rb") as f:
        await f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            data = await f.read(min(_READ, remaining))
            if not data:
                break
            sha.update(data)
            remaining -= len(data)
    return sha.hexdigest()


async def fast_upload(
    cloud,
    local_path: Path,
    size: int,
    sha1: str,
    cid: str,
    filename: str,
    *,
    concurrency: int = 8,
    on_progress: Optional[ProgressCb] = None,
    cancel_event=None,
) -> Optional[FastResult]:
    """上传到 115 的 cid/filename。返回 FastResult；取消返回 None。

    size/sha1 由调用方算好传入（download 阶段已得）；为 0/空时此处自动补算。
    """
    local_path = Path(local_path)
    if size <= 0:
        size = os.path.getsize(local_path)
    if not sha1:
        sha1 = await _sha1_range(local_path, 0, size - 1)
    cid_int = int(cid) if str(cid).isdigit() else 0
    api = cloud.raw

    # ── 1) 秒传探测（含二次区间校验，最多两轮） ──────────────────────────
    data = await api.upload_init(filename, size, sha1.upper(), cid_int)
    if data.get("sign_key") and data.get("sign_check"):
        start_s, _, end_s = data["sign_check"].partition("-")
        sign_val = await _sha1_range(local_path, int(start_s), int(end_s))
        log.info("秒传二次校验: 区间 %s -> sha1 %s…", data["sign_check"], sign_val[:12])
        data = await api.upload_init(filename, size, sha1.upper(), cid_int,
                                     sign_key=data["sign_key"], sign_val=sign_val.upper())

    if str(data.get("status")) == "2":
        log.info("🎯 秒传命中: %s", filename)
        if on_progress:
            try:
                await on_progress(size, size)
            except Exception:  # noqa: BLE001
                pass
        return FastResult("秒传", data)

    if cancel_event is not None and cancel_event.is_set():
        return None

    # ── 2) OSS 直传 ────────────────────────────────────────────────────────
    bucket = data.get("bucket") or ""
    obj = data.get("object") or ""
    if not bucket or not obj:
        raise RuntimeError(f"upload/init 未返回 OSS 参数: {str(data)[:200]}")

    token = await api.get_upload_token()

    async def _refresher():
        """闭包:每次返回新 STS dict。仅在 STS 过期时被调用以续传。
        保持 oss_upload 不知道 Open115Client 的存在,反向依赖隔离。"""
        return await api.get_upload_token()

    if on_progress:
        try:
            await on_progress(0, size)
        except Exception:  # noqa: BLE001
            pass
    await upload_to_oss(
        cloud.raw.session, local_path, size,
        endpoint=token.get("endpoint", ""),
        bucket=bucket, obj=obj, token=token,
        callback=data.get("callback"),
        concurrency=concurrency,
        on_progress=on_progress,
        cancel_event=cancel_event,
        token_refresher=_refresher,
    )
    log.info("OSS 直传完成: %s -> cid=%s", filename, cid)
    return FastResult("oss", data)
