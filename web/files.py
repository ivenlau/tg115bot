"""Web 台·115 文件管理页：浏览 / 全盘搜索 / 删除重命名移动新建 / 服务器下载 / 路径上传。

与 TUI FilesPage 同源能力，复用 cloud115.filesystem / cloud115.download 的现成链路：
- 浏览列表走 list_files（首屏 100 条，与 TUI 一致，不做翻页）
- 搜索结果带 sha1，按 pick_code 直下（download_by_pick_code，不经路径解析）
- 下载/上传是长耗时操作：进程内任务注册表 + asyncio 任务跑进度，
  页面底部「传输任务」区每 2s HTMX 轮询（语义 = TUI 的 #dl-status 行）

下载/上传的「本地」指 **服务器磁盘**（Web 活在服务进程里）；浏览器中转大文件不在 v1 范围。
"""
from __future__ import annotations

import asyncio
import time
from typing import List

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from web.helpers import (get_cloud, page_ctx, path_crumbs, path_join,
                         path_parent, templates)

router = APIRouter()

_LIST_LIMIT = 100          # 浏览首屏条数（与 TUI 一致）
_SEARCH_LIMIT = 50

# ── 传输任务注册表（进程内；下载/上传共享，asyncio 单线程无锁） ──────────

_jobs: dict = {}
_job_seq = 0
_MAX_JOBS = 30


def _new_job(kind: str, name: str) -> dict:
    global _job_seq
    _job_seq += 1
    job = {"id": f"{kind}-{_job_seq}", "kind": kind, "name": name,
           "status": "running", "written": 0, "total": 0,
           "detail": "", "error": "", "ts": time.time()}
    _jobs[job["id"]] = job
    _prune_jobs()
    return job


def _prune_jobs() -> None:
    """超出上限时优先清最老的已结束任务（running 不清，进度不丢）。"""
    overflow = len(_jobs) - _MAX_JOBS
    if overflow <= 0:
        return
    finished = sorted((j for j in _jobs.values() if j["status"] != "running"),
                      key=lambda j: j["ts"])
    for j in finished[:overflow]:
        _jobs.pop(j["id"], None)


def job_snapshot() -> List[dict]:
    return sorted(_jobs.values(), key=lambda j: j["ts"], reverse=True)


async def _run_download(job: dict, cloud, pc: str, dest_dir: str) -> None:
    from cloud115.download import download_by_pick_code

    def on_prog(written: int, total: int) -> None:
        job["written"], job["total"] = written, total

    try:
        r = await download_by_pick_code(cloud, pc, dest_dir.strip() or "~/Downloads",
                                        on_progress=on_prog)
        job.update(status="done", written=r["size"], total=r["size"],
                   detail=str(r["dest"]))
    except Exception as e:  # noqa: BLE001 -- 含 sha1 不符（.part 现场保留）
        job.update(status="failed", error=str(e)[:300])


async def _run_upload(job: dict, cloud, src: str, remote_base: str) -> None:
    from core.uploader import upload_to_dir
    from scripts import manual

    try:
        files, bases, missing = manual.expand_sources([src])
        for m in missing:
            job["error"] = (job["error"] + "；" if job["error"] else "") + str(m)[:150]
        if not files:
            job["status"] = "failed"
            if not job["error"]:
                job["error"] = "路径不匹配任何文件"
            return
        n = len(files)
        for i, f in enumerate(files, 1):
            job["name"], job["detail"] = f.name, f"{i}/{n}"
            rel = f.relative_to(bases[f]).parent
            remote = (remote_base.rstrip("/")
                      + ("/" + rel.as_posix() if str(rel) != "." else ""))
            size, sha1 = await manual.sha1_of(f)

            async def on_prog(w: int, t: int) -> None:
                job["written"], job["total"] = w, t

            result = await upload_to_dir(cloud, f, size, sha1, remote, f.name)
            job["detail"] = f"{i}/{n}（{result.method}）"
        if job["error"]:
            job["status"] = "failed"
        else:
            job.update(status="done", detail=f"{n} 个文件已上传")
    except Exception as e:  # noqa: BLE001
        job.update(status="failed", error=str(e)[:300])


# ── 列表数据（浏览 / 搜索两模式共用一个片段） ────────────────────────────

