"""电影订阅追更：TMDB 匹配 -> 轮询 nullbr API -> 资源发布自动 115 离线。

对照参考项目 subscribe_movie.py，简化取舍：
  - TMDB 匹配沿用其"免 API key 网页搜索"法（BeautifulSoup 解析 themoviedb.org）
  - 资源评分简化：分辨率优先级 + 中字加分，不强制杜比（参考项目的强条件常导致选不到资源）
  - 每 4 小时轮询一次订阅表；找到 ed2k 优先（115 离线对 ed2k 支持最好），magnet 兜底

需要 nullbr API 授权（config.movie_sub.app_id/api_key）：
  https://nullbr.online/api 申请
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

import aiohttp

from core.app import state

log = logging.getLogger(__name__)

NULLBR_BASE = "https://api.nullbr.eu.org"
TMDB_SEARCH = "https://www.themoviedb.org/search/movie"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
CHECK_INTERVAL = 4 * 3600        # 轮询间隔：4 小时
RESOLUTION_PRIORITY = ("2160p", "1080p", "720p")   # 分辨率优先级（高分优先）


@dataclass
class Candidate:
    url: str
    score: int
    name: str = ""
    size: str = ""
    zh_sub: bool = False


def score_resource(item: dict, key: str) -> Candidate:
    """资源评分（纯函数）：分辨率优先级 + 中字加分。"""
    name = str(item.get("name") or "")
    resolution = str(item.get("resolution") or "")
    zh_sub = item.get("zh_sub") == 1
    score = 10 if zh_sub else 0
    for i, res in enumerate(RESOLUTION_PRIORITY):
        if res in resolution or res in name:
            score += len(RESOLUTION_PRIORITY) - i
            break
    return Candidate(url=item.get(key) or "", score=score, name=name,
                     size=str(item.get("size") or ""), zh_sub=zh_sub)


def pick_best(resources: List[dict], key: str,
              require_zh_sub: bool = False) -> Optional[Candidate]:
    """从资源列表挑最优（纯函数）。require_zh_sub=True 时只要带中字的。"""
    cands = sorted((score_resource(i, key) for i in resources if i.get(key)),
                   key=lambda c: c.score, reverse=True)
    if require_zh_sub:
        cands = [c for c in cands if c.zh_sub]
    return cands[0] if cands else None


async def tmdb_search_id(movie_name: str) -> Optional[str]:
    """TMDB 网页搜索电影 ID（免 API key，对照参考实现；走 TG 同代理）。"""
    cfg = state.config
    proxy = cfg.telegram.proxy if cfg and cfg.telegram.proxy else None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.get(TMDB_SEARCH, params={"query": movie_name},
                             headers={"User-Agent": UA,
                                      "accept-language": "zh-CN"},
                             proxy=proxy) as r:
                text = await r.text()
    except Exception as e:  # noqa: BLE001
        log.warning("TMDB 搜索失败: %r", e)
        return None
    if "找不到和您的查询相符的电影" in text:
        return None
    m = re.search(rf'class="result[^"]*"\s+href="/movie/(\d+)[^"]*"[^>]*>\s*'
                  r'<h2[^>]*>\s*([^<（(]+)', text)
    if not m:
        m = re.search(r'href="/movie/(\d+)-', text)
        return m.group(1) if m else None
    # 标题需与搜索词一致（精确匹配，对照参考实现的语义）
    if movie_name.strip() not in m.group(2):
        return None
    return m.group(1)


async def nullbr_get(path: str) -> Dict:
    """调 nullbr API（需授权头）。path 如 /movie/{tmdb_id}/ed2k。"""
    cfg = state.config
    ms = cfg.movie_sub
    headers = {"User-Agent": UA, "X-APP-ID": ms.app_id, "X-API-KEY": ms.api_key}
    proxy = cfg.telegram.proxy if cfg.telegram.proxy else None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.get(f"{NULLBR_BASE}{path}", headers=headers, proxy=proxy) as r:
            if r.status != 200:
                raise RuntimeError(f"nullbr HTTP {r.status}: {await r.text()[:200]}")
            data = await r.json(content_type=None)
    if not isinstance(data, dict) or data.get("code") not in (None, 0, 200):
        raise RuntimeError(f"nullbr 返回异常: {str(data)[:150]}")
    return data


async def check_movie(sub_row) -> Optional[str]:
    """检查一部订阅电影是否已有资源；有则返回下载链接。"""
    tmdb_id = sub_row.tmdb_id
    require_zh = bool(getattr(sub_row, "zh_sub", False))
    for key in ("ed2k", "magnet"):
        try:
            data = await nullbr_get(f"/movie/{tmdb_id}/{key}")
        except Exception as e:  # noqa: BLE001
            log.warning("nullbr %s 查询失败(%s): %r", key, tmdb_id, e)
            continue
        resources = data.get(key) or data.get("data", {}).get(key) or []
        if not isinstance(resources, list) or not resources:
            continue
        best = pick_best(resources, key, require_zh_sub=require_zh)
        if best and best.url:
            return best.url
    return None


async def movie_watcher() -> None:
    """后台协程：周期轮询订阅表，资源发布即离线并通知。"""
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL)
            if state.db is None or state.accounts is None:
                continue
            cfg = state.config
            if not (cfg.movie_sub.app_id and cfg.movie_sub.api_key):
                continue   # 未配置 nullbr 授权，跳过
            subs = await state.db.list_movie_subs()
            for sub in subs:
                if sub.downloaded:
                    continue
                url = await check_movie(sub)
                if not url:
                    continue
                ok, msg = False, ""
                from core.offline import submit as offline_submit
                ok, msg = await offline_submit(
                    url, sub.save_path, source="movie", chat_id=sub.chat_id)
                if ok:
                    await state.db.update_movie_sub(sub.id, downloaded=True,
                                                    download_url=url)
                    log.info("电影订阅命中: %s", sub.movie_name)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.debug("movie_watcher 异常", exc_info=True)
