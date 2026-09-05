"""Web 台·配置页：参数编辑（YAML 全文）/ 快捷开关 / 115 扫码授权 / 服务重启 / 诊断。

配置读写复用 tb.ops 的纯函数（validate/write/set_key，带备份与双重校验）；
115 授权走主账号客户端的 PKCE 扫码流程——Web 版渲染真二维码图片（SVG），
比 TUI 的 ASCII 二维码扫码体验好得多，轮询用 HTMX 自替换片段。

重启语义注意：Web 活在服务进程里，直接重启是自杀——派生 detached 进程
延迟 2s 执行 service.do_restart()，让响应先送达浏览器；且仅当本进程确为
tb 托管的服务进程（PID 文件命中自身）时才亮出该按钮。
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from web.helpers import get_cloud, page_ctx, templates

router = APIRouter()

# 快捷开关：(配置键, 展示名)。与 TUI ConfigPage 的三开关一致
_TOGGLE_KEYS = [("web.enable", "Web 台"),
                ("storage.keep_local", "本地副本"),
                ("channel_monitor.enabled", "频道监控")]


# ── 参数编辑 ─────────────────────────────────────────────────────────────

def _editor_data(msg: str = "", err: str = "") -> dict:
    """配置编辑器片段上下文：文件全文 + 三开关当前值（读 config.yaml，非内存配置）。"""
    from tb import ops

    text, read_err = "", ""
    try:
        text = ops.CONFIG_FILE.read_text(encoding="utf-8")
    except OSError as e:
        read_err = f"读取失败: {e}（先 tb init）"

    toggles = []
    if not read_err:
        from config import load_config
        try:
            cfg = load_config()
            values = {"web.enable": cfg.web.enable,
                      "storage.keep_local": cfg.storage.keep_local,
                      "channel_monitor.enabled": cfg.channel_monitor.enabled}
            toggles = [{"key": k, "label": label, "on": bool(values.get(k))}
                       for k, label in _TOGGLE_KEYS]
        except Exception as e:  # noqa: BLE001
            read_err = f"配置解析失败: {e}"
    return {"text": text, "toggles": toggles, "msg": msg, "err": err,
            "read_err": read_err}


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request) -> HTMLResponse:
    data = _editor_data()
    auth = _auth_data(request)
    return templates.TemplateResponse(request, "config.html", page_ctx(
        request, active="config", **data, **auth))


@router.get("/partials/config-editor", response_class=HTMLResponse)
async def config_editor_partial(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "_config_editor.html", _editor_data())


@router.post("/config/save", response_class=HTMLResponse)
async def config_save(request: Request, text: str = Form("")) -> HTMLResponse:
    from tb import ops

    ok, msg = ops.validate_config_text(text)
    if not ok:
        data = _editor_data(err=f"未保存——{msg}")
    else:
        try:
            ops.write_config_text(text)
            data = _editor_data(msg="已保存并通过校验（自动备份旧文件；大部分参数重启服务后生效）")
        except OSError as e:
            data = _editor_data(err=f"写盘失败: {e}")
    return templates.TemplateResponse(request, "_config_editor.html", data)


@router.post("/config/toggle", response_class=HTMLResponse)
async def config_toggle(request: Request, key: str = Form("")) -> HTMLResponse:
    from tb import ops

    labels = dict(_TOGGLE_KEYS)
    if key not in labels:
        data = _editor_data(err=f"未知开关: {key}")
    else:
        # 现值取反（读 config.yaml，与编辑器同源）
        from config import load_config
        try:
            cfg = load_config()
            cur = bool({"web.enable": cfg.web.enable,
                        "storage.keep_local": cfg.storage.keep_local,
                        "channel_monitor.enabled": cfg.channel_monitor.enabled}[key])
        except Exception as e:  # noqa: BLE001
            cur = False
        ok, msg = ops.set_config_key(key, not cur)
        if ok:
            data = _editor_data(msg=f"{labels[key]} = {'开' if not cur else '关'}"
                                    "（已写盘，重启服务生效）")
        else:
            data = _editor_data(err=f"{labels[key]} 修改失败: {msg}")
    return templates.TemplateResponse(request, "_config_editor.html", data)


@router.post("/config/restart", response_class=HTMLResponse)
async def config_restart(request: Request) -> HTMLResponse:
    """派生脱离进程延迟重启（本进程即服务进程，必须让响应先走完）。"""
    from tb import service

    if service.live_pid() != os.getpid():
        return HTMLResponse("<p class='err'>本服务非 tb 托管（PID 文件未命中本进程），"
                            "请手动执行 <code>tb restart</code>。</p>")
    code = (f"import sys, time; sys.path.insert(0, {str(service.INSTALL_DIR)!r}); "
            "time.sleep(2); from tb import service; sys.exit(service.do_restart())")
    kwargs = {}
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, "-c", code], cwd=str(service.INSTALL_DIR),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    except OSError as e:
        return HTMLResponse(f"<p class='err'>重启派生失败: {e}（请手动 tb restart）</p>")
    return HTMLResponse(
        "<p class='ok'>重启指令已发出，服务正在重启（Web 台随之中断）…"
        "约 15 秒后本页自动刷新；若未恢复请稍后手动刷新。</p>"
        "<script>setTimeout(function(){ location.reload(); }, 15000);</script>")


# ── 诊断（tb doctor 的只读子集，放线程跑避免阻塞事件循环） ────────────────

@router.post("/partials/doctor", response_class=HTMLResponse)
async def doctor_partial(request: Request) -> HTMLResponse:
    import asyncio as _asyncio

    from tb import ops

    def run():
        return ops.doctor_checks()

    try:
        checks = await _asyncio.to_thread(run)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(f"<p class='err'>诊断失败: {e}</p>")
    ok = all(c[1] for c in checks)
    rows = "".join(
        f"<div class='{ 'ok' if fine else 'err' }'>{'✅' if fine else '❌'} "
        f"<b>{name}</b>：{detail}</div>"
        for name, fine, detail in checks)
    verdict = "一切正常 ✅" if ok else "存在需要处理的项目 ❌"
    return HTMLResponse(f"<div class='card'>{rows}"
                        f"<p style='margin-bottom:0'><b>结论：{verdict}</b></p></div>")


# ── 115 扫码授权（PKCE；授权对象 = 主账号客户端） ────────────────────────

# 进程内单路扫码会话（同一时刻只跑一个流程；新 start 覆盖旧的）
_qr_state: dict = {}


def _qr_svg_url(data: str) -> str:
    """二维码内容 -> SVG data URL（真图扫码，免 TUI ASCII 的终端色反问题）。"""
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=12)
        b64 = base64.b64encode(img.to_string()).decode()
        return f"data:image/svg+xml;base64,{b64}"
    except Exception:  # noqa: BLE001 -- 缺 qrcode 库时退回文本链接
        return ""


def _auth_data(request: Request, qr_img: str = "", qr_account: str = "",
               auth_msg: str = "") -> dict:
    accounts = request.app.state.accounts
    status = accounts.status_list() if accounts else []
    return {"qr_img": qr_img, "qr_account": qr_account, "auth_msg": auth_msg,
            "account_status": status, "auth_state": dict(_qr_state)}


def _auth_fragment(request: Request, **kw) -> HTMLResponse:
    return templates.TemplateResponse(request, "_auth.html", _auth_data(request, **kw))


@router.post("/auth/qr/start", response_class=HTMLResponse)
async def auth_start(request: Request) -> HTMLResponse:
    cloud = get_cloud(request)
    if cloud is None:
        return _auth_fragment(request, auth_msg="115 客户端未就绪（先 tb init 并重启服务）")
    try:
        qr = await cloud.raw.start_qr_auth()
    except Exception as e:  # noqa: BLE001
        return _auth_fragment(request, auth_msg=f"获取二维码失败: {e}")
    _qr_state.clear()
    _qr_state.update(uid=qr["uid"], t=qr["time"], sign=qr["sign"],
                     verifier=qr["verifier"], account=cloud.account.name)
    img = _qr_svg_url(qr["qrcode"])
    return _auth_fragment(request, qr_img=img, qr_account=cloud.account.name,
                          auth_msg="" if img else "缺 qrcode 库，无法出图；扫码链接："
                                                  + str(qr["qrcode"]))


@router.get("/partials/auth-poll", response_class=HTMLResponse)
async def auth_poll(request: Request) -> HTMLResponse:
    """轮询扫码状态（HTMX outerHTML 自替换；终态片段不含触发器即停止轮询）。"""
    cloud = get_cloud(request)
    if cloud is None or not _qr_state.get("uid"):
        return HTMLResponse("<div class='err'>扫码会话丢失，请重新生成二维码。</div>")
    trigger = ("<div id='auth-poll' hx-get='/partials/auth-poll' "
               "hx-trigger='every 3s' hx-swap='outerHTML'>")
    try:
        status = await cloud.raw.poll_qr_status(_qr_state["uid"], _qr_state["t"],
                                                _qr_state["sign"])
    except Exception as e:  # noqa: BLE001 -- 网络抖动继续轮询
        return HTMLResponse(f"{trigger}<span class='warn'>轮询异常（继续）：{e}</span></div>")
    if status == 2:
        try:
            await cloud.raw.exchange_qr_token(_qr_state["uid"], _qr_state["verifier"])
            accounts = request.app.state.accounts
            name = _qr_state.get("account") or ""
            if accounts is not None:
                accounts.mark_authorized(name)
            _qr_state.clear()
            return HTMLResponse(f"<div class='ok'>✅ 账号 {name} 授权成功，token 已保存"
                                "（对该账号立即生效，无需重启）。</div>")
        except Exception as e:  # noqa: BLE001
            _qr_state.clear()
            return HTMLResponse(f"<div class='err'>换取 token 失败: {e}（请重新扫码）</div>")
    if status == -1:
        _qr_state.clear()
        return HTMLResponse("<div class='err'>二维码已过期，请重新生成。</div>")
    if status == -2:
        _qr_state.clear()
        return HTMLResponse("<div class='err'>你在 115 APP 上取消了授权。</div>")
    label = "已扫码，请在 115 APP 上确认…" if status == 1 else "等待扫码…（约 5 分钟有效）"
    return HTMLResponse(f"{trigger}<span class='hint'>{label}</span></div>")