async def _files_data(request: Request, path: str = "/tg115bot", kw: str = "",
                      msg: str = "", err: str = "") -> dict:
    """片段上下文：搜索模式（kw 非空）或浏览模式。"""
    from core.progress import human_bytes

    cloud = get_cloud(request)
    entries: List[dict] = []
    count = 0

    if kw:
        if cloud is None:
            err = err or "115 客户端未就绪"
        else:
            try:
                data = await cloud.raw.search_files(kw, limit=_SEARCH_LIMIT)
                items = data.get("list") or []
                count = int(data.get("count") or len(items))
                for it in items:
                    is_dir = str(it.get("fc") or "1") == "0"
                    entries.append({
                        "icon": "📂" if is_dir else "📄",
                        "name": str(it.get("fn") or "?"),
                        "size": "" if is_dir else human_bytes(int(it.get("fs") or 0)),
                        "pc": "" if is_dir else str(it.get("pc") or ""),
                        "sha1": "" if is_dir else str(it.get("sha1") or "")[:8],
                    })
            except Exception as e:  # noqa: BLE001
                err = err or f"搜索失败: {e}"
        return {"request": request, "search": True, "kw": kw, "entries": entries,
                "count": count, "msg": msg, "err": err, "root": path or "/tg115bot"}

    if cloud is None:
        err = err or "115 客户端未就绪（服务未运行或账号未授权）"
    else:
        try:
            from cloud115.filesystem import resolve_cid
            from scripts.manual import (entry_is_dir, entry_name, entry_size,
                                        sort_entries)
            cid = await resolve_cid(cloud, path)
            data = await cloud.raw.list_files(int(cid), limit=_LIST_LIMIT)
            items = sort_entries(data.get("list") or [])
            count = int(data.get("count") or len(items))
            for it in items:
                is_dir = entry_is_dir(it)
                entries.append({
                    "icon": "📂" if is_dir else "📄",
                    "name": entry_name(it),
                    "size": "" if is_dir else human_bytes(entry_size(it)),
                    "full": path_join(path, entry_name(it)) if is_dir else "",
                })
        except Exception as e:  # noqa: BLE001
            err = err or f"加载失败: {e}"
    return {"request": request, "search": False, "path": path,
            "parent": path_parent(path), "crumbs": path_crumbs(path),
            "entries": entries, "count": count, "msg": msg, "err": err}


async def _render_files(request: Request, path: str = "/tg115bot", kw: str = "",
                        msg: str = "", err: str = "") -> HTMLResponse:
    data = await _files_data(request, path, kw, msg, err)
    return templates.TemplateResponse(request, "_files_list.html", data)


@router.get("/files", response_class=HTMLResponse)
async def files_page(request: Request, path: str = "/tg115bot", kw: str = "") -> HTMLResponse:
    data = await _files_data(request, path, kw)
    data.pop("request", None)   # page_ctx 已注入 request，避免键冲突
    # include 默认继承上下文：片段变量直接摊进整页上下文（jobs 供首帧传输任务区）
    return templates.TemplateResponse(
        request, "files.html", page_ctx(request, active="files",
                                        jobs=job_snapshot(), **data))


@router.get("/partials/files", response_class=HTMLResponse)
async def files_partial(request: Request, path: str = "/tg115bot", kw: str = "") -> HTMLResponse:
    return await _render_files(request, path, kw)


@router.get("/partials/jobs", response_class=HTMLResponse)
async def jobs_partial(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "_jobs.html", {"jobs": job_snapshot()})


# ── 浏览模式操作（响应均为刷新后的列表片段） ─────────────────────────────

async def _need(request: Request, path: str, name: str = "") -> tuple:
    """公共前置校验：(cloud, 错误片段|None)。name 可选（目录操作不需要）。"""
    cloud = get_cloud(request)
    if cloud is None:
        return None, await _render_files(request, path, err="115 客户端未就绪")
    if name and ("/" in name or name.strip() in ("", "..")):
        return None, await _render_files(request, path, err="条目名不合法")
    return cloud, None


def _find_fid(entry: dict) -> str:
    from scripts.manual import entry_fid
    return entry_fid(entry)


@router.post("/files/delete", response_class=HTMLResponse)
async def files_delete(request: Request, path: str = Form(...), name: str = Form(...)) -> HTMLResponse:
    cloud, bad = await _need(request, path, name)
    if bad:
        return bad
    try:
        from cloud115.filesystem import find_entry
        entry = await find_entry(cloud, path_join(path, name))
        await cloud.raw.delete_files([_find_fid(entry)])
        cloud.raw.invalidate_path_cache()
        msg, err = f"已删除 {name}（移入 115 回收站，可在网盘恢复）", ""
    except Exception as e:  # noqa: BLE001
        msg, err = "", f"删除失败: {e}"
    return await _render_files(request, path, msg=msg, err=err)


