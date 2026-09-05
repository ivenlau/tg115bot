"""Web 台·115 离线任务页：列表（30s 自动刷新）/ 添加 / 删除。

与 TUI OfflinePage 同源能力（scripts/manual.cmd_offline_*），数据直接走
state 里的主账号 115 客户端。列表每 30s HTMX 轮询局部刷新（与仪表盘同模式）。
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from web.helpers import get_cloud, page_ctx, templates

router = APIRouter()

# status 语义与 TUI OFFLINE_ICON 一致：-1 失败 / 1 进行中 / 2 完成
_OFFLINE_LABEL = {-1: ("❌", "err", "失败"), 1: ("📥", "", "进行中"), 2: ("✅", "ok", "完成")}


def offline_row(t: dict) -> dict:
    """离线任务 API 条目 -> 模板行（纯函数，便于单测）。"""
    try:
        st = int(t.get("status") or 0)
    except (TypeError, ValueError):
        st = 0
    icon, cls, label = _OFFLINE_LABEL.get(st, ("•", "muted", str(st) or "未知"))
    return {
        "icon": icon,
        "label": label,
        "cls": cls,
        "name": t.get("name") or (t.get("url") or "?")[:60],
        "pct": f"{t.get('percentDone', 0)}%" if st == 1 else "",
        "info_hash": str(t.get("info_hash") or ""),
    }


async def _list_ctx(request: Request, msg: str = "", err: str = "") -> dict:
    cloud = get_cloud(request)
    rows: List[dict] = []
    if cloud is None:
        err = err or "115 客户端未就绪（服务未运行或账号未授权）"
    else:
        try:
            rows = [offline_row(t) for t in await cloud.raw.offline_list_all()]
        except Exception as e:  # noqa: BLE001
            err = err or f"离线列表加载失败: {e}"
    return {"rows": rows, "msg": msg, "err": err,
            "save_dir": request.app.state.tg.config.upload.target_dir}


@router.get("/offline", response_class=HTMLResponse)
async def offline_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "offline.html", page_ctx(request, active="offline", **await _list_ctx(request)))


@router.get("/partials/offline", response_class=HTMLResponse)
async def offline_partial(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "_offline_list.html", await _list_ctx(request))


@router.post("/offline/add", response_class=HTMLResponse)
async def offline_add(request: Request, url: str = Form("")) -> HTMLResponse:
    url = url.strip()
    cloud = get_cloud(request)
    msg = err = ""
    if not url:
        err = "链接不能为空"
    elif cloud is None:
        err = "115 客户端未就绪"
    else:
        save = request.app.state.tg.config.upload.target_dir
        try:
            await cloud.raw.offline_add(url, save)
            msg = f"已提交离线任务 → {save}"
        except Exception as e:  # noqa: BLE001
            err = f"添加失败: {e}"
    return templates.TemplateResponse(
        request, "_offline_list.html", await _list_ctx(request, msg=msg, err=err))


@router.post("/offline/delete", response_class=HTMLResponse)
async def offline_delete(request: Request, info_hash: str = Form("")) -> HTMLResponse:
    cloud = get_cloud(request)
    msg = err = ""
    if cloud is None:
        err = "115 客户端未就绪"
    elif not info_hash:
        err = "缺少 info_hash"
    else:
        try:
            await cloud.raw.offline_del(info_hash, del_source_file=1)
            msg = "已删除离线任务（连同已下载的源文件）"
        except Exception as e:  # noqa: BLE001
            err = f"删除失败: {e}"
    return templates.TemplateResponse(
        request, "_offline_list.html", await _list_ctx(request, msg=msg, err=err))
