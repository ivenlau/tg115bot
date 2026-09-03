"""Textual TUI——裸 `tb` 的交互模式（P2）。

五个页面：仪表盘 / 文件 / 离线任务 / 日志 / 授权。侧栏导航 + Footer 快捷键。
数据层与 CLI 完全共用（manual.py 的 helpers + cloud115 client），TUI 只是视图。

关键设计——常驻 115 循环：
  aiohttp 的 ClientSession 绑定创建它的事件循环。若在某次 asyncio.run() 的
  临时循环上建 ctx、再在另一次临时循环上使用，就会连环 "Event loop is closed"。
  因此 115 客户端一生都活在 `_CloudLoop`（专属线程 + run_forever 循环）上，
  页面的线程 worker 经 submit() 跨线程提交协程，UI 循环永不阻塞、会话永不换循环。
"""
from __future__ import annotations

import asyncio
import io
import os
import shutil
import threading
import time
from concurrent.futures import Future
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label,
                             ListView, ListItem, RichLog, Static, Switch,
                             TabbedContent, TabPane, TextArea)

from tb import service

PAGES = ["仪表盘", "文件", "离线任务", "配置", "日志"]


def _post(app, fn, *args, **kwargs):
    """call_from_thread 安全包装：应用退出竞态时静默丢弃（worker 不因它炸）。"""
    try:
        app.call_from_thread(fn, *args, **kwargs)
    except Exception:  # noqa: BLE001
        pass


def _qr_ascii(data: str) -> str:
    """QR 数据 → 终端 ASCII 二维码文本（qrcode 纯核心；缺库退回链接文本）。"""
    try:
        import qrcode
        buf = io.StringIO()
        q = qrcode.QRCode()
        q.add_data(data)
        q.make(fit=True)
        q.print_ascii(out=buf, invert=os.environ.get("QR_INVERT", "1") != "0")
        return buf.getvalue()
    except ImportError:
        return data