@router.post("/files/rename", response_class=HTMLResponse)
async def files_rename(request: Request, path: str = Form(...), name: str = Form(...),
                       value: str = Form("")) -> HTMLResponse:
    cloud, bad = await _need(request, path, name)
    if bad:
        return bad
    new_name = value.strip()
    if not new_name or "/" in new_name:
        return await _render_files(request, path, err="新名称不能为空且不含 /")
    try:
        from cloud115.filesystem import find_entry
        entry = await find_entry(cloud, path_join(path, name))
        await cloud.raw.rename_file(_find_fid(entry), new_name)
        msg, err = f"已重命名 {name} → {new_name}", ""
    except Exception as e:  # noqa: BLE001
        msg, err = "", f"重命名失败: {e}"
    return await _render_files(request, path, msg=msg, err=err)


@router.post("/files/move", response_class=HTMLResponse)
async def files_move(request: Request, path: str = Form(...), name: str = Form(...),
                     value: str = Form("")) -> HTMLResponse:
    cloud, bad = await _need(request, path, name)
    if bad:
        return bad
    dest = value.strip()
    if not dest:
        return await _render_files(request, path, err="目标目录不能为空")
    try:
        from cloud115.filesystem import find_entry
        entry = await find_entry(cloud, path_join(path, name))
        info = await cloud.raw.get_file_info(dest)
        to_cid = int(info["file_id"]) if info and info.get("file_id") is not None \
            else int(await cloud.raw.create_dir_recursive(dest))
        await cloud.raw.move_files(_find_fid(entry), to_cid)
        cloud.raw.invalidate_path_cache()
        msg, err = f"已移动 {name} → {dest}", ""
    except Exception as e:  # noqa: BLE001
        msg, err = "", f"移动失败: {e}"
    return await _render_files(request, path, msg=msg, err=err)


@router.post("/files/mkdir", response_class=HTMLResponse)
async def files_mkdir(request: Request, path: str = Form(...), value: str = Form("")) -> HTMLResponse:
    cloud, bad = await _need(request, path)
    if bad:
        return bad
    name = value.strip()
    if not name or "/" in name:
        return await _render_files(request, path, err="目录名不能为空且不含 /（逐级进入再建）")
    try:
        from cloud115.filesystem import mkdir_p
        target = path_join(path, name)
        await mkdir_p(cloud, target)
        cloud.raw.invalidate_path_cache()
        msg, err = f"已创建 {target}", ""
    except Exception as e:  # noqa: BLE001
        msg, err = "", f"新建失败: {e}"
    return await _render_files(request, path, msg=msg, err=err)


@router.post("/files/search", response_class=HTMLResponse)
async def files_search(request: Request, kw: str = Form("")) -> HTMLResponse:
    kw = kw.strip()
    if not kw:
        return await _render_files(request, "/tg115bot", err="关键词不能为空")
    return await _render_files(request, kw=kw)


# ── 下载 / 上传（启动后台任务，响应为传输任务片段） ──────────────────────

def _start_download(cloud, pc: str, name: str, dest_dir: str) -> dict:
    job = _new_job("download", name or pc)
    task = asyncio.create_task(_run_download(job, cloud, pc, dest_dir))
    job["_task"] = task        # 防任务被 GC；快照渲染时下划线键被模板忽略
    return job


def _start_upload(cloud, src: str, remote_base: str) -> dict:
    job = _new_job("upload", src)
    task = asyncio.create_task(_run_upload(job, cloud, src, remote_base))
    job["_task"] = task
    return job


@router.post("/files/download", response_class=HTMLResponse)
async def files_download(request: Request, path: str = Form(""), name: str = Form(""),
                         pc: str = Form(""), value: str = Form("")) -> HTMLResponse:
    cloud = get_cloud(request)
    if cloud is None:
        return await _render_files(request, path or "/tg115bot", err="115 客户端未就绪")
    dest_dir = value.strip()
    if not dest_dir:
        return await _render_files(request, path or "/tg115bot", err="本地目录不能为空")
    try:
        if not pc:
            from cloud115.filesystem import find_entry
            entry = await find_entry(cloud, path_join(path, name))
            pc = str(entry.get("pc") or "")
            if not pc:
                raise RuntimeError("条目缺 pick_code，无法取直链")
            name = name or str(entry.get("fn") or "?")
    except Exception as e:  # noqa: BLE001
        return await _render_files(request, path or "/tg115bot", err=f"下载失败: {e}")
    job = _start_download(cloud, pc, name, dest_dir)
    return templates.TemplateResponse(request, "_jobs.html", {"jobs": job_snapshot()})


@router.post("/files/upload", response_class=HTMLResponse)
async def files_upload(request: Request, path: str = Form(...), src: str = Form("")) -> HTMLResponse:
    cloud = get_cloud(request)
    if cloud is None:
        return await _render_files(request, path, err="115 客户端未就绪")
    src = src.strip()
    if not src:
        return await _render_files(request, path, err="本地路径不能为空")
    job = _start_upload(cloud, src, path)
    return templates.TemplateResponse(request, "_jobs.html", {"jobs": job_snapshot()})
