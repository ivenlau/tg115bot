"""Web 台路由：仪表盘 / 任务历史 / 账号 / 频道规则 / 日志。

页面用 Jinja2 + HTMX：仪表盘每 3s 拉取局部刷新；频道规则增删走 HTMX 局部替换。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import List

from fastapi import APIRouter, Form, Request
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

router = APIRouter()


def _ctx(request: Request, **kw) -> dict:
    """公共模板上下文（TemplateResponse 新签名下 request 由第一参数传入，
    但仍放进 context，模板里可直接用）。"""
    base = {
        "request": request,
        "active": "",
        "qsize": 0,
    }
    tg = request.app.state.tg
    base["qsize"] = tg.queue.qsize() if tg.queue else 0
    base.update(kw)
    return base


# ── 仪表盘 ────────────────────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    tg = request.app.state.tg
    db = request.app.state.db
    accounts = request.app.state.accounts
    active: List[dict] = list(tg.task_progress.values())
    stats = await db.task_stats() if db else {}
    acct_status = accounts.status_list() if accounts else []
    now = time.time()
    for a in active:
        a["elapsed"] = int(now - (a.get("started_at") or now))
        a["pct"] = (a.get("current", 0) * 100 / a["total"]) if a.get("total") else 0
    return templates.TemplateResponse(request, "dashboard.html", _ctx(
        request, active="dashboard", active_tasks=active, stats=stats,
        accounts=acct_status, free_gb=(tg.workspace.free_bytes() / 1024**3) if tg.workspace else 0,
        min_free_gb=tg.config.storage.min_free_gb if tg.config else 0,
    ))


@router.get("/partials/dashboard", response_class=HTMLResponse)
async def dashboard_partial(request: Request) -> HTMLResponse:
    """仪表盘局部刷新片段（HTMX 每 3s 拉取）。"""
    tg = request.app.state.tg
    db = request.app.state.db
    accounts = request.app.state.accounts
    active: List[dict] = list(tg.task_progress.values())
    stats = await db.task_stats() if db else {}
    acct_status = accounts.status_list() if accounts else []
    now = time.time()
    for a in active:
        a["elapsed"] = int(now - (a.get("started_at") or now))
        a["pct"] = (a.get("current", 0) * 100 / a["total"]) if a.get("total") else 0
    return templates.TemplateResponse(request, "_dashboard.html", {
        "active_tasks": active, "stats": stats,
        "accounts": acct_status, "qsize": tg.queue.qsize() if tg.queue else 0,
        "free_gb": (tg.workspace.free_bytes() / 1024**3) if tg.workspace else 0,
        "min_free_gb": tg.config.storage.min_free_gb if tg.config else 0,
    })


# ── 任务历史 ──────────────────────────────────────────────────────────────
@router.get("/tasks", response_class=HTMLResponse)
async def tasks(request: Request) -> HTMLResponse:
    db = request.app.state.db
    rows = await db.recent_tasks(100) if db else []
    return templates.TemplateResponse(request, "tasks.html", _ctx(request, active="tasks", tasks=rows))


# ── 账号 ──────────────────────────────────────────────────────────────────
@router.get("/accounts", response_class=HTMLResponse)
async def accounts(request: Request) -> HTMLResponse:
    accounts_mgr = request.app.state.accounts
    db = request.app.state.db
    runtime = accounts_mgr.status_list() if accounts_mgr else []
    db_rows = {r.name: r for r in (await db.list_accounts())} if db else {}
    for a in runtime:
        row = db_rows.get(a["name"])
        a["last_error"] = row.last_error if row else ""
        a["last_used_at"] = row.last_used_at if row else 0
    return templates.TemplateResponse(request, "accounts.html", _ctx(request, active="accounts", accounts=runtime))


# ── 频道规则 ──────────────────────────────────────────────────────────────
@router.get("/channels", response_class=HTMLResponse)
async def channels(request: Request) -> HTMLResponse:
    db = request.app.state.db
    rules = await db.list_rules() if db else []
    return templates.TemplateResponse(request, "channels.html", _ctx(request, active="channels", rules=rules))


@router.post("/channels", response_class=HTMLResponse)
async def channels_add(
    request: Request,
    channel_id: int = Form(...),
    title: str = Form(""),
    target_dir: str = Form(""),
    whitelist: str = Form(""),
    blacklist: str = Form(""),
    enabled: bool = Form(True),
) -> HTMLResponse:
    db = request.app.state.db
    tg = request.app.state.tg
    wl = [w.strip() for w in whitelist.replace("，", ",").split(",") if w.strip()]
    bl = [b.strip() for b in blacklist.replace("，", ",").split(",") if b.strip()]
    if db:
        await db.upsert_rule(channel_id, title, wl, bl, target_dir, enabled)
        if tg.monitor is not None:
            await tg.monitor.reload()
    rules = await db.list_rules() if db else []
    return templates.TemplateResponse(request, "_channels_list.html", {"rules": rules})


@router.post("/channels/{rule_id}/delete", response_class=HTMLResponse)
async def channels_delete(request: Request, rule_id: int) -> HTMLResponse:
    db = request.app.state.db
    tg = request.app.state.tg
    if db:
        await db.delete_rule(rule_id)
        if tg.monitor is not None:
            await tg.monitor.reload()
    rules = await db.list_rules() if db else []
    return templates.TemplateResponse(request, "_channels_list.html", {"rules": rules})


# ── 日志 ──────────────────────────────────────────────────────────────────
@router.get("/logs", response_class=HTMLResponse)
async def logs(request: Request, level: str = "") -> HTMLResponse:
    db = request.app.state.db
    rows = await db.recent_logs(300, level=level or None) if db else []
    return templates.TemplateResponse(request, "logs.html", _ctx(
        request, active="logs", logs=rows, level=level,
    ))
