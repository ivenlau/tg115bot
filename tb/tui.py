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
                             ListView, ListItem, RichLog, Static)

from tb import service

PAGES = ["仪表盘", "文件", "离线任务", "日志", "授权"]


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
        self._thread = threading.Thread(target=self._run, name="tb-cloud", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()
        self.loop.close()

    def submit(self, coro) -> Future:
        self._ready.wait(5)
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)


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
    DataTable { height: 1fr; }
    RichLog { height: 1fr; }
    """

    BINDINGS = [("q", "quit", "退出")]
    ctx = None            # manual.Ctx（后台就绪后填充）
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
        self.run_worker(self._build_ctx, thread=True, exclusive=True)
        self.query_one("#nav", ListView).index = 0   # 触发 Highlighted -> 挂载首页

    def _build_ctx(self) -> None:
        """线程 worker：在常驻 115 循环上建上下文（need_login=False，未授权可去授权页扫码）。"""
        from scripts import manual

        try:
            cfg = manual.load_config()
        except Exception as e:  # noqa: BLE001
            self.ctx_error = f"配置加载失败: {e}（tb init）"
            self._side_status(self.ctx_error)
            return

        try:
            self.ctx = self._cloud.submit(
                manual.build_ctx(cfg, self.account, need_login=False)).result(30)
        except Exception as e:  # noqa: BLE001
            self.ctx_error = str(e)
            self._side_status(self.ctx_error)
            return
        self._side_status(f"账号 {self.ctx.account.name}")

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
        page_cls = [DashboardPage, FilesPage, OfflinePage, LogPage, AuthPage][idx]

        async def _swap() -> None:
            await content.remove_children()
            await content.mount(page_cls())
        self.call_after_refresh(_swap)

    def notify_user(self, msg: str, error: bool = False) -> None:
        self.notify(msg, severity="error" if error else "information")

    def on_unmount(self) -> None:
        """退出：在常驻循环上关 115 会话，然后停循环。"""
        ctx, self.ctx = self.ctx, None
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

class DashboardPage(Page):
    def compose(self) -> ComposeResult:
        yield Label("仪表盘", classes="page-title")
        yield Static("加载中…", id="dash")

    def on_mount(self) -> None:
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
                lines.append(f"● 服务运行中   PID {pid}   内存 {mb:.0f}MB   已运行 {up_s}")
            except Exception:  # noqa: BLE001
                lines.append(f"● 服务运行中   PID {pid}")
        else:
            lines.append("○ 服务未运行（tb start 或侧栏外终端启动）")
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
                        lines.append(f"☁️ 115          未授权或不可达（{e}）→ 去「授权」页扫码")
                self.app._cloud.submit(go()).result(60)
                self.app.call_from_thread(
                    self.query_one("#dash", Static).update, "\n".join(lines))
            self.run_worker(fill, thread=True)


# ── 文件浏览 ────────────────────────────────────────────────────────────

class FilesPage(Page):
    def __init__(self) -> None:
        super().__init__()
        self.path = "/tg115bot"
        self._confirm_key: str | None = None

    def compose(self) -> ComposeResult:
        yield Label("文件（115 网盘）", classes="page-title")
        yield Static(self.path, id="files-path")
        yield DataTable(id="files", cursor_type="row", zebra_stripes=True)
        yield Input(placeholder="上传：输入本地文件/目录/通配符路径后回车（如 /data/photos 或 D:\\p*.jpg）",
                    id="upload-input")
        yield Label("回车=进入目录 · d=删除(两次确认) · r=刷新 · 底部输入框=上传", classes="hint")

    def on_mount(self) -> None:
        t = self.query_one("#files", DataTable)
        t.add_columns("类型", "名称", "大小")
        self.load_dir()

    def load_dir(self) -> None:
        ctx = self.app_ctx
        if ctx is None:
            self.query_one("#files", DataTable).add_row("⚠️", self.app.ctx_error or "初始化中…", "")
            return

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
                t = self.query_one("#files", DataTable)
                t.clear()
                t.add_row("⚠️", msg, "")
            try:
                rows = self.app._cloud.submit(go()).result(60)
            except Exception as e:  # noqa: BLE001 -- 网络/授权问题不炸 worker
                self.app.call_from_thread(err, f"加载失败: {e}")
                return

            def apply() -> None:
                t = self.query_one("#files", DataTable)
                t.clear()
                t.add_row("📁", "..", "")
                for r in rows:
                    t.add_row(*r, key=r[1])
                self.query_one("#files-path", Static).update(self.path)
            self.app.call_from_thread(apply)
        self.run_worker(fill, thread=True)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        name = event.row_key.value
        row = self.query_one("#files", DataTable).get_row(event.row_key)
        if row and str(row[0]).startswith("📂"):
            self.path = (self.path.rstrip("/") + "/" + name if name != ".."
                         else str(Path(self.path).parent))
            self.load_dir()

    def on_key(self, event) -> None:  # noqa: BLE001
        if event.key == "r":
            self.load_dir()
        elif event.key == "d":
            self._try_delete()

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
                self.app.call_from_thread(self.load_dir)
            self.run_worker(fill, thread=True)
            self.app.notify_user(f"🗑 已删除 {name}（回收站可恢复）")
        else:
            self._confirm_key = name
            self.app.notify_user(f"再按一次 d 确认删除: {name}")

    def on_input_submitted(self, event: Input.Submitted) -> None:
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
                    self.app.call_from_thread(self.app.notify_user, f"❌ {m}", True)
                for f in files:
                    rel = f.relative_to(bases[f]).parent
                    remote = (self.path.rstrip("/")
                              + ("/" + rel.as_posix() if str(rel) != "." else ""))
                    size, sha1 = await manual.sha1_of(f)
                    result = await upload_to_dir(ctx.cloud, f, size, sha1, remote, f.name,
                                                 oss_concurrency=8)
                    self.app.call_from_thread(
                        self.app.notify_user, f"✅ {f.name} ({result.method})")
            try:
                self.app._cloud.submit(go()).result()
            except Exception as e:  # noqa: BLE001
                self.app.call_from_thread(self.app.notify_user, f"上传失败: {e}", True)
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
                self.app.call_from_thread(self.app.notify_user, f"离线列表加载失败: {e}", True)
                return
            self.app.call_from_thread(apply, rows)
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
            self.app.call_from_thread(self.refresh_list)
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
            self.app.call_from_thread(self.refresh_list)
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


# ── 扫码授权 ────────────────────────────────────────────────────────────

class AuthPage(Page):
    def compose(self) -> ComposeResult:
        yield Label("115 扫码授权", classes="page-title")
        yield Button("生成二维码（强刷 token）", id="auth-btn", variant="primary")
        yield Static("点击按钮开始。用 115 APP 扫码；深色背景扫不动时 QR_INVERT=0 重进。",
                     id="auth-qr")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "auth-btn":
            return
        ctx = self.app_ctx
        static = self.query_one("#auth-qr", Static)
        if ctx is None:
            static.update(f"❌ {self.app.ctx_error or '初始化中…'}（先 tb init）")
            return
        event.button.disabled = True

        def flow() -> None:
            async def go():
                api = ctx.cloud.raw
                qr = await api.start_qr_auth()
                self.app.call_from_thread(static.update,
                                          _qr_ascii(qr["qrcode"]) + "\n请用 115 APP 扫描…")
                await asyncio.sleep(5)
                while True:
                    st = await api.poll_qr_status(qr["uid"], qr["time"], qr["sign"])
                    if st == 2:
                        await api.exchange_qr_token(qr["uid"], qr["verifier"])
                        self.app.call_from_thread(static.update, "✅ 授权成功，token 已保存")
                        self.app.call_from_thread(self.app.notify_user, "115 授权成功 ✅")
                        return
                    if st == -1:
                        self.app.call_from_thread(static.update, "❌ 二维码已过期，点按钮重新生成")
                        return
                    if st == -2:
                        self.app.call_from_thread(static.update, "❌ 你在 APP 里取消了授权")
                        return
                    await asyncio.sleep(3)
            try:
                self.app._cloud.submit(go()).result()
            except Exception as e:  # noqa: BLE001
                self.app.call_from_thread(static.update, f"❌ 授权失败: {e}")
            finally:
                def enable() -> None:
                    self.query_one("#auth-btn", Button).disabled = False
                self.app.call_from_thread(enable)
        self.run_worker(flow, thread=True)


def run(account: str = "") -> int:
    """TUI 入口（tb 裸调用）。"""
    try:
        TBApp(account).run()
        return 0
    except Exception as e:  # noqa: BLE001 -- TUI 环境异常给出明确出口
        print(f"TUI 运行失败: {e}（TB_TUI=0 可用菜单模式）")
        return 1
