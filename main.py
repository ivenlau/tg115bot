"""tg115bot 入口：组装并启动 日志 + DB + 多账号 115 + 队列 + Pyrogram + 频道监控 + Web 台。

Phase 3-4：持久化、多账号轮转、频道监控、Web 管理台、凭据加密、结构化日志。
"""
from __future__ import annotations

import asyncio
import logging
import sys

from config import load_config
from utils.logging import (install_rotating_stdout, log_drainer,
                           log_retention_loop, setup_logging)
from cloud115.account import AccountManager
from core.workspace import Workspace
from core.queue import TaskQueue
from core.pipeline import run_task
from core.app import state
from core.offline import offline_watcher
from core.rss import rss_watcher
from bot.client import build_bot, build_user
from bot.handlers import register
from bot.channel_monitor import ChannelMonitor
from persistence.db import Database

log = logging.getLogger("tg115bot")


def _force_utf8_stdio() -> None:
    """stdout/stderr 重定向到文件时强制 UTF-8。

    Windows 上 Python 重定向输出默认跟 locale 走（中文系统=GBK），而 PowerShell 7
    的 Get-Content 默认按 UTF-8 读 -> 乱码；PS 5.1 又只认 ANSI。统一写 UTF-8，
    两版读端显式 -Encoding UTF8 即全对齐。控制台直跑不受影响。
    """
    for s in (sys.stdout, sys.stderr):
        try:
            if not s.isatty() and s.encoding.lower().replace("-", "") != "utf8":
                s.reconfigure(encoding="utf-8")
        except Exception:
            pass


async def _start_web(cfg) -> tuple | None:
    """启动 Web 管理台（uvicorn 后台任务）。返回 (server, task) 或 None。"""
    if not cfg.web.enable:
        return None
    import uvicorn
    from web.app import create_app
    app = create_app(state, state.db, state.accounts)
    config = uvicorn.Config(
        app, host=cfg.web.host, port=cfg.web.port,
        log_config=None, access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="tg115bot-web")
    log.info("Web 管理台已启动: http://%s:%d", cfg.web.host, cfg.web.port)
    return server, task


async def main() -> None:
    _force_utf8_stdio()
    cfg = load_config()
    install_rotating_stdout(cfg)   # 越早越好：此后所有 print/traceback 限长滚动
    db_log_handler = setup_logging(cfg)
    state.config = cfg
    log.info("配置加载完成；work_dir=%s", cfg.work_dir_abs)

    # ── 持久化 ────────────────────────────────────────────────────────────
    db = Database(cfg.db_path)
    await db.init()
    state.db = db
    drainer_task: asyncio.Task | None = None
    if db_log_handler is not None:
        drainer_task = asyncio.create_task(
            log_drainer(db, db_log_handler), name="tg115bot-log-drainer"
        )
    retention_task = asyncio.create_task(
        log_retention_loop(cfg), name="tg115bot-log-retention"
    )
    offline_task = asyncio.create_task(offline_watcher(), name="tg115bot-offline")
    rss_task = asyncio.create_task(rss_watcher(), name="tg115bot-rss")

    # ── 115 多账号 ────────────────────────────────────────────────────────
    accounts = AccountManager(cfg.accounts, cfg.session_dir, cfg.rate115.min_interval_sec, db)
    try:
        await accounts.init()
    except Exception as e:  # noqa: BLE001
        log.error("115 账号初始化全部失败: %r", e)
    state.accounts = accounts
    if accounts.names():
        state.cloud = accounts.primary  # 兼容：/auth 等可直接用主账号

    # ── 工作区 + 队列 ────────────────────────────────────────────────────
    state.workspace = Workspace(cfg.work_dir_abs, cfg.storage.min_free_gb,
                                keep_local=cfg.storage.keep_local)
    state.queue = TaskQueue(concurrency=cfg.queue.concurrency, runner=run_task)
    await state.queue.start()

    # ── Pyrogram 客户端 ──────────────────────────────────────────────────
    bot = build_bot(cfg)
    user = build_user(cfg)
    state.pyro_bot = bot
    state.pyro_user = user
    register(bot)

    # 频道监控（在 bot.start 前注册 handler）
    if cfg.channel_monitor.enabled:
        monitor = ChannelMonitor()
        await monitor.reload()
        monitor.register(bot)
        state.monitor = monitor
        log.info("频道监控已启用，规则 %d 条", monitor.rule_count)

    await bot.start()
    if user is not None:
        try:
            await user.start()
            log.info("user session 已启用（下载额度更高，不易触发 FloodWait）")
        except Exception as e:  # noqa: BLE001
            log.warning("user client 启动失败，退回仅 bot: %r", e)
            state.pyro_user = None

    me = await bot.get_me()
    log.info("bot 已上线：@%s - 发送视频/文件即上传到 115", me.username)

    # ── AI 助手（配置启用才加载） ─────────────────────────────────────────
    if cfg.ai.enabled:
        from ai import tools as _ai_tools  # noqa: F401 触发工具注册
        from ai.agent import load_sessions
        from ai.dynamic import load_dynamic_tools
        await load_dynamic_tools()
        await load_sessions()
        log.info("AI 助手已启用（模型=%s，工具=%d）", cfg.ai.model, len(_ai_tools.TOOLS))

    # ── Web 管理台 ───────────────────────────────────────────────────────
    web = await _start_web(cfg)

    try:
        await asyncio.Event().wait()  # 永久挂起直到被停止
    finally:
        log.info("正在停止 …")
        if web is not None:
            web[0].should_exit = True
            try:
                await asyncio.wait_for(web[1], timeout=5)
            except asyncio.TimeoutError:
                web[1].cancel()
        if drainer_task is not None:
            drainer_task.cancel()
            await asyncio.gather(drainer_task, return_exceptions=True)
        retention_task.cancel()
        await asyncio.gather(retention_task, return_exceptions=True)
        offline_task.cancel()
        await asyncio.gather(offline_task, return_exceptions=True)
        rss_task.cancel()
        await asyncio.gather(rss_task, return_exceptions=True)
        await state.queue.stop()
        if state.pyro_user is not None:
            await state.pyro_user.stop()
        await bot.stop()
        if state.accounts is not None:
            await state.accounts.close()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
