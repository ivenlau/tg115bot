"""Web 台路由：仪表盘 / 任务历史 / 账号 / 频道规则 / 日志。

页面用 Jinja2 + HTMX：仪表盘每 3s 拉取局部刷新；频道规则增删、任务记录删除走
HTMX 局部替换。文件 / 离线任务 / 配置在 web/files.py、web/offline.py、web/console.py。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from web.helpers import cloud_usage, get_cloud, page_ctx, templates

router = APIRouter()

_TERMINAL_STATUS = ("done", "failed", "cancelled")


# ── 仪表盘 ────────────────────────────────────────────────────────────────

async def _dash_data(request: Request) -> dict:
    tg = request.app.state.tg
    db = request.app.state.db
    accounts = request.app.state.accounts
    active = list(tg.task_progress.values())
    stats = await db.task_stats() if db else {}
    acct_status = accounts.status_list() if accounts else []
    now = time.time()
    for a in active:
        a["elapsed"] = int(now - (a.get("started_at") or now))
        a["pct"] = (a.get("current", 0) * 100 / a["total"]) if a.get("total") else 0

    # 115 空间 / API 余量（60s 缓存；未就绪显示占位）
    space, quota = await cloud_usage(get_cloud(request))
    cloud_api = {}
    cloud = get_cloud(request)
    if cloud is not None:
        cloud_api = {"used": cloud.raw.request_count, "limit": cloud.raw.daily_limit}

    return {
        "active_tasks": active, "stats": stats, "accounts": acct_status,
        "qsize": tg.queue.qsize() if tg.queue else 0,
        "free_gb": (tg.workspace.free_bytes() / 1024**3) if tg.workspace else 0,
        "min_free_gb": tg.config.storage.min_free_gb if tg.config else 0,
        "space": space, "quota": quota, "api": cloud_api,
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    data = await _dash_data(request)
    return templates.TemplateResponse(request, "dashboard.html",
                                      page_ctx(request, active="dashboard", **data))


@router.get("/partials/dashboard", response_class=HTMLResponse)
async def dashboard_partial(request: Request) -> HTMLResponse:
    """仪表盘局部刷新片段（HTMX 每 3s 拉取）。"""
    return templates.TemplateResponse(request, "_dashboard.html", await _dash_data(request))


# ── 任务历史 ──────────────────────────────────────────────────────────────

@router.get("/tasks", response_class=HTMLResponse)
async def tasks(request: Request) -> HTMLResponse:
    db = request.app.state.db
    rows = await db.recent_tasks(100) if db else []
    return templates.TemplateResponse(request, "tasks.html",
                                      page_ctx(request, active="tasks", tasks=rows))


@router.get("/partials/tasks", response_class=HTMLResponse)
async def tasks_partial(request: Request) -> HTMLResponse:
    db = request.app.state.db
    rows = await db.recent_tasks(100) if db else []
    return templates.TemplateResponse(request, "_tasks_list.html",
                                      {"tasks": rows, "msg": "", "err": ""})


@router.post("/tasks/{task_id}/delete", response_class=HTMLResponse)
async def task_delete(request: Request, task_id: str) -> HTMLResponse:
    """删除任务记录（仅终态；不动 115 云端文件）。"""
    db = request.app.state.db
    msg = err = ""
    if db is None:
        err = "数据库不可用"
    else:
        rows = await db.recent_tasks(100)
        row = next((r for r in rows if r.task_id == task_id), None)
        if row is None:
            err = "记录不存在或已被删除"
        elif row.status not in _TERMINAL_STATUS:
            err = "仅完成/失败/已取消的任务可删除记录"
        else:
            await db.delete_task(task_id)
            msg = "已删除任务记录"
    rows = await db.recent_tasks(100) if db else []
    return templates.TemplateResponse(request, "_tasks_list.html",
                                      {"tasks": rows, "msg": msg, "err": err})


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
    return templates.TemplateResponse(request, "accounts.html",
                                      page_ctx(request, active="accounts", accounts=runtime))


# ── 频道规则 ──────────────────────────────────────────────────────────────

@router.get("/channels", response_class=HTMLResponse)
async def channels(request: Request) -> HTMLResponse:
    db = request.app.state.db
    rules = await db.list_rules() if db else []
    return templates.TemplateResponse(request, "channels.html",
                                      page_ctx(request, active="channels", rules=rules))


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


# ── 日志（业务日志 DB 查询 + 运行日志 stdout tail） ───────────────────────

_STDOUT_TAIL_BYTES = 256 * 1024
_STDOUT_TAIL_LINES = 200


def _stdout_tail(lines: int = _STDOUT_TAIL_LINES) -> str:
    """stdout.log 尾部（读最后 256KB 取尾 N 行；轮转/缺失静默降级）。"""
    from tb import service

    try:
        with open(service.STDOUT_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _STDOUT_TAIL_BYTES))
            text = f.read().decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])
    except OSError:
        return ""


@router.get("/logs", response_class=HTMLResponse)
async def logs(request: Request, level: str = "", view: str = "") -> HTMLResponse:
    db = request.app.state.db
    rows = [] if view == "stdout" else \
        (await db.recent_logs(300, level=level or None) if db else [])
    log_path = ""
    if view == "stdout":
        from tb import service
        log_path = str(service.STDOUT_LOG)
    return templates.TemplateResponse(request, "logs.html", page_ctx(
        request, active="logs", logs=rows, level=level,
        view="stdout" if view == "stdout" else "db",
        tail=_stdout_tail() if view == "stdout" else "",
        log_path=log_path,
    ))


@router.get("/partials/stdout", response_class=HTMLResponse)
async def stdout_partial(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "_stdout.html", {"tail": _stdout_tail()})
