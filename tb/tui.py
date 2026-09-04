"""Textual TUI——裸 `tb` 的交互模式（P2）。

五个页面：仪表盘 / 文件 / 离线任务 / 日志 / 授权。侧栏导航；标题栏右侧退出按钮，
底部状态栏时钟（q 快捷键仍可退出）。
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

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Header, Input, Label,
                             ListView, ListItem, RichLog, Static, Switch,
                             TabbedContent, TabPane, TextArea)

from tb import service

# 图标只用 East Asian Width=W 的单码位字符（rich 与终端一致按 2 格渲染）：
# ⚙️/☁️/⬇️ 这类默认文本呈现的码位（EAW=N，靠 VS16 才成 emoji）在部分
# 终端按 1 格渲染，菜单/表格列的文字会错位一格
NAV_ITEMS = [("📊", "仪表盘"), ("📁", "文件"), ("⏬", "离线任务"),
             ("🔧", "配置"), ("📜", "日志")]


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
    HeaderClockSpace { display: none; }   /* 时钟移到状态栏，去掉头部右侧占位 */
    #btn-exit { dock: right; height: 1; width: auto; min-width: 4; padding: 0 1;
                background: transparent; border: none; color: $text-muted; }
    #btn-exit:hover { background: $error; color: $text; }
    #statusbar { dock: bottom; height: 1; background: $footer-background;
                 color: $footer-key-foreground; padding: 0 1; }
    #sidebar { width: 26; border-right: solid $primary-darken-1; background: $surface; padding: 1; height: 1fr; }
    #sidebar ListView { height: auto; }
    #nav > ListItem { color: $text-muted; padding: 0 1; margin-bottom: 1; background: transparent; border-left: tall transparent; }
    #nav > ListItem.-highlight { color: $text; text-style: bold; background: $panel; border-left: tall $accent; }
    #side-status { dock: bottom; color: $text-muted; padding: 0 1; background: $panel; border: round $primary-darken-1; }
    #content { padding: 1 2; height: 1fr; }
    .page-title { text-style: bold; color: $text; margin-bottom: 1; }
    .hint { color: $text-muted; margin-top: 1; }
    .hidden { display: none; }
    #dash-cards { layout: grid; grid-size: 2 2; grid-gutter: 0 1; height: auto; margin-bottom: 1; }
    .dash-card { width: 1fr; height: auto; min-height: 5; padding: 0 1; background: $panel; border: round $primary-darken-1; }
    #card-svc.ok { border: round $success; }
    #card-svc.down { border: round $error; }
    #card-disk { border: round yellow; }
    #card-disk.full { border: round $error; }
    #card-cloud { border: round cyan; }
    #card-api { border: round magenta; }
    #svc-btns { height: auto; margin: 1 0; }
    #svc-btns Button { margin-right: 1; }
    #task-ops { height: auto; margin: 1 0 0 0; }
    #task-ops .hint { width: 1fr; margin-top: 0; }
    #task-ops Button { margin-left: 1; }
    #cfg-switches { height: auto; margin: 0 0 1 0; }
    #cfg-switches Switch { margin: 0 1 0 0; }
    #cfg-switches Label { margin: 0 2 0 0; }
    #cfg-text { height: 1fr; }
    #cfg-btns { height: auto; margin: 1 0; }
    #cfg-btns Button { margin-right: 1; }
    #dl-status { color: $text-muted; }
    #file-ops { height: auto; margin: 1 0; }
    #file-ops Button { margin-right: 1; }
    /* 下载/移动无对应语义 variant，自定义色模仿 variant 的上下边框结构 */
    #op-dl { background: #0f766e; border-top: tall #14b8a6; border-bottom: tall #115e59;
             color: $button-color-foreground; }
    #op-dl:hover { background: #115e59; border-top: tall #0f766e; border-bottom: tall #134e4a; }
    #op-move { background: #6d28d9; border-top: tall #8b5cf6; border-bottom: tall #5b21b6;
               color: $button-color-foreground; }
    #op-move:hover { background: #5b21b6; border-top: tall #6d28d9; border-bottom: tall #4c1d95; }
    #upload-row { height: auto; margin: 0 0 1 0; }
    #upload-row Input { width: 1fr; }
    #upload-row Button { margin-left: 1; }
    #off-ops { height: auto; margin: 1 0; }
    #off-ops Button { margin-right: 1; }
    #off-add-row { height: auto; margin: 0 0 1 0; }
    #off-add-row Input { width: 1fr; }
    #off-add-row Button { margin-left: 1; }
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
        yield Header(icon="📦", show_clock=False)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield ListView(*[ListItem(Label(f"{icon} {name}"))
                                 for icon, name in NAV_ITEMS], id="nav")
                yield Static("初始化中…", id="side-status")
            yield Container(id="content")
        yield Static(time.strftime("%H:%M:%S"), id="statusbar")

    def on_mount(self) -> None:
        # 初始化整体投给常驻循环 fire-and-forget：退出时可被真取消，不占线程
        # worker——消除「退出时 worker 卡 .result 拖死进程」的一整类问题
        self._cloud.submit(self._init_all())
        self.query_one("#nav", ListView).index = 0   # 触发 Highlighted -> 挂载首页
        self.set_interval(5, self._refresh_side)     # 侧栏底部小卡（账号/服务点）
        self._refresh_side()
        self.set_interval(1, self._tick)             # 状态栏时钟（到秒）
        self.call_after_refresh(self._mount_exit_btn)

    async def _mount_exit_btn(self) -> None:
        """标题栏右上退出按钮（HeaderIcon/HeaderTitle 非公开 API，只能注入挂载）。"""
        await self.query_one(Header).mount(Button("❌", id="btn-exit"))

    def _tick(self) -> None:
        try:
            self.query_one("#statusbar", Static).update(time.strftime("%H:%M:%S"))
        except Exception:  # noqa: BLE001 -- 页面未挂载/退出竞态时忽略
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-exit":
            self.exit()

    def deliver_screenshot(self, filename=None, path=None, time_format=None):
        """命令面板截图：默认写到 ~/Downloads，无桌面服务器该目录常不存在，
        Textual 的 open() 不建父目录必失败——先建目录，建不起来退回当前目录。"""
        if path is None:
            try:
                from platformdirs import user_downloads_path
                user_downloads_path().mkdir(parents=True, exist_ok=True)
            except OSError:
                path = "."
        return super().deliver_screenshot(filename, path, time_format)

    async def _init_all(self) -> None:
        """建 115 上下文 + DB 句柄（need_login=False，未授权可去配置页扫码）。"""
        from scripts import manual

        try:
            cfg = manual.load_config()
        except Exception as e:  # noqa: BLE001
            self.ctx_error = f"配置加载失败: {e}（tb init）"
            _post(self, self._refresh_side)
            return

        try:
            ctx = await manual.build_ctx(cfg, self.account, need_login=False)
        except Exception as e:  # noqa: BLE001
            self.ctx_error = str(e)
            _post(self, self._refresh_side)
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
        _post(self, self._refresh_side)

    def _side_status(self, text: str) -> None:
        try:
            self.query_one("#side-status", Static).update(text)
        except Exception:  # noqa: BLE001 -- 页面未挂载时忽略
            pass

    def _refresh_side(self) -> None:
        """侧栏底部小卡：账号 + 服务状态圆点（5s 刷新，与仪表盘同节奏）。"""
        if self.ctx is not None:
            acct = f"👤 {_plain(self.ctx.account.name)}"
        elif self.ctx_error:
            acct = f"❗ {_plain(self.ctx_error)[:36]}"
        else:
            acct = "⏳ 初始化中…"
        running = service.live_pid() is not None
        self._side_status(f"{acct}\n{'⏩ 服务运行中' if running else '🛑 服务未运行'}")

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

TASK_ICON = {"queued": "⏳", "downloading": "📥", "uploading": "📤",
             "done": "✅", "failed": "❌", "cancelled": "🚫"}
TASK_LABEL = {"queued": "排队中", "downloading": "下载中", "uploading": "上传中",
              "done": "完成", "failed": "失败", "cancelled": "已取消"}
TASK_STYLE = {"queued": "yellow", "downloading": "cyan", "uploading": "magenta",
              "done": "green", "failed": "red", "cancelled": "dim"}


def _pct_bar(pct: float, width: int = 10) -> str:
    """文本进度条（DataTable 单元格用；着色交给 Text style）。"""
    filled = max(0, min(width, round(pct / 100 * width)))
    return "▓" * filled + "░" * (width - filled)


def _plain(s) -> str:
    """异常文本进 markup 卡片前去方括号：Textual 对未知标签按字面渲染，
    未闭合的 `[x` 会吞掉后续真实的样式闭合标签（escape 只覆盖 [a-z 前缀）。"""
    return str(s).replace("[", "(").replace("]", ")")


# ── 模态弹窗（危险操作确认 / 信息展示） ────────────────────────────────

class _DialogScreen(ModalScreen):
    """居中弹窗骨架：底屏自动压暗，Esc 关闭。"""

    BINDINGS = [("escape", "dismiss_screen", "关闭")]

    DEFAULT_CSS = """
    _DialogScreen { align: center middle; }
    #dlg { width: auto; max-width: 76; height: auto; background: $surface;
           border: round $accent; padding: 1 2; }
    .dlg-title { text-style: bold; color: $text; margin-bottom: 1; }
    .dlg-msg { margin-bottom: 1; }
    #dlg-input { width: 64; }
    #dlg-err { height: auto; }
    .dlg-btns { height: auto; align-horizontal: right; }
    .dlg-btns Button { margin-left: 1; }
    """

    def action_dismiss_screen(self) -> None:
        self.dismiss(None)


class ConfirmModal(_DialogScreen):
    """危险操作确认：dismiss(True)=确认 / dismiss(False)=取消（Esc=取消）。"""

    def __init__(self, title: str, message: str, danger: bool = True) -> None:
        super().__init__()
        self.dlg_title, self.message, self.danger = title, message, danger

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            yield Label(self.dlg_title, classes="dlg-title")
            yield Static(self.message, classes="dlg-msg")
            with Horizontal(classes="dlg-btns"):
                yield Button("取消", id="dlg-cancel", variant="default")
                yield Button("确认", id="dlg-ok",
                             variant="error" if self.danger else "primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "dlg-ok")


class InfoModal(_DialogScreen):
    """信息展示弹窗（诊断结果等）。"""

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.dlg_title, self.body = title, body

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            yield Label(self.dlg_title, classes="dlg-title")
            yield Static(self.body, classes="dlg-msg")
            with Horizontal(classes="dlg-btns"):
                yield Button("关闭", id="dlg-close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class PromptModal(_DialogScreen):
    """输入型弹窗：Enter/确认=dismiss(输入值)；Esc/取消=dismiss(None)。

    validator(值) -> 错误文案 | None：返回文案时弹窗内红字提示、不关闭。
    """

    def __init__(self, title: str, message: str = "", value: str = "",
                 placeholder: str = "", validator=None) -> None:
        super().__init__()
        self.dlg_title = title
        self.message = message
        self.init_value = value
        self.placeholder = placeholder
        self.validator = validator

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            yield Label(self.dlg_title, classes="dlg-title")
            if self.message:
                yield Static(self.message, classes="dlg-msg")
            yield Input(value=self.init_value, placeholder=self.placeholder,
                        id="dlg-input")
            yield Static("", id="dlg-err")
            with Horizontal(classes="dlg-btns"):
                yield Button("取消", id="dlg-cancel", variant="default")
                yield Button("确认", id="dlg-ok", variant="primary")

    def on_mount(self) -> None:
        inp = self.query_one("#dlg-input", Input)
        inp.focus()
        if self.init_value:
            inp.action_select_all()   # 预填全选：直接打字即覆盖

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dlg-ok":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        val = self.query_one("#dlg-input", Input).value.strip()
        err = self.validator(val) if self.validator else None
        if err:
            self.query_one("#dlg-err", Static).update(f"[red]{err}[/red]")
            return
        self.dismiss(val)


class DashboardPage(Page):
    # 与任务表行对齐的元数据 [(task_id, status, filename)]，随 _refresh_tasks 更新
    _task_meta: list = []

    def compose(self) -> ComposeResult:
        yield Label("📊 仪表盘", classes="page-title")
        with Container(id="dash-cards"):
            yield Static("", id="card-svc", classes="dash-card")
            yield Static("", id="card-disk", classes="dash-card")
            yield Static("", id="card-cloud", classes="dash-card")
            yield Static("", id="card-api", classes="dash-card")
        with Horizontal(id="svc-btns"):
            yield Button("启动 (s)", id="btn-start", variant="success")
            yield Button("停止 (t)", id="btn-stop", variant="error")
            yield Button("重启 (r)", id="btn-restart", variant="warning")
            yield Button("诊断 (d)", id="btn-doctor")
        with Horizontal(id="task-ops"):
            yield Label("最近任务（bot 侧：TG 上传/频道监控/备份/直链；5s 刷新）",
                        classes="hint")
            yield Button("删记录 (x)", id="op-task-del", variant="error")
        yield DataTable(id="tasks", zebra_stripes=True)

    def on_mount(self) -> None:
        t = self.query_one("#tasks", DataTable)
        t.add_columns("时间", "文件名", "大小", "状态", "进度", "方式")
        self.every(5, self.refresh_dash)
        self.refresh_dash()
        # 页面内要有焦点，快捷键才能冒泡到页面（否则进了侧栏导航）
        self.call_after_refresh(self.query_one("#btn-start", Button).focus)

    def refresh_dash(self) -> None:
        self._card_service()
        self._card_disk()
        self._card_cloud()
        self._refresh_tasks()

    # ── 指标卡（标题行 dim，主值 bold，细节行 dim） ──────────────────────

    def _card_service(self) -> None:
        card = self.query_one("#card-svc", Static)
        pid = service.live_pid()
        if not pid:
            card.remove_class("ok").add_class("down")
            card.update("[dim]服务[/dim]\n🛑 [bold red]未运行[/bold red]"
                        "\n[dim]点下方「启动」[/dim]")
            return
        value = f"⏩ [bold green]运行中[/bold green]  PID {pid}"
        detail = ""
        try:
            import psutil
            p = psutil.Process(pid)
            mb = p.memory_info().rss / 1048576
            up = time.time() - p.create_time()
            d, rem = divmod(int(up), 86400)
            # 到分钟 + PID 挪到主值行：三行文案在 2×2 窄卡片里都不换行，卡等高
            up_s = f"{d}d{rem // 3600:02d}:{rem % 3600 // 60:02d}"
            detail = f"{mb:.0f}MB · 已运行 {up_s}"
        except Exception:  # noqa: BLE001 -- 进程刚退出等瞬时态：只显示 PID
            pass
        card.remove_class("down").add_class("ok")
        body = f"[dim]服务[/dim]\n{value}"
        if detail:
            body += f"\n[dim]{detail}[/dim]"
        card.update(body)

    def _card_disk(self) -> None:
        card = self.query_one("#card-disk", Static)
        try:
            du = shutil.disk_usage(str(service.INSTALL_DIR))
        except OSError as e:
            card.set_class(False, "full")
            card.update(f"[dim]磁盘[/dim]\n❗ [yellow]不可用[/yellow]"
                        f"\n[dim]{_plain(str(e))}[/dim]")
            return
        from core.progress import human_bytes
        used, total = du.used, du.total
        pct = used * 100 / total if total else 0.0
        # 与 115 卡同构的用量条；80/90 阈值红黄绿，≥90 边框同步变红（.full）
        color = "red" if pct >= 90 else "yellow" if pct >= 80 else "green"
        card.set_class(pct >= 90, "full")
        card.update("[dim]磁盘[/dim]"
                    f"\n💾 [bold]{human_bytes(used)}[/bold] / {human_bytes(total)}"
                    f"\n[{color}]{_pct_bar(pct)}[/{color}] [dim]{pct:.0f}% 已用[/dim]")

    def _card_cloud(self) -> None:
        cloud_card = self.query_one("#card-cloud", Static)
        api_card = self.query_one("#card-api", Static)
        ctx = self.app_ctx
        if ctx is None:
            cloud_card.update("[dim]115 空间[/dim]\n📦 [yellow]未就绪[/yellow]"
                              f"\n[dim]{_plain(self.app.ctx_error or '初始化中…')}[/dim]")
            api_card.update("[dim]API 余量[/dim]\n[dim]—[/dim]")
            return

        def fill() -> None:
            async def go():
                sp = await ctx.cloud.raw.user_space()
                q = await ctx.cloud.raw.offline_quota()
                return sp, q
            try:
                sp, q = self.app._cloud.submit(go()).result(60)
            except Exception as e:  # noqa: BLE001
                text = str(e).strip()
                msg = text.splitlines()[0][:60] if text else repr(e)[:60]
                _post(self.app, cloud_card.update,
                      f"[dim]115 空间[/dim]\n📦 [red]不可达[/red]"
                      f"\n[dim]{_plain(msg)} → 配置页扫码[/dim]")
                _post(self.app, api_card.update,
                      "[dim]API 余量[/dim]\n[dim]—（115 不可达）[/dim]")
                return

            def apply() -> None:
                from core.progress import human_bytes
                used, total = sp.get("used", 0), sp.get("total", 0)
                if total:
                    pct = used * 100 / total
                    cloud_card.update(
                        "[dim]115 空间[/dim]"
                        f"\n📦 [bold]{human_bytes(used)}[/bold] / {human_bytes(total)}"
                        f"\n[cyan]{_pct_bar(pct)}[/cyan] [dim]{pct:.1f}%[/dim]")
                else:
                    cloud_card.update("[dim]115 空间[/dim]\n📦 用量未知")
                rc, dl = ctx.cloud.raw.request_count, ctx.cloud.raw.daily_limit
                detail = "今日已用"
                if q:
                    detail += f" · 离线配额 {q.get('used', '?')}/{q.get('count', '?')}"
                api_card.update("[dim]API 余量[/dim]"
                                f"\n🧮 [bold]{rc}[/bold] / {dl}"
                                f"\n[dim]{detail}[/dim]")
            _post(self.app, apply)
        # exclusive：网络黑洞时旧 worker 未超时前不叠新 worker（与 dash-tasks 同策）
        self.run_worker(fill, thread=True, group="dash-cloud", exclusive=True)

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
                self._task_meta = [(r.task_id, r.status, r.filename or "?")
                                   for r in rows]
                if not rows:
                    t.add_row("", "暂无任务——向 bot 发送文件即可开始", "", "", "", "")
                    return
                for r in rows:
                    tm = time.strftime("%m-%d %H:%M", time.localtime(r.created_at or 0))
                    style = TASK_STYLE.get(r.status, "")
                    st = Text(f"{TASK_ICON.get(r.status, '')} {TASK_LABEL.get(r.status, r.status)}",
                              style=style)
                    pct = getattr(r, "progress", -1)   # bot 侧节流落库的实时进度
                    if r.status in ("downloading", "uploading") and 0 <= pct < 100:
                        pg = Text(f"{_pct_bar(pct)} {pct:.0f}%", style=style or "cyan")
                    else:
                        pg = ""
                    via = (r.method or r.source or "").strip()
                    # 文件名用 Text 裸文本：str 单元格会被 from_markup 解析，
                    # 字幕组命名（[Group][1080p].mkv）里未闭合 [x 会吞掉半截名字
                    t.add_row(tm, Text(r.filename or "?"),
                              human_bytes(r.size or 0), st, pg, via)
            _post(self.app, apply)
        self.run_worker(fill, thread=True, group="dash-tasks", exclusive=True)

    # ── 任务记录删除（仅终态：done/failed/cancelled） ──────────────────────

    def op_task_delete(self) -> None:
        db = self.app.db
        if db is None:
            self.app.notify_user("任务数据不可用", True)
            return
        t = self.query_one("#tasks", DataTable)
        if t.cursor_row is None or not 0 <= t.cursor_row < len(self._task_meta):
            self.app.notify_user("先选中一个任务（↑/↓ 移动光标）", True)
            return
        task_id, status, name = self._task_meta[t.cursor_row]
        if status not in ("done", "failed", "cancelled"):
            self.app.notify_user("仅完成/失败/已取消的任务可删除记录", True)
            return
        label = TASK_LABEL.get(status, status)
        self.app.push_screen(
            ConfirmModal("删除任务记录",
                         f"删除 [bold]{_plain(name)}[/bold]（{label}）的记录？\n"
                         "仅清掉这条记录，不影响 115 云端文件。",
                         danger=True),
            lambda ok: ok and self._do_task_delete(task_id))

    def _do_task_delete(self, task_id: str) -> None:
        db = self.app.db
        if db is None:
            return

        def fill() -> None:
            async def go():
                await db.delete_task(task_id)
            try:
                self.app._cloud.submit(go()).result(10)
            except Exception as e:  # noqa: BLE001
                _post(self.app, self.app.notify_user, f"删除失败: {e}", True)
                return
            _post(self.app, self.refresh_dash)
        self.run_worker(fill, thread=True, group="dash-tasks", exclusive=True)

    # ── 服务控制（线程 worker；exclusive 防连点竞态） ─────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "op-task-del":
            self.op_task_delete()
            return
        self._dispatch(event.button.id or "")

    def on_key(self, event) -> None:  # noqa: BLE001
        if event.key == "x":            # 删除任务记录（与按钮同路）
            self.op_task_delete()
            return
        keys = {"s": "btn-start", "t": "btn-stop",
                "r": "btn-restart", "d": "btn-doctor"}
        bid = keys.get(event.key)
        if bid:
            self._dispatch(bid)

    def _dispatch(self, bid: str) -> None:
        if bid == "btn-doctor":
            self._run_doctor()
            return
        # 第三元组 = 确认弹窗文案；None 直接执行（启动无破坏性）
        actions = {"btn-start": ("启动", service.do_start, None),
                   "btn-stop": ("停止", service.do_stop, "服务将停止，bot 不再收发消息。"),
                   "btn-restart": ("重启", service.do_restart, "服务将重启，期间短暂不可用。")}
        if bid not in actions:
            return
        label, fn, confirm = actions[bid]
        if confirm is None:
            self._svc_action(label, fn)
            return

        def cb(ok: bool) -> None:
            if ok:
                self._svc_action(label, fn)
        self.app.push_screen(ConfirmModal(f"{label}服务", confirm, danger=True), cb)

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
        if not enabled:
            return
        try:    # 动作结束恢复页内焦点：禁用期间焦点会被挪走，快捷键将失效
            self.query_one("#btn-start", Button).focus()
        except Exception:  # noqa: BLE001 -- 页面已卸载等竞态
            pass

    def _run_doctor(self) -> None:
        from tb import ops

        def run() -> None:
            checks = ops.doctor_checks()
            lines = [f"  {'✅' if fine else '❌'} {name}: {_plain(detail)}"
                     for name, fine, detail in checks]
            ok = all(c[1] for c in checks)
            lines.append("  结论: " + ("一切正常 ✅" if ok else "存在需要处理的项目 ❌"))
            # 结果走模态弹窗；_plain 防 detail 里的方括号破坏 markup
            _post(self.app, self.app.push_screen,
                  InfoModal("服务诊断", "\n".join(lines)))
        self.run_worker(run, thread=True, group="doctor", exclusive=True)


# ── 文件浏览 ────────────────────────────────────────────────────────────

class FilesPage(Page):
    def __init__(self) -> None:
        super().__init__()
        self.path = "/tg115bot"
        self._load_gen = 0      # 代际号：导航后的过期刷新结果不再落表
        # 搜索结果模式：结果渲染进主表格（多一列 sha1），回车/s 按 pick_code 直下
        self._in_search = False
        self._search_results: list[dict] = []
        self._search_kw = ""

    def compose(self) -> ComposeResult:
        yield Label("📁 文件（115 网盘）", classes="page-title")
        yield Static(self.path, id="files-path")
        yield DataTable(id="files", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="file-ops"):
            yield Button("删除 (d)", id="op-del", variant="error")
            yield Button("下载 (s)", id="op-dl")
            yield Button("重命名 (n)", id="op-rename", variant="warning")
            yield Button("移动 (m)", id="op-move")
            yield Button("新建 (+)", id="op-mkdir", variant="success")
            yield Button("搜索 (f)", id="op-search", variant="primary")
            yield Button("刷新 (r)", id="op-refresh")
        with Horizontal(id="upload-row"):
            yield Input(placeholder="上传：输入本地文件/目录/通配符路径后回车（如 /data/photos 或 D:\\p*.jpg）",
                        id="upload-input")
            yield Button("上传 (u)", id="op-upload", variant="primary", disabled=True)
        yield Static("", id="dl-status", classes="hidden")

    def on_mount(self) -> None:
        t = self.query_one("#files", DataTable)
        t.add_columns("类型", "名称", "大小")
        t.focus()          # 键盘优先：挂载即聚焦表格，r/d/s 立即可用
        self.load_dir()

    def load_dir(self) -> None:
        # 离开搜索模式：复位标记与列（搜索多一列 sha1）；ctx 未就绪也要先复位，
        # 否则 add_row(3 值) 会撞上 4 列表格
        self._in_search = False
        self._search_results = []
        t0 = self.query_one("#files", DataTable)
        if len(t0.columns) != 3:
            t0.clear(columns=True)
            t0.add_columns("类型", "名称", "大小")
        ctx = self.app_ctx
        if ctx is None:
            t0.add_row("❓", self.app.ctx_error or "初始化中…", "")
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
                t.add_row("❌", msg, "")
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
        if not row:
            return
        if self._in_search:
            self._search_row_activated(row)
            return
        if not str(row[0]).startswith("📂"):
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
        # 快捷键与按钮同路；弹窗打开时按键被模态屏拦截，不会误触
        if event.key == "f":
            self.op_search()
            return
        if event.key == "s" and self._in_search:
            # 搜索模式下 s 与回车同路（pick_code 直下），路径式下载不适用
            self._search_activate_current()
            return
        if event.key in ("d", "n", "m", "+", "plus") and self._in_search:
            self._guard_search_mode()
            return
        acts = {"r": self.op_refresh, "d": self.op_delete, "s": self.op_download,
                "n": self.op_rename, "m": self.op_move}
        if event.key in acts:
            acts[event.key]()
        elif event.key in ("+", "plus"):
            self.op_mkdir()
        elif event.key == "u":        # 高频操作：一键聚焦上传输入框
            self.query_one("#upload-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "op-search":
            self.op_search()
            return
        if bid == "op-dl" and self._in_search:
            self._search_activate_current()
            return
        if bid in ("op-del", "op-rename", "op-move", "op-mkdir") and self._in_search:
            self._guard_search_mode()
            return
        ops = {"op-del": self.op_delete, "op-dl": self.op_download,
               "op-rename": self.op_rename, "op-move": self.op_move,
               "op-mkdir": self.op_mkdir, "op-upload": self._submit_upload,
               "op-refresh": self.op_refresh}
        fn = ops.get(bid)
        if fn:
            fn()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "upload-input":
            self.query_one("#op-upload", Button).disabled = not event.value.strip()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "upload-input":
            self._submit_upload()

    def _submit_upload(self) -> None:
        """上传入口（输入框回车 / 上传按钮共用）；提交后清空输入并复位按钮。"""
        inp = self.query_one("#upload-input", Input)
        src = inp.value.strip()
        if not src:
            return
        inp.value = ""                # 触发 Input.Changed -> 按钮自动禁用
        self._do_upload(src)

    # ── 操作入口：按钮/快捷键共用；参数走 PromptModal，删除走 ConfirmModal ──

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

    def _need_ctx(self):
        ctx = self.app_ctx
        if ctx is None:
            self.app.notify_user("115 未就绪", error=True)
        return ctx

    def _need_selected(self) -> tuple[str, str] | None:
        sel = self._selected()
        if sel is None:
            self.app.notify_user("先选中一个条目（↑/↓ 移动光标）", True)
        return sel

    def op_refresh(self) -> None:
        self.load_dir()

    # ── 全盘搜索：结果渲染进主表格（多一列 sha1），回车/s 按 pick_code 直下 ──

    def _guard_search_mode(self) -> None:
        """搜索结果模式下拦截依赖「当前目录」上下文的操作（del/rename/move/mkdir）。"""
        self.app.notify_user("搜索结果模式：回车或 s 下载选中项，r 返回浏览", True)

    def op_search(self) -> None:
        if self._need_ctx() is None:
            return
        self.app.push_screen(
            PromptModal("搜索", "全盘搜索 115 网盘，输入关键词：",
                        placeholder="如 1080p / 文件名片段",
                        validator=lambda v: None if v.strip() else "关键词不能为空"),
            lambda v: v is not None and self._search(v.strip()))

    def _search(self, kw: str) -> None:
        ctx = self.app_ctx
        if ctx is None:
            self.app.notify_user("115 未就绪", error=True)
            return
        self._load_gen += 1
        gen = self._load_gen

        def fill() -> None:
            async def go():
                data = await ctx.cloud.raw.search_files(kw, limit=50)
                return data.get("list") or []
            try:
                items = self.app._cloud.submit(go()).result(60)
            except Exception as e:  # noqa: BLE001
                _post(self.app, self.app.notify_user, f"搜索失败: {e}", True)
                return

            def apply() -> None:
                if gen != self._load_gen:      # 期间已导航/再搜索，丢弃过期结果
                    return
                from core.progress import human_bytes
                self._in_search = True
                self._search_results = list(items)
                self._search_kw = kw
                t = self.query_one("#files", DataTable)
                t.clear(columns=True)
                t.add_columns("类型", "名称", "大小", "sha1")
                t.add_row("📂", "..", "", "")   # 回车返回浏览模式
                for it in items:
                    is_dir = str(it.get("fc") or "1") == "0"
                    sha1 = str(it.get("sha1") or "")
                    t.add_row("📂" if is_dir else "📄",
                              str(it.get("fn") or "?"),
                              "" if is_dir else human_bytes(int(it.get("fs") or 0)),
                              "" if is_dir else sha1[:8])
                self.query_one("#files-path", Static).update(
                    f"🔍 搜索“{kw}”（{len(items)} 项）　回车/s 下载　r 返回浏览")
                t.focus()
            _post(self.app, apply)
        self.run_worker(fill, thread=True)

    def _search_activate_current(self) -> None:
        """s 键 / 下载按钮在搜索模式下激活当前光标行（与回车同路）。"""
        t = self.query_one("#files", DataTable)
        if t.cursor_row is None or t.cursor_row < 0:
            return
        try:
            row = t.get_row_at(t.cursor_row)
        except Exception:  # noqa: BLE001
            return
        self._search_row_activated(row)

    def _search_row_activated(self, row) -> None:
        name = str(row[1])
        if name == "..":
            self.op_refresh()             # 返回浏览模式（load_dir 复位标记与列）
            return
        t = self.query_one("#files", DataTable)
        idx = t.cursor_row - 1            # 减去首行 ..
        if idx < 0 or idx >= len(self._search_results):
            return
        it = self._search_results[idx]
        if str(it.get("fc") or "1") == "0":
            self.app.notify_user("目录下载不支持（搜索结果无路径上下文，v1 仅文件）", True)
            return
        pc = str(it.get("pc") or "")
        if not pc:
            self.app.notify_user("结果缺 pick_code，无法下载", True)
            return
        fname = str(it.get("fn") or "?")
        self.app.push_screen(
            PromptModal("下载",
                        f"下载 [bold]{_plain(fname)}[/bold] 到本地目录（自动创建）：",
                        placeholder="~/Downloads",
                        validator=lambda v: None if v else "本地目录不能为空"),
            lambda v: v is not None and self._download_by_pc(fname, pc,
                                                             Path(v).expanduser()))

    def _download_by_pc(self, name: str, pc: str, dest_dir: Path) -> None:
        """pick_code 直下（搜索命中即下载，不经 find_entry 路径解析）。"""
        ctx = self.app_ctx
        if ctx is None:
            self.app.notify_user("115 未就绪", error=True)
            return
        status = self.query_one("#dl-status", Static)
        status.remove_class("hidden")
        status.update(f"📥 {name}  准备中…")
        last = [0.0]

        def on_progress(written: int, total: int) -> None:
            now = time.monotonic()
            if now - last[0] < 1.0 and written != total:
                return
            last[0] = now
            from core.progress import human_bytes
            pct = f" {written * 100 // total}%" if total else ""
            _post(self.app, status.update,
                  f"📥 {name}  {human_bytes(written)}{pct}")

        def dl() -> None:
            from cloud115.download import download_by_pick_code
            from core.progress import human_bytes

            async def go():
                return await download_by_pick_code(ctx.cloud, pc, dest_dir,
                                                   on_progress=on_progress)
            try:
                r = self.app._cloud.submit(go()).result()
            except Exception as e:  # noqa: BLE001 -- 含 sha1 不符（.part 现场保留）
                _post(self.app, status.update, f"❌ 下载失败: {e}")
                _post(self.app, self.app.notify_user, f"下载失败: {e}", True)
                return
            _post(self.app, status.update,
                  f"✅ {r['dest'].name}  {human_bytes(r['size'])}  ->  {r['dest']}")
            _post(self.app, self.app.notify_user, f"✅ 已下载到 {r['dest']}")
        self.run_worker(dl, thread=True)

    def op_delete(self) -> None:
        if self._need_ctx() is None:
            return
        sel = self._need_selected()
        if sel is None:
            return
        self.app.push_screen(
            ConfirmModal("删除文件",
                         f"删除 [bold]{_plain(sel[1])}[/bold]？\n移入 115 回收站，可在网盘恢复。",
                         danger=True),
            lambda ok: ok and self._do_delete(sel[1]))

    def op_download(self) -> None:
        if self._need_ctx() is None:
            return
        sel = self._need_selected()
        if sel is None:
            return
        if sel[0].startswith("📂"):
            self.app.notify_user("目录下载 v1 不支持（逐文件取直链会快速烧 API 配额）", True)
            return
        self.app.push_screen(
            PromptModal("下载", f"下载 [bold]{_plain(sel[1])}[/bold] 到本地目录（自动创建）：",
                        placeholder="~/Downloads",
                        validator=lambda v: None if v else "本地目录不能为空"),
            lambda v: v is not None and self._download(sel[1], Path(v).expanduser()))

    def op_rename(self) -> None:
        sel = self._need_selected()
        if sel is None:
            return
        self.app.push_screen(
            PromptModal("重命名", f"重命名 [bold]{_plain(sel[1])}[/bold] 为：", value=sel[1],
                        validator=lambda v: None if v and "/" not in v else "新名不能为空且不含 /"),
            lambda v: v is not None and self._rename(sel[1], v))

    def op_move(self) -> None:
        sel = self._need_selected()
        if sel is None:
            return
        self.app.push_screen(
            PromptModal("移动",
                        f"移动 [bold]{_plain(sel[1])}[/bold] 到 115 目录（不存在自动创建）：",
                        value=self.path,
                        validator=lambda v: None if v else "目标目录不能为空"),
            lambda v: v is not None and self._move(sel[1], v))

    def op_mkdir(self) -> None:
        self.app.push_screen(
            PromptModal("新建目录", f"在 [bold]{_plain(self.path)}[/bold] 下新建目录：",
                        validator=lambda v: None if v and "/" not in v
                        else "目录名不能为空且不含 /（逐级进入再建）"),
            lambda v: v is not None and self._mkdir(v))

    def _download(self, name: str, dest_dir: Path) -> None:
        ctx = self.app_ctx
        if ctx is None:
            self.app.notify_user("115 未就绪", error=True)
            return
        status = self.query_one("#dl-status", Static)
        status.remove_class("hidden")
        full = self.path.rstrip("/") + "/" + name
        status.update(f"📥 {name}  准备中…")
        last = [0.0]

        def on_progress(written: int, total: int) -> None:
            now = time.monotonic()
            if now - last[0] < 1.0 and written != total:
                return
            last[0] = now
            from core.progress import human_bytes
            pct = f" {written * 100 // total}%" if total else ""
            _post(self.app, status.update,
                                      f"📥 {name}  {human_bytes(written)}{pct}")

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

    def _do_delete(self, name: str) -> None:
        ctx = self.app_ctx
        if ctx is None:
            return

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
        self.app.notify_user(f"🧹 已删除 {name}（回收站可恢复）")

    def _do_upload(self, src: str) -> None:
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
        yield Label("⏬ 115 离线任务（30s 自动刷新）", classes="page-title")
        yield DataTable(id="off-t", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="off-ops"):
            yield Button("删除任务 (d)", id="op-off-del", variant="error")
            yield Button("刷新 (r)", id="op-off-refresh")
        with Horizontal(id="off-add-row"):
            yield Input(placeholder="添加：粘贴 magnet/ed2k/直链 后回车（保存到 upload.target_dir）",
                        id="off-add")
            yield Button("添加 (a)", id="op-off-add", variant="primary", disabled=True)

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
        elif event.key == "a":        # 快速进入添加输入框
            self.query_one("#off-add", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        ops = {"op-off-del": self._delete_selected,
               "op-off-refresh": self.refresh_list,
               "op-off-add": self._submit_add}
        fn = ops.get(event.button.id or "")
        if fn:
            fn()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "off-add":
            self.query_one("#op-off-add", Button).disabled = not event.value.strip()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "off-add":
            self._submit_add()

    def _submit_add(self) -> None:
        """添加入口（输入框回车 / 按钮共用）；提交后清空并复位按钮。"""
        inp = self.query_one("#off-add", Input)
        url = inp.value.strip()
        if not url:
            return
        inp.value = ""
        self._do_add(url)

    def _delete_selected(self) -> None:
        ctx = self.app_ctx
        t = self.query_one("#off-t", DataTable)
        if ctx is None:
            self.app.notify_user("115 未就绪", error=True)
            return
        try:
            row = t.get_row_at(t.cursor_row)
        except Exception:  # noqa: BLE001 -- 空表/无选中
            self.app.notify_user("先选中一个任务（↑/↓ 移动光标）", True)
            return
        ih = str(row[3])
        if not ih:
            return
        name = str(row[1])
        self.app.push_screen(
            ConfirmModal("删除离线任务",
                         f"删除 [bold]{_plain(name)}[/bold]？\n将连同已下载的源文件一起删除。",
                         danger=True),
            lambda ok: ok and self._do_delete(ih))

    def _do_delete(self, ih: str) -> None:
        ctx = self.app_ctx
        if ctx is None:
            return

        def fill() -> None:
            async def go():
                await ctx.cloud.raw.offline_del(ih, del_source_file=1)
            self.app._cloud.submit(go()).result(60)
            _post(self.app, self.refresh_list)
        self.run_worker(fill, thread=True)
        self.app.notify_user(f"🧹 已删离线任务 {ih}…")

    def _do_add(self, url: str) -> None:
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
        yield Label(f"📜 日志（{service.STDOUT_LOG}）", classes="page-title")
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
        yield Label("🔧 配置", classes="page-title")
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
                    yield Button("保存并校验 (s)", id="cfg-save", variant="primary")
                    yield Button("重新加载 (l)", id="cfg-reload")
                    yield Button("重启服务 (r)", id="cfg-restart", variant="warning")
                yield Static("", id="cfg-status")
                yield Label("保存为全文写回（自动备份 config.yaml.bak.<时间戳>）；"
                            "大部分参数需重启服务生效", classes="hint")
            with TabPane("115 授权"):
                yield AuthSection()

    def on_mount(self) -> None:
        self._reload()
        # 页面内要有焦点，快捷键才能冒泡到页面；聚焦保存键（不在编辑区）
        self.call_after_refresh(self.query_one("#cfg-save", Button).focus)
        # 开关初值来自当前配置
        try:
            from scripts import manual
            cfg = manual.load_config()
            self.query_one("#sw-web", Switch).value = bool(cfg.web.enable)
            self.query_one("#sw-keep", Switch).value = bool(cfg.storage.keep_local)
            self.query_one("#sw-chan", Switch).value = bool(cfg.channel_monitor.enabled)
        except Exception as e:  # noqa: BLE001
            self._status(f"❗ 配置读取失败: {e}")

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
        self._dispatch(event.button.id or "")

    def on_key(self, event) -> None:  # noqa: BLE001
        # 焦点在编辑区（TextArea/Input）时不劫持字母键，避免影响配置编辑
        if isinstance(self.screen.focused, (TextArea, Input)):
            return
        keys = {"s": "cfg-save", "l": "cfg-reload", "r": "cfg-restart"}
        bid = keys.get(event.key)
        if bid:
            self._dispatch(bid)

    def _dispatch(self, bid: str) -> None:
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
            btn = self.query_one("#cfg-restart", Button)

            def cb(ok: bool) -> None:
                if not ok:
                    return

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
            self.app.push_screen(
                ConfirmModal("重启服务", "确认重启服务？期间短暂不可用。", danger=True), cb)


class AuthSection(Vertical):
    """115 扫码授权（原独立授权页，现为配置页的子 Tab）。"""

    def compose(self) -> ComposeResult:
        yield Label("🔑 115 扫码授权", classes="page-title")
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
            self.query_one("#auth-state", Static).update(f"❗ {e}")

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
