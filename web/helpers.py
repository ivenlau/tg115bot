"""Web 台共享小件：模板引擎 + 公共上下文 + 115 客户端获取 + 路径工具。

Web 路由活在 bot 服务进程的主事件循环上（uvicorn 为 asyncio 任务），
AccountManager 的客户端会话就建在该循环——路由里直接 await 即可，
无 TUI 那类 aiohttp 会话跨循环问题（tb/tui.py 的 _CloudLoop 为此而生，Web 不需要）。
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core.progress import human_bytes

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["human_bytes"] = human_bytes
templates.env.filters["ts"] = lambda v: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v)) if v else "-"
templates.env.filters["status_badge"] = lambda s: {
    "done": "✅", "failed": "❌", "cancelled": "🚫",
    "downloading": "📥", "uploading": "⬆️", "queued": "⏳",
}.get(s, "•")


def page_ctx(request: Request, **kw) -> dict:
    """整页公共模板上下文（侧栏状态卡用的账号名/队列深度；partial 不经过它）。"""
    base = {"request": request, "active": "", "qsize": 0, "account_name": "-"}
    tg = request.app.state.tg
    base["qsize"] = tg.queue.qsize() if tg.queue else 0
    try:
        accounts = getattr(tg.config, "accounts", None) if tg.config else None
        if accounts:
            base["account_name"] = accounts[0].name
    except Exception:  # noqa: BLE001 -- 侧栏小卡信息缺失不值得炸页面
        pass
    base.update(kw)
    return base


def get_cloud(request):
    """主账号 115 客户端（bot 进程内现成；未就绪返回 None，路由自行渲染错误）。"""
    st = request.app.state
    cloud = getattr(st, "cloud", None)
    if cloud is None and getattr(st, "accounts", None) is not None:
        try:
            cloud = st.accounts.primary
        except Exception:  # noqa: BLE001
            cloud = None
    return cloud


# user_space/offline_quota 是网络调用；仪表盘 3s 轮询不能每次真打 115，缓存 60s
_usage_cache: dict = {"t": 0.0, "space": {}, "quota": {}}
_USAGE_TTL = 60.0


async def cloud_usage(cloud) -> tuple[dict, dict]:
    """(空间 used/total, 离线配额 used/count)；失败退旧值并 15s 后重试。"""
    if cloud is None:
        return {}, {}
    now = time.monotonic()
    if now - _usage_cache["t"] <= _USAGE_TTL:
        return _usage_cache["space"], _usage_cache["quota"]
    try:
        _usage_cache["space"] = await cloud.raw.user_space()
        _usage_cache["quota"] = await cloud.raw.offline_quota()
        _usage_cache["t"] = now
    except Exception:  # noqa: BLE001 -- 115 不可达时保留旧值，稍后再试
        _usage_cache["t"] = now - _USAGE_TTL + 15.0
    return _usage_cache["space"], _usage_cache["quota"]


def path_join(path: str, name: str) -> str:
    """115 目录路径 + 条目名（115 全程用 /，Windows 上不可走 Path）。"""
    return (path or "/").rstrip("/") + "/" + name


def path_parent(path: str) -> str:
    """父目录路径（纯字符串求法，与 TUI FilesPage 同款）。"""
    p = (path or "/").rstrip("/")
    if not p:
        return "/"
    return p[: p.rfind("/")] or "/"


def path_crumbs(path: str) -> list[dict]:
    """路径 -> 面包屑段 [{name, path}]，首段固定根目录。"""
    crumbs = [{"name": "/", "path": "/"}]
    cur = ""
    for seg in [s for s in (path or "").split("/") if s]:
        cur += "/" + seg
        crumbs.append({"name": seg, "path": cur})
    return crumbs