class _CloudLoop:
    """常驻线程 + 专属事件循环：115 客户端（aiohttp session）绑定其上。

    submit() 从任意线程把协程投递到该循环执行并阻塞等待结果；
    所有 115 调用共用这一个循环，会话不会跨循环使用。
    """

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stopped = False
        self._tasks: set = set()        # 在跑协程任务（stop 时真取消，防退出挂死）
        self._futures: set = set()      # 提交的 concurrent future（未启动的可直接 cancel）
        self._thread = threading.Thread(target=self._run, name="tb-cloud", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()
        # 收尾排空：让被取消的任务/异步生成器（aiohttp）完成，再关循环
        try:
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        except Exception:  # noqa: BLE001
            pass
        self.loop.close()

    async def _tracked(self, coro):
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            return await coro
        finally:
            self._tasks.discard(task)

    def submit(self, coro) -> Future:
        self._ready.wait(5)
        if self._stopped or self.loop is None or not self.loop.is_running():
            coro.close()          # 防未 await 警告
            raise RuntimeError("cloud 循环已停止")
        fut = asyncio.run_coroutine_threadsafe(self._tracked(coro), self.loop)
        self._futures.add(fut)
        fut.add_done_callback(self._futures.discard)
        return fut

    def stop(self) -> None:
        self._stopped = True
        if self.loop is not None and self.loop.is_running():
            def _cancel_and_stop() -> None:
                for t in list(self._tasks):     # 在跑的上传/轮询/初始化全部取消
                    t.cancel()
                for f in list(self._futures):   # 未启动的直接取消
                    f.cancel()
                # 关键：下一轮再停——cancel 只标记，CancelledError 的送达需要
                # 再跑一轮循环；同轮立即 stop 会让 future 永不完成（worker 卡满
                # 超时、进程退出挂死的根因）
                self.loop.call_soon(self.loop.stop)
            self.loop.call_soon_threadsafe(_cancel_and_stop)


class TBApp(App):
    """tg115bot 主应用。"""

    TITLE = "tg115bot"
    CSS = """
    Screen { layout: horizontal; }
    #sidebar { width: 26; border-right: solid $primary; background: $surface; padding: 1; height: 1fr; }
    #sidebar ListView { height: auto; }
    #side-status { dock: bottom; color: $text-muted; padding: 1 1 0 1; }
    #content { padding: 1 2; height: 1fr; }
    .page-title { text-style: bold; color: $text; margin-bottom: 1; }
    .hint { color: $text-muted; margin-top: 1; }
    .hidden { display: none; }
    #svc-btns { height: auto; margin: 1 0; }
    #svc-btns Button { margin-right: 1; }
    #doctor-out { padding-top: 1; margin-bottom: 1; }
    #cfg-switches { height: auto; margin: 0 0 1 0; }
    #cfg-switches Switch { margin: 0 1 0 0; }
    #cfg-switches Label { margin: 0 2 0 0; }
    #cfg-text { height: 1fr; }
    #cfg-btns { height: auto; margin: 1 0; }
    #cfg-btns Button { margin-right: 1; }
    #dl-status { color: $text-muted; }
    DataTable { height: 1fr; }
    RichLog { height: 1fr; }
    """

    BINDINGS = [("q", "quit", "退出")]
    ctx = None            # manual.Ctx（后台就绪后填充）
    db = None             # persistence.Database（后台就绪后填充；只读用途）
    ctx_error: str = ""   # 构建失败原因（未配置/未授权等）

    def __init__(self, account: str = "") -> None:
        super().__init__()
        self.account = account
        self._cloud = _CloudLoop()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield ListView(*[ListItem(Label(p)) for p in PAGES], id="nav")
                yield Static("初始化中…", id="side-status")
            yield Container(id="content")
        yield Footer()

    def on_mount(self) -> None:
        # 初始化整体投给常驻循环 fire-and-forget：退出时可被真取消，不占线程
        # worker——消除「退出时 worker 卡 .result 拖死进程」的一整类问题
        self._cloud.submit(self._init_all())
        self.query_one("#nav", ListView).index = 0   # 触发 Highlighted -> 挂载首页

    async def _init_all(self) -> None:
        """建 115 上下文 + DB 句柄（need_login=False，未授权可去配置页扫码）。"""
        from scripts import manual

        try:
            cfg = manual.load_config()
        except Exception as e:  # noqa: BLE001
            self.ctx_error = f"配置加载失败: {e}（tb init）"
            _post(self, self._side_status, self.ctx_error)
            return

        try:
            ctx = await manual.build_ctx(cfg, self.account, need_login=False)
        except Exception as e:  # noqa: BLE001
            self.ctx_error = str(e)
            _post(self, self._side_status, self.ctx_error)
            return
        # 任务列表数据源（WAL 并发读安全；失败则仪表盘任务区静默降级）
        db = None
        try:
            from persistence.db import Database
            db = Database(cfg.db_path)
            await db.init()
        except Exception:  # noqa: BLE001
            if db is not None:
                try:
                    await db.close()
                except Exception:  # noqa: BLE001
                    pass
            db = None
        self.ctx, self.db = ctx, db
        _post(self, self._side_status, f"账号 {ctx.account.name}")

    def _side_status(self, text: str) -> None:
        try:
            self.query_one("#side-status", Static).update(text)
        except Exception:  # noqa: BLE001 -- 页面未挂载时忽略
            pass

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        # 用 Highlighted 而非 Selected：键盘/鼠标与程序化改 index 都会触发
        idx = event.list_view.index
        if idx is not None:
            self._show_page(idx)

    def _show_page(self, idx: int) -> None:
        content = self.query_one("#content", Container)
        page_cls = [DashboardPage, FilesPage, OfflinePage, ConfigPage, LogPage][idx]

        async def _swap() -> None:
            await content.remove_children()
            await content.mount(page_cls())
        self.call_after_refresh(_swap)

    def notify_user(self, msg: str, error: bool = False) -> None:
        self.notify(msg, severity="error" if error else "information")

    def on_unmount(self) -> None:
        """退出：在常驻循环上关 DB 与 115 会话，然后停循环。"""
        ctx, db, self.ctx, self.db = self.ctx, self.db, None, None
        try:
            if db is not None:
                self._cloud.submit(db.close()).result(5)
        except Exception:  # noqa: BLE001
            pass
        try:
            if ctx is not None:
                self._cloud.submit(ctx.cloud.close()).result(10)
        except Exception:  # noqa: BLE001 -- 关闭失败不影响退出
            pass
        self._cloud.stop()


# ── 页面基类：定时器管理 ────────────────────────────────────────────────

class Page(Vertical):
    """页面基类：注册的定时器随卸载停止（注意不可占用 _timers——那是 Textual 内部字段）。"""

    def __init__(self) -> None:
        super().__init__()
        self._page_timers = []

    def every(self, period: float, fn) -> None:
        self._page_timers.append(self.set_interval(period, fn))

    def on_unmount(self) -> None:
        for t in self._page_timers:
            t.stop()

    @property
    def app_ctx(self):
        return self.app.ctx  # noqa: SLF001 -- TUI 页面与 App 同族


# ── 仪表盘 ──────────────────────────────────────────────────────────────

TASK_ICON = {"queued": "⏳", "downloading": "⬇️", "uploading": "⬆️",
             "done": "✅", "failed": "❌", "cancelled": "🚫"}


class DashboardPage(Page):
    def compose(self) -> ComposeResult:
        yield Label("仪表盘", classes="page-title")
        yield Static("加载中…", id="dash")
        with Horizontal(id="svc-btns"):
            yield Button("启动", id="btn-start", variant="success")
            yield Button("停止", id="btn-stop", variant="error")
            yield Button("重启", id="btn-restart", variant="warning")
            yield Button("诊断", id="btn-doctor")
        yield Static("", id="doctor-out", classes="hidden")
        yield Label("最近任务（bot 侧：TG 上传/频道监控/备份/直链；5s 刷新）",
                    classes="hint")
        yield DataTable(id="tasks", zebra_stripes=True)

    def on_mount(self) -> None:
        t = self.query_one("#tasks", DataTable)
        t.add_columns("时间", "文件名", "大小", "状态", "方式/来源")
        self.every(5, self.refresh_dash)
        self.refresh_dash()

    def refresh_dash(self) -> None:
        lines: list[str] = []
        # 服务
        pid = service.live_pid()
        if pid:
            try:
                import psutil
                p = psutil.Process(pid)
                mb = p.memory_info().rss / 1048576
                up = time.time() - p.create_time()
                d, rem = divmod(int(up), 86400)
                up_s = f"{d}d {rem // 3600:02d}:{rem % 3600 // 60:02d}:{rem % 60:02d}"
                lines.append(f"🟢 服务运行中   PID {pid}   内存 {mb:.0f}MB   已运行 {up_s}")
            except Exception:  # noqa: BLE001
                lines.append(f"🟢 服务运行中   PID {pid}")
        else:
            lines.append("🔴 服务未运行（点下方「启动」）")
        # 磁盘
        try:
            free = shutil.disk_usage(str(service.INSTALL_DIR)).free / 1024 ** 3
            lines.append(f"💾 磁盘剩余     {free:.1f}GB")
        except OSError:
            pass
        lines.append("")
        # 115（需授权）
        ctx = self.app_ctx
        if ctx is None:
            lines.append(f"☁️ 115          {self.app.ctx_error or '初始化中…'}")
        else:
            def fill() -> None:
                async def go():
                    try:
                        sp = await ctx.cloud.raw.user_space()
                        if sp.get("total"):
                            from core.progress import human_bytes
                            pct = sp["used"] * 100 / sp["total"]
                            lines.append(
                                f"☁️ 115 空间     {human_bytes(sp['used'])} / "
                                f"{human_bytes(sp['total'])}（{pct:.1f}%）")
                        q = await ctx.cloud.raw.offline_quota()
                        if q:
                            lines.append(f"⬇️ 离线配额     已用 {q.get('used', '?')} / {q.get('count', '?')}")
                        lines.append(f"🛡 API 余量     今日已用 {ctx.cloud.raw.request_count}"
                                     f"（阈值 {ctx.cloud.raw.daily_limit}）")
                    except Exception as e:  # noqa: BLE001
                        lines.append(f"☁️ 115          未授权或不可达（{e}）→ 去「配置」页扫码")
                self.app._cloud.submit(go()).result(60)
                _post(self.app, 
                    self.query_one("#dash", Static).update, "\n".join(lines))
            self.run_worker(fill, thread=True)
        self._refresh_tasks()

    # ── 最近任务（tasks 表倒序；WAL 并发读安全） ─────────────────────────

    def _refresh_tasks(self) -> None:
        db = self.app.db
        if db is None:
            return

        def fill() -> None:
            async def go():
                return await db.recent_tasks(15)
            try:
                rows = self.app._cloud.submit(go()).result(10)
            except Exception:  # noqa: BLE001 -- 单轮查询失败静默跳过
                return

            def apply() -> None:
                from core.progress import human_bytes
                t = self.query_one("#tasks", DataTable)
                t.clear()
                if not rows:
                    t.add_row("", "暂无任务——向 bot 发送文件即可开始", "", "", "")
                    return
                for r in rows:
                    tm = time.strftime("%m-%d %H:%M", time.localtime(r.created_at or 0))
                    icon = TASK_ICON.get(r.status, r.status)
                    st = f"{icon} {r.status}" if r.status in ("downloading", "uploading", "queued") else icon
                    via = (r.method or r.source or "").strip()
                    t.add_row(tm, r.filename or "?", human_bytes(r.size or 0), st, via)
            _post(self.app, apply)
        self.run_worker(fill, thread=True, group="dash-tasks", exclusive=True)

    # ── 服务控制（线程 worker；exclusive 防连点竞态） ─────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "btn-doctor":
            self._run_doctor()
            return
        actions = {"btn-start": ("启动", service.do_start),
                   "btn-stop": ("停止", service.do_stop),
                   "btn-restart": ("重启", service.do_restart)}
        if bid in actions:
            label, fn = actions[bid]
            self._svc_action(label, fn)

    def _svc_action(self, label: str, fn) -> None:
        def run() -> None:
            _post(self.app, self._set_buttons, False)
            rc = fn()
            _post(self.app, self._set_buttons, True)
            _post(self.app, self.refresh_dash)
            _post(self.app, 
                self.app.notify_user,
                f"{label}完成" if rc == 0 else f"{label}失败（exit {rc}）",
                error=bool(rc))
        self.run_worker(run, thread=True, group="svc", exclusive=True)

    def _set_buttons(self, enabled: bool) -> None:
        for b in self.query(Button):
            b.disabled = not enabled

    def _run_doctor(self) -> None:
        from tb import ops

        def run() -> None:
            checks = ops.doctor_checks()
            lines = [f"  {'✅' if fine else '❌'} {name}: {detail}"
                     for name, fine, detail in checks]
            ok = all(c[1] for c in checks)
            lines.append("  结论: " + ("一切正常 ✅" if ok else "存在需要处理的项目 ❌"))

            def apply() -> None:
                out = self.query_one("#doctor-out", Static)
                out.remove_class("hidden")
                out.update("\n".join(lines))
            _post(self.app, apply)
        self.run_worker(run, thread=True, group="doctor", exclusive=True)


# ── 文件浏览 ────────────────────────────────────────────────────────────

class FilesPage(Page):
    def __init__(self) -> None:
        super().__init__()
        self.path = "/tg115bot"
        self._confirm_key: str | None = None
        self._pending: tuple[str, str] | None = None   # (动作, 目标名)：download/rename/move/mkdir
        self._load_gen = 0      # 代际号：导航后的过期刷新结果不再落表

    def compose(self) -> ComposeResult:
        yield Label("文件（115 网盘）", classes="page-title")
        yield Static(self.path, id="files-path")
        yield DataTable(id="files", cursor_type="row", zebra_stripes=True)
        yield Input(placeholder="上传：输入本地文件/目录/通配符路径后回车（如 /data/photos 或 D:\\p*.jpg）",
                    id="upload-input")
        yield Input(placeholder="动作输入框", id="action-input", classes="hidden")
        yield Static("", id="dl-status", classes="hidden")
        yield Label("回车=进入 · d=删除(两次) · s=下载 · n=重命名 · m=移动 · +=新建 · r=刷新 · 输入框=上传",
                    classes="hint")

    def on_mount(self) -> None:
        t = self.query_one("#files", DataTable)
        t.add_columns("类型", "名称", "大小")
        t.focus()          # 键盘优先：挂载即聚焦表格，r/d/s 立即可用
        self.load_dir()

    def load_dir(self) -> None:
        ctx = self.app_ctx
        if ctx is None:
            self.query_one("#files", DataTable).add_row("⚠️", self.app.ctx_error or "初始化中…", "")
            return
        self._load_gen += 1
        gen = self._load_gen

        def fill() -> None:
            from cloud115.filesystem import resolve_cid
            from scripts.manual import (entry_is_dir, entry_name, entry_size,
                                        sort_entries)

            async def go():
                from core.progress import human_bytes
                cid = await resolve_cid(ctx.cloud, self.path)
                data = await ctx.cloud.raw.list_files(int(cid), limit=100)
                items = sort_entries(data.get("list") or [])
                return [("📂" if entry_is_dir(it) else "📄", entry_name(it),
                         "" if entry_is_dir(it) else human_bytes(entry_size(it)))
                        for it in items]

            def err(msg: str) -> None:
                if gen != self._load_gen:
                    return
                t = self.query_one("#files", DataTable)
                t.clear()
                t.add_row("⚠️", msg, "")
            try:
                rows = self.app._cloud.submit(go()).result(60)
            except Exception as e:  # noqa: BLE001 -- 网络/授权问题不炸 worker
                _post(self.app, err, f"加载失败: {e}")
                return

            def apply() -> None:
                if gen != self._load_gen:      # 期间已导航到别处，丢弃过期结果
                    return
                t = self.query_one("#files", DataTable)
                t.clear()
                t.add_row("📂", "..", "")      # 图标必须与目录一致，回车才能回上级
                for r in rows:
                    t.add_row(*r)              # 不指定 key：115 列表的「递归混入」形态
                                                 # 会出现同名条目，按名作 key 会 DuplicateKey
                self.query_one("#files-path", Static).update(self.path)
            _post(self.app, apply)
        self.run_worker(fill, thread=True)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key is None:
            return
        try:
            row = self.query_one("#files", DataTable).get_row(event.row_key)
        except Exception:  # noqa: BLE001 -- 行已被 clear/重建
            return
        if not row or not str(row[0]).startswith("📂"):
            return
        name = str(row[1])     # 名字取行数据（未用 key，key 是自动生成的）
        if name == "..":
            # 纯字符串求父路径——不用 Path（Windows 上 Path('/tg115bot').parent
            # 会把 / 换成 \，115 路径全是 /，直接报错）
            cut = self.path.rstrip("/")
            self.path = cut[: cut.rfind("/")] or "/"
        else:
            self.path = self.path.rstrip("/") + "/" + name
        self.load_dir()

    def on_key(self, event) -> None:  # noqa: BLE001
        if event.key == "r":
            self.load_dir()
        elif event.key == "d":
            self._try_delete()
        elif event.key == "s":
            sel = self._selected()
            if sel and sel[0].startswith("📂"):
                self.app.notify_user("目录下载 v1 不支持（逐文件取直链会快速烧 API 配额）", True)
            elif sel:
                self._ask("download", sel[1],
                          f"下载 {sel[1]} 到本地目录（回车开始，Esc 取消）")
        elif event.key == "n":
            sel = self._selected()
            if sel:
                self._ask("rename", sel[1],
                          f"重命名 {sel[1]} 为（回车确认，Esc 取消）", prefill=sel[1])
        elif event.key == "m":
            sel = self._selected()
            if sel:
                self._ask("move", sel[1],
                          f"移动 {sel[1]} 到 115 目录（不存在自动创建，Esc 取消）",
                          prefill=self.path)
        elif event.key in ("+", "plus"):
            self._ask("mkdir", "",
                      f"在 {self.path} 下新建目录（回车创建，Esc 取消）")
        elif event.key == "escape":
            self._cancel_ask()

    # ── 通用动作弹框：一个隐藏 Input 承载 download/rename/move/mkdir ─────

    def _selected(self) -> tuple[str, str] | None:
        """当前光标行 (类型icon, 名字)；无效或 .. 返回 None。"""
        t = self.query_one("#files", DataTable)
        if t.cursor_row is None or t.cursor_row < 0:
            return None
        try:
            row = t.get_row_at(t.cursor_row)
        except Exception:  # noqa: BLE001
            return None
        name = str(row[1])
        if name == "..":
            return None
        return str(row[0]), name

    def _ask(self, action: str, name: str, placeholder: str, prefill: str = "") -> None:
        self._pending = (action, name)
        inp = self.query_one("#action-input", Input)
        inp.placeholder = placeholder
        inp.value = prefill
        inp.remove_class("hidden")
        self.call_after_refresh(inp.focus)

    def _cancel_ask(self) -> None:
        self._pending = None
        try:
            self.query_one("#action-input", Input).add_class("hidden")
            self.query_one("#files", DataTable).focus()
        except Exception:  # noqa: BLE001
            pass

    def _download(self, name: str, dest_dir: Path) -> None:
        ctx = self.app_ctx
        if ctx is None:
            self.app.notify_user("115 未就绪", error=True)
            return
        status = self.query_one("#dl-status", Static)
        status.remove_class("hidden")
        full = self.path.rstrip("/") + "/" + name
        status.update(f"⬇️ {name}  准备中…")
        last = [0.0]

        def on_progress(written: int, total: int) -> None:
            now = time.monotonic()
            if now - last[0] < 1.0 and written != total:
                return
            last[0] = now
            from core.progress import human_bytes
            pct = f" {written * 100 // total}%" if total else ""
            _post(self.app, status.update,
                                      f"⬇️ {name}  {human_bytes(written)}{pct}")

        def dl() -> None:
            from cloud115.download import (download_file, parse_downurl,
                                           sanitize_name, unique_dest)
            from cloud115.filesystem import find_entry
            from core.progress import human_bytes
            from scripts.manual import entry_name

            async def go():
                entry = await find_entry(ctx.cloud, full)
                pc = str(entry.get("pc") or "")
                if not pc:
                    raise RuntimeError("条目缺 pickcode，无法取直链")
                info = parse_downurl(await ctx.cloud.raw.get_download_url(pc))
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = unique_dest(dest_dir,
                                   sanitize_name(info["file_name"] or entry_name(entry)))
                size, sha1 = await download_file(info["url"], dest,
                                                 expected_size=info["file_size"],
                                                 on_progress=on_progress)
                return info, dest, size, sha1

            try:
                info, dest, size, sha1 = self.app._cloud.submit(go()).result()
            except Exception as e:  # noqa: BLE001
                _post(self.app, status.update, f"❌ 下载失败: {e}")
                _post(self.app, self.app.notify_user, f"下载失败: {e}", True)
                return
            part = dest.with_name(dest.name + ".part")
            if info.get("sha1") and sha1 != info["sha1"]:
                _post(self.app, 
                    status.update, f"❌ SHA1 不符，现场保留: {part.name}")
                _post(self.app, self.app.notify_user, "SHA1 不符，已保留 .part", True)
                return
            part.rename(dest)
            _post(self.app, 
                status.update, f"✅ {dest.name}  {human_bytes(size)}  ->  {dest}")
            _post(self.app, self.app.notify_user, f"✅ 已下载到 {dest}")
        self.run_worker(dl, thread=True)

    # ── 重命名 / 移动 / 新建目录：同一套「cloud 循环 + 局部刷新」模式 ─────

    def _rename(self, name: str, new_name: str) -> None:
        if not new_name or "/" in new_name:
            self.app.notify_user("新名不能为空且不含 /", True)
            return
        ctx = self.app_ctx
        if ctx is None:
            self.app.notify_user("115 未就绪", error=True)
            return
        full = self.path.rstrip("/") + "/" + name

        def fill() -> None:
            from cloud115.filesystem import find_entry
            from scripts.manual import entry_fid

            async def go():
                entry = await find_entry(ctx.cloud, full)
                await ctx.cloud.raw.rename_file(entry_fid(entry), new_name)
                ctx.cloud.raw.invalidate_path_cache()
            try:
                self.app._cloud.submit(go()).result(60)
            except Exception as e:  # noqa: BLE001
                _post(self.app, self.app.notify_user, f"重命名失败: {e}", True)
                return
            _post(self.app, self.load_dir)
            _post(self.app, self.app.notify_user, f"✅ {name} → {new_name}")
        self.run_worker(fill, thread=True)

    def _move(self, name: str, dst: str) -> None:
        ctx = self.app_ctx
        if ctx is None:
            self.app.notify_user("115 未就绪", error=True)
            return
        src = self.path.rstrip("/") + "/" + name

        def fill() -> None:
            from cloud115.filesystem import find_entry
            from scripts.manual import entry_fid

            async def go():
                entry = await find_entry(ctx.cloud, src)
                di = await ctx.cloud.raw.get_file_info(dst)
                if di and di.get("file_id") is not None:
                    to_cid = int(di["file_id"])
                else:
                    to_cid = int(await ctx.cloud.raw.create_dir_recursive(dst))
                await ctx.cloud.raw.move_files(entry_fid(entry), to_cid)
                ctx.cloud.raw.invalidate_path_cache()
            try:
                self.app._cloud.submit(go()).result(60)
            except Exception as e:  # noqa: BLE001
                _post(self.app, self.app.notify_user, f"移动失败: {e}", True)
                return
            _post(self.app, self.load_dir)
            _post(self.app, self.app.notify_user, f"✅ {name} → {dst}")
        self.run_worker(fill, thread=True)

    def _mkdir(self, name: str) -> None:
        if "/" in name.strip():
            self.app.notify_user("目录名不含 /（逐级进入再建即可）", True)
            return
        ctx = self.app_ctx
        if ctx is None:
            self.app.notify_user("115 未就绪", error=True)
            return
        target = self.path.rstrip("/") + "/" + name.strip()

        def fill() -> None:
            from cloud115.filesystem import mkdir_p

            async def go():
                await mkdir_p(ctx.cloud, target)
                ctx.cloud.raw.invalidate_path_cache()
            try:
                self.app._cloud.submit(go()).result(60)
            except Exception as e:  # noqa: BLE001
                _post(self.app, self.app.notify_user, f"新建失败: {e}", True)
                return
            _post(self.app, self.load_dir)
            _post(self.app, self.app.notify_user, f"✅ 已创建 {target}")
        self.run_worker(fill, thread=True)

    def _try_delete(self) -> None:
        ctx = self.app_ctx
        t = self.query_one("#files", DataTable)
        if ctx is None or t.cursor_row is None or t.cursor_row < 0:
            return
        try:
            row = t.get_row_at(t.cursor_row)
        except Exception:  # noqa: BLE001
            return
        name = str(row[1])
        if name == "..":
            return
        if self._confirm_key == name:
            self._confirm_key = None

            def fill() -> None:
                from cloud115.filesystem import find_entry
                from scripts.manual import entry_fid

                async def go():
                    entry = await find_entry(ctx.cloud, self.path.rstrip("/") + "/" + name)
                    await ctx.cloud.raw.delete_files([entry_fid(entry)])
                    ctx.cloud.raw.invalidate_path_cache()
                self.app._cloud.submit(go()).result(60)
                _post(self.app, self.load_dir)
            self.run_worker(fill, thread=True)
            self.app.notify_user(f"🗑 已删除 {name}（回收站可恢复）")
        else:
            self._confirm_key = name
            self.app.notify_user(f"再按一次 d 确认删除: {name}")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "action-input":
            raw = event.value.strip()
            pending, self._pending = self._pending, None
            self._cancel_ask()
            if not raw or not pending:
                return
            action, name = pending
            if action == "download":
                self._download(name, Path(raw).expanduser())
            elif action == "rename":
                self._rename(name, raw)
            elif action == "move":
                self._move(name, raw)
            elif action == "mkdir":
                self._mkdir(raw)
            return
        if event.input.id != "upload-input":
            return
        src = event.value.strip()
        if not src:
            return
        event.input.value = ""
        ctx = self.app_ctx
        if ctx is None:
            self.app.notify_user("115 未就绪", error=True)
            return

        def up() -> None:
            from core.uploader import upload_to_dir
            from scripts import manual

            async def go():
                files, bases, missing = manual.expand_sources([src])
                for m in missing:
                    _post(self.app, self.app.notify_user, f"❌ {m}", True)
                for f in files:
                    rel = f.relative_to(bases[f]).parent
                    remote = (self.path.rstrip("/")
                              + ("/" + rel.as_posix() if str(rel) != "." else ""))
                    size, sha1 = await manual.sha1_of(f)
                    result = await upload_to_dir(ctx.cloud, f, size, sha1, remote, f.name,
                                                 oss_concurrency=8)
                    _post(self.app, 
                        self.app.notify_user, f"✅ {f.name} ({result.method})")
            try:
                self.app._cloud.submit(go()).result()
            except Exception as e:  # noqa: BLE001
                _post(self.app, self.app.notify_user, f"上传失败: {e}", True)
        self.run_worker(up, thread=True)
        self.app.notify_user(f"开始上传: {src}")


# ── 离线任务 ────────────────────────────────────────────────────────────

class OfflinePage(Page):
    def compose(self) -> ComposeResult:
        yield Label("115 离线任务（30s 自动刷新）", classes="page-title")
        yield DataTable(id="off-t", cursor_type="row", zebra_stripes=True)
        yield Input(placeholder="添加：粘贴 magnet/ed2k/直链 后回车（保存到 upload.target_dir）",
                    id="off-add")
        yield Label("r=刷新 · d=删除选中任务（连文件）", classes="hint")

    def on_mount(self) -> None:
        t = self.query_one("#off-t", DataTable)
        t.add_columns("状态", "名称", "进度", "info_hash")
        t.focus()          # 键盘优先：挂载即聚焦表格，r/d 立即可用
        self.every(30, self.refresh_list)
        self.refresh_list()

    def refresh_list(self) -> None:
        ctx = self.app_ctx
        if ctx is None:
            return

        def fill() -> None:
            from scripts.manual import OFFLINE_ICON

            async def go():
                tasks = await ctx.cloud.raw.offline_list_all()
                return [(OFFLINE_ICON.get(tk.get("status"), "•"),
                         tk.get("name") or (tk.get("url") or "?")[:50],
                         f"{tk.get('percentDone', 0)}%" if tk.get("status") == 1 else "",
                         str(tk.get("info_hash") or "")[:12])
                        for tk in tasks]

            def apply(rows) -> None:
                t = self.query_one("#off-t", DataTable)
                t.clear()
                for r in rows:
                    t.add_row(*r)
            try:
                rows = self.app._cloud.submit(go()).result(120)
            except Exception as e:  # noqa: BLE001
                _post(self.app, self.app.notify_user, f"离线列表加载失败: {e}", True)
                return
            _post(self.app, apply, rows)
        self.run_worker(fill, thread=True)

    def on_key(self, event) -> None:  # noqa: BLE001
        if event.key == "r":
            self.refresh_list()
        elif event.key == "d":
            self._delete_selected()

    def _delete_selected(self) -> None:
        ctx = self.app_ctx
        t = self.query_one("#off-t", DataTable)
        if ctx is None or t.cursor_row is None or t.cursor_row < 0:
            return
        try:
            row = t.get_row_at(t.cursor_row)
        except Exception:  # noqa: BLE001
            return
        ih = str(row[3])
        if not ih:
            return

        def fill() -> None:
            async def go():
                await ctx.cloud.raw.offline_del(ih, del_source_file=1)
            self.app._cloud.submit(go()).result(60)
            _post(self.app, self.refresh_list)
        self.run_worker(fill, thread=True)
        self.app.notify_user(f"🗑 已删离线任务 {ih}…")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "off-add":
            return
        url = event.value.strip()
        if not url:
            return
        event.input.value = ""
        ctx = self.app_ctx
        if ctx is None:
            self.app.notify_user("115 未就绪", error=True)
            return
        save = ctx.cfg.upload.target_dir

        def fill() -> None:
            async def go():
                await ctx.cloud.raw.offline_add(url, save)
            self.app._cloud.submit(go()).result(60)
            _post(self.app, self.refresh_list)
        self.run_worker(fill, thread=True)
        self.app.notify_user("已提交离线任务")


# ── 日志尾随 ────────────────────────────────────────────────────────────

class LogPage(Page):
    def __init__(self) -> None:
        super().__init__()
        self._offset = 0

    def compose(self) -> ComposeResult:
        yield Label(f"日志（{service.STDOUT_LOG}）", classes="page-title")
        yield RichLog(id="log", highlight=False, markup=False, wrap=False)

    def on_mount(self) -> None:
        log = self.query_one("#log", RichLog)
        try:
            data = service.STDOUT_LOG.read_bytes()
            self._offset = len(data)
            for line in data.decode("utf-8", errors="replace").splitlines()[-50:]:
                log.write(line)
        except OSError:
            log.write("（暂无日志）")
        self.every(1, self._poll)

    def _poll(self) -> None:
        try:
            size = service.STDOUT_LOG.stat().st_size
            if size < self._offset:      # 轮转/清空 → 从头
                self._offset = 0
            if size == self._offset:
                return
            with open(service.STDOUT_LOG, "rb") as f:
                f.seek(self._offset)
                chunk = f.read()
                self._offset += len(chunk)
            self.query_one("#log", RichLog).write(
                chunk.decode("utf-8", errors="replace").rstrip("\n"))
        except OSError:
            pass


# ── 配置页（参数编辑 + 115 授权子功能） ─────────────────────────────────

class ConfigPage(Page):
    def __init__(self) -> None:
        super().__init__()
        from tb import ops
        self.cfg_path = ops.CONFIG_FILE     # 测试可重定向到临时文件

    def compose(self) -> ComposeResult:
        yield Label("配置", classes="page-title")
        with TabbedContent():
            with TabPane("参数"):
                with Horizontal(id="cfg-switches"):
                    yield Switch(value=False, id="sw-web")
                    yield Label("Web 台")
                    yield Switch(value=False, id="sw-keep")
                    yield Label("本地副本")
                    yield Switch(value=False, id="sw-chan")
                    yield Label("频道监控")
                yield TextArea("", id="cfg-text", show_line_numbers=True)
                with Horizontal(id="cfg-btns"):
                    yield Button("保存并校验", id="cfg-save", variant="primary")
                    yield Button("重新加载", id="cfg-reload")
                    yield Button("重启服务", id="cfg-restart", variant="warning")
                yield Static("", id="cfg-status")
                yield Label("保存为全文写回（自动备份 config.yaml.bak.<时间戳>）；"
                            "大部分参数需重启服务生效", classes="hint")
            with TabPane("115 授权"):
                yield AuthSection()

    def on_mount(self) -> None:
        self._reload()
        # 开关初值来自当前配置
        try:
            from scripts import manual
            cfg = manual.load_config()
            self.query_one("#sw-web", Switch).value = bool(cfg.web.enable)
            self.query_one("#sw-keep", Switch).value = bool(cfg.storage.keep_local)
            self.query_one("#sw-chan", Switch).value = bool(cfg.channel_monitor.enabled)
        except Exception as e:  # noqa: BLE001
            self._status(f"⚠️ 配置读取失败: {e}")

    def _status(self, text: str, error: bool = False) -> None:
        s = self.query_one("#cfg-status", Static)
        s.update(("❌ " if error else "") + text)

    def _reload(self) -> None:
        try:
            self.query_one("#cfg-text", TextArea).text = \
                self.cfg_path.read_text(encoding="utf-8")
            self._status("已加载（未修改）")
        except OSError as e:
            self._status(f"读取失败: {e}（先 tb init）", error=True)

    def on_switch_changed(self, event: Switch.Changed) -> None:
        mapping = {"sw-web": ("web.enable", "Web 台"),
                   "sw-keep": ("storage.keep_local", "本地副本"),
                   "sw-chan": ("channel_monitor.enabled", "频道监控")}
        if event.switch.id not in mapping:
            return
        key, label = mapping[event.switch.id]
        from tb import ops
        ok, msg = ops.set_config_key(key, event.value, self.cfg_path)
        if ok:
            self._status(f"✅ {label} = {'开' if event.value else '关'}（已写盘，重启生效）")
            self._reload()      # 开关走 yaml 往返，编辑器同步磁盘最新
        else:
            self._status(f"{label} 修改失败: {msg}", error=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "cfg-save":
            from tb import ops
            text = self.query_one("#cfg-text", TextArea).text
            ok, msg = ops.validate_config_text(text)
            if not ok:
                self._status(f"未保存——{msg}", error=True)
                return
            try:
                ops.write_config_text(text, self.cfg_path)
                self._status("✅ 已保存并通过校验（大部分参数重启服务后生效）")
            except OSError as e:
                self._status(f"写盘失败: {e}", error=True)
        elif bid == "cfg-reload":
            self._reload()
        elif bid == "cfg-restart":
            btn = event.button

            def run() -> None:
                rc = service.do_restart()
                _post(self.app, 
                    self.app.notify_user,
                    "重启完成" if rc == 0 else f"重启失败（exit {rc}）", error=bool(rc))

                def enable() -> None:
                    btn.disabled = False
                _post(self.app, enable)
            btn.disabled = True
            self.run_worker(run, thread=True, group="svc", exclusive=True)


class AuthSection(Vertical):
    """115 扫码授权（原独立授权页，现为配置页的子 Tab）。"""

    def compose(self) -> ComposeResult:
        yield Label("115 扫码授权", classes="page-title")
        yield Static("", id="auth-state")
        yield Button("生成二维码（强刷 token）", id="auth-btn", variant="primary")
        yield Static("点击按钮开始。用 115 APP 扫码；深色背景扫不动时 QR_INVERT=0 重进。",
                     id="auth-qr")

    def on_mount(self) -> None:
        try:
            from scripts import manual
            cfg = manual.load_config()
            names = ", ".join(a.name for a in cfg.accounts) or "（config.accounts 为空）"
            self.query_one("#auth-state", Static).update(f"👤 账号: {names}")
        except Exception as e:  # noqa: BLE001
            self.query_one("#auth-state", Static).update(f"⚠️ {e}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "auth-btn":
            return
        ctx = self.app.ctx
        static = self.query_one("#auth-qr", Static)
        if ctx is None:
            static.update(f"❌ {self.app.ctx_error or '初始化中…'}（先 tb init）")
            return
        event.button.disabled = True

        def flow() -> None:
            async def go():
                api = ctx.cloud.raw
                qr = await api.start_qr_auth()
                _post(self.app, static.update,
                                          _qr_ascii(qr["qrcode"]) + "\n请用 115 APP 扫描…")
                await asyncio.sleep(5)
                while True:
                    st = await api.poll_qr_status(qr["uid"], qr["time"], qr["sign"])
                    if st == 2:
                        await api.exchange_qr_token(qr["uid"], qr["verifier"])
                        _post(self.app, static.update, "✅ 授权成功，token 已保存")
                        _post(self.app, self.app.notify_user, "115 授权成功 ✅")
                        return
                    if st == -1:
                        _post(self.app, static.update, "❌ 二维码已过期，点按钮重新生成")
                        return
                    if st == -2:
                        _post(self.app, static.update, "❌ 你在 APP 里取消了授权")
                        return
                    await asyncio.sleep(3)
            try:
                self.app._cloud.submit(go()).result()
            except Exception as e:  # noqa: BLE001
                _post(self.app, static.update, f"❌ 授权失败: {e}")
            finally:
                def enable() -> None:
                    self.query_one("#auth-btn", Button).disabled = False
                _post(self.app, enable)
        self.run_worker(flow, thread=True)


def run(account: str = "") -> int:
    """TUI 入口（tb 裸调用）。"""
    try:
        TBApp(account).run()
        return 0
    except Exception as e:  # noqa: BLE001 -- TUI 环境异常给出明确出口
        print(f"TUI 运行失败: {e}（TB_TUI=0 可用菜单模式）")
        return 1
