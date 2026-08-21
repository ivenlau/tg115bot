"""115 离线下载编排：提交 -> 轮询 -> 完成通知 / 失败重试。

资源在 115 服务器下载，不占本地带宽、不走代理。入口：
  - 手动: /offline <链接> 或直接给 bot 发磁力/ed2k/直链（handlers 识别）
  - RSS / 电影订阅（Phase B/C）复用 ``submit()`` 提交。

状态机: pending -> running -> done / failed(retrying -> 重新提交, 最多 N 次)。
后台任务 ``offline_watcher`` 周期轮询 115 任务列表，比对本地 DB 同步状态并通知。
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional
from urllib.parse import urlparse

from core.app import state
from core.progress import human_bytes
from persistence.models import (
    OFFLINE_DONE, OFFLINE_FAILED, OFFLINE_PENDING, OFFLINE_RETRYING,
    OFFLINE_RUNNING, OfflineTaskRow,
)

log = logging.getLogger(__name__)

# 轮询间隔与重试策略
WATCH_INTERVAL = 60           # 轮询 115 任务列表间隔（秒）
MAX_RETRIES = 2               # 失败自动重试次数
RETRY_DELAY = 300             # 重试前等待（秒）

# 链接识别（magnet / ed2k / http(s) 直链含常见媒体/种子扩展）
_MAGNET_RE = re.compile(r"^magnet:\?xt=urn:btih:[0-9a-zA-Z]+")
_ED2K_RE = re.compile(r"^ed2k://\|file\|")
_TORRENT_RE = re.compile(
    r"^https?://\S+\.(torrent|mp4|mkv|avi|mov|wmv|flv|ts|iso|rar|zip|7z)(\?\S*)?$",
    re.IGNORECASE,
)


def is_media_url(text: str) -> bool:
    """严格判定：是否为可直接下载的媒体/种子直链（含扩展名）。
    RSS 自动离线用——泛 http 网页链接不应自动提交。"""
    return bool(_TORRENT_RE.match((text or "").strip()))


def classify_link(text: str) -> Optional[str]:
    """识别文本是否为可离线的链接；返回 'magnet'|'ed2k'|'url'|None。"""
    t = (text or "").strip()
    if not t or "\n" in t or len(t) > 2048:
        return None
    if _MAGNET_RE.match(t):
        return "magnet"
    if _ED2K_RE.match(t):
        return "ed2k"
    if _TORRENT_RE.match(t):
        return "url"
    parsed = urlparse(t)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return "url"          # 泛 http 链接也允许（由调用方决定是否提示）
    return None


async def submit(
    url: str,
    save_path: str,
    *,
    source: str = "manual",
    chat_id: int = 0,
) -> tuple[bool, str]:
    """提交一个离线任务。返回 (成功?, 说明)。URL 去重（pending/running 不重复提交）。"""
    if state.db is None or state.accounts is None:
        return False, "服务未就绪"
    if classify_link(url) is None:
        return False, "不支持的链接（magnet/ed2k/直链）"

    dup = await state.db.get_offline_by_url(url)
    if dup and dup.status in (OFFLINE_PENDING, OFFLINE_RUNNING, OFFLINE_RETRYING):
        return False, f"该链接已在离线队列（{dup.status}）"

    cloud = await state.accounts.get()
    try:
        await cloud.raw.offline_add(url, save_path)
    except Exception as e:  # noqa: BLE001
        log.warning("离线提交失败 %s: %r", url[:60], e)
        return False, f"提交失败: {e}"

    await state.db.insert_offline(OfflineTaskRow(
        url=url, save_path=save_path, status=OFFLINE_PENDING,
        source=source, chat_id=chat_id,
    ))
    return True, "已提交 115 离线下载"


async def _notify(chat_id: int, text: str) -> None:
    """TG 通知（尽力而为）。"""
    if not chat_id or state.pyro_bot is None:
        return
    try:
        await state.pyro_bot.send_message(chat_id, text)
    except Exception as e:  # noqa: BLE001
        log.debug("离线通知失败: %r", e)


async def _sync_once() -> None:
    """拉一次 115 任务列表，同步本地 DB 状态并发通知。"""
    if state.db is None or state.accounts is None:
        return
    tracking = await state.db.offline_by_status(
        OFFLINE_PENDING, OFFLINE_RUNNING, OFFLINE_RETRYING
    )
    if not tracking:
        return
    cloud = await state.accounts.get()
    try:
        remote = await cloud.raw.offline_list_all()
    except Exception as e:  # noqa: BLE001
        log.debug("离线列表拉取失败: %r", e)
        return

    by_url = {t.get("url"): t for t in remote if t.get("url")}
    for row in tracking:
        rt = by_url.get(row.url)
        if rt is None:
            continue   # 115 端可能已被清理；等重试逻辑处理或保持现状
        pct = int(rt.get("percentDone") or 0)
        if cloud.raw.offline_done(rt):
            await state.db.update_offline(
                row.id, status=OFFLINE_DONE, name=rt.get("name") or "",
                info_hash=rt.get("info_hash") or "", percent=100,
            )
            # 完成即清云端任务记录（保留文件）
            if rt.get("info_hash"):
                await cloud.raw.offline_del(rt["info_hash"], del_source_file=0)
            await _notify(row.chat_id,
                          f"✅ 离线完成\n📄 {rt.get('name') or row.url[:60]}\n📁 {row.save_path}")
            log.info("离线完成: %s", rt.get("name") or row.url[:60])
        elif cloud.raw.offline_failed(rt):
            if row.retries < MAX_RETRIES:
                await state.db.update_offline(
                    row.id, status=OFFLINE_RETRYING, retries=row.retries + 1,
                    name=rt.get("name") or "", info_hash=rt.get("info_hash") or "",
                    error=str(rt.get("status")),
                )
                await _notify(row.chat_id,
                              f"⚠️ 离线失败，将自动重试({row.retries + 1}/{MAX_RETRIES})\n"
                              f"📄 {rt.get('name') or row.url[:60]}")
            else:
                await state.db.update_offline(
                    row.id, status=OFFLINE_FAILED, name=rt.get("name") or "",
                    info_hash=rt.get("info_hash") or "", error="重试次数用尽",
                )
                await _notify(row.chat_id,
                              f"❌ 离线失败（已重试 {MAX_RETRIES} 次）\n📄 {rt.get('name') or row.url[:60]}")
        else:
            await state.db.update_offline(
                row.id, status=OFFLINE_RUNNING, percent=pct,
                name=rt.get("name") or "", info_hash=rt.get("info_hash") or "",
            )


async def _retry_pass() -> None:
    """重试 pass：RETRYING 且已过等待时间的重新提交。"""
    if state.db is None or state.accounts is None:
        return
    rows = await state.db.offline_by_status(OFFLINE_RETRYING)
    import time as _time
    now = _time.time()
    for row in rows:
        if now - row.updated_at < RETRY_DELAY:
            continue
        cloud = await state.accounts.get()
        if row.info_hash:
            await cloud.raw.offline_del(row.info_hash, del_source_file=1)
        try:
            await cloud.raw.offline_add(row.url, row.save_path)
            await state.db.update_offline(row.id, status=OFFLINE_PENDING, error="")
            log.info("离线重试已提交: %s", row.url[:60])
        except Exception as e:  # noqa: BLE001
            await state.db.update_offline(row.id, error=repr(e)[:200])


async def offline_watcher() -> None:
    """后台协程：轮询同步 + 失败重试。main.py 启动。"""
    while True:
        try:
            await asyncio.sleep(WATCH_INTERVAL)
            await _sync_once()
            await _retry_pass()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- watcher 不能挂
            log.debug("offline_watcher 异常", exc_info=True)
