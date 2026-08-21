"""RSS 通用订阅：定时拉取 -> 关键词过滤 -> 提取下载链接 -> 自动 115 离线。

替代参考项目的四个站点爬虫（sehua/t66y/javbus/av_daily，均依赖 Selenium）：
RSS 是站点官方或第三方生成的标准格式，无需浏览器、不怕站点改版。
PT 站 / 资源聚合站 / RSSHub 频道源都适用。

- 订阅存 DB（rss_feeds 表）：/addrss 添加，支持白名单关键词（空=全部）
- 已见条目 URL 存 rss_seen 表去重（防重复提交）
- 提取规则：条目 link 本身是 magnet/种子/媒体直链则直接离线；
  否则从标题/摘要里正则搜 magnet/ed2k
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import List, Optional

from core.app import state
from core.offline import classify_link, is_media_url

log = logging.getLogger(__name__)

FETCH_INTERVAL = 600          # 拉取间隔（秒）
_MAGNET_IN_TEXT = re.compile(r"magnet:\?xt=urn:btih:[0-9a-zA-Z]{20,}[^\s\"'<>]*")
_ED2K_IN_TEXT = re.compile(r"ed2k://\|file\|[^\"'<>]+")


def extract_links(title: str, link: str, summary: str = "") -> List[str]:
    """从条目提取可离线链接（纯函数，可测）。优先条目 link，其次正文里的 magnet/ed2k。"""
    out: List[str] = []
    # 条目 link 仅认 magnet/ed2k/媒体种子直链；普通网页地址不自动离线
    if classify_link(link) in ("magnet", "ed2k") or is_media_url(link):
        out.append(link.strip())
    text = f"{title or ''} {summary or ''}"
    out.extend(m.group(0) for m in _MAGNET_IN_TEXT.finditer(text))
    out.extend(m.group(0) for m in _ED2K_IN_TEXT.finditer(text))
    # 去重保序
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def entry_matches(title: str, whitelist: Optional[List[str]]) -> bool:
    """关键词白名单（空=全部命中；大小写不敏感子串，与频道监控同语义）。"""
    wl = [w for w in (whitelist or []) if w]
    if not wl:
        return True
    t = (title or "").lower()
    return any(w.lower() in t for w in wl)


async def _fetch_once(feed) -> None:
    """拉取一个订阅源并处理新条目。feed: RssFeedRow。"""
    import aiohttp

    cfg = state.config
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) tg115bot/1.0"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            # RSS 源多为境外/需科学，走 TG 同代理
            proxy = None
            if cfg and cfg.telegram.proxy:
                proxy = cfg.telegram.proxy
            async with s.get(feed.url, headers=headers, proxy=proxy) as r:
                text = await r.text()
    except Exception as e:  # noqa: BLE001
        log.warning("RSS 拉取失败 %s: %r", feed.name or feed.url[:50], e)
        await state.db.update_feed(feed.id, last_error=repr(e)[:200])
        return

    try:
        import feedparser
    except ImportError:
        log.error("未安装 feedparser（pip install feedparser），RSS 功能不可用")
        return
    parsed = feedparser.parse(text)
    if not parsed.entries:
        log.warning("RSS 无条目: %s", feed.name or feed.url[:50])
        return

    target = feed.save_path or (cfg.upload.target_dir if cfg else "/tg115bot")
    new_count = 0
    for entry in parsed.entries:
        title = getattr(entry, "title", "") or ""
        link = getattr(entry, "link", "") or ""
        if not entry_matches(title, feed.whitelist):
            continue
        for url in extract_links(title, link, getattr(entry, "summary", "") or ""):
            if await state.db.seen_link(url):
                continue
            ok, msg = False, ""
            from core.offline import submit as offline_submit
            ok, msg = await offline_submit(url, target, source="rss", chat_id=feed.chat_id)
            await state.db.mark_seen(url, title)
            if ok:
                new_count += 1
                log.info("RSS 自动离线: %s <- %s", title[:50], feed.name)
    await state.db.update_feed(feed.id, last_error="")
    if new_count:
        log.info("RSS [%s] 新提交 %d 个离线任务", feed.name, new_count)


async def rss_watcher() -> None:
    """后台协程：周期拉取全部启用的订阅源。main.py 启动。"""
    while True:
        try:
            await asyncio.sleep(FETCH_INTERVAL)
            if state.db is None:
                continue
            feeds = await state.db.list_feeds()
            for feed in feeds:
                if not feed.enabled:
                    continue
                # 按各自 last_fetch 间隔节流（统一间隔即可，这里顺带错峰）
                if time.time() - feed.last_fetch < FETCH_INTERVAL - 30:
                    continue
                await state.db.update_feed(feed.id, touch=True)
                await _fetch_once(feed)
                await asyncio.sleep(5)   # 源间间隔，礼貌抓取
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.debug("rss_watcher 异常", exc_info=True)
