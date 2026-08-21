"""Pyrogram 命令与媒体消息处理。

收到媒体 → 取文件名/大小 → 回复一条跟踪消息 → 入队 → worker 跑 pipeline → 跟踪消息被持续更新。
"""
from __future__ import annotations

import logging

from pyrogram import filters
from pyrogram.types import Message

from config import get_config
from core.app import state
from core.downloader import media_info
from core.progress import human_bytes
from core.offline import classify_link, submit
from core.queue import Task
from utils.rate import with_flood_wait

log = logging.getLogger(__name__)

HELP = (
    "**tg115bot** —— TG 视频 → 115 网盘\n\n"
    "直接发送视频/文件给我即可上传到 115。\n\n"
    "/start /help — 本帮助\n"
    "/setdir `<115路径>` — 设置你的 115 目标目录\n"
    "/auth — 检查 115 授权状态（多账号逐个探活）\n"
    "/cancel — 取消你最近一个进行中任务\n"
    "/channels — 查看频道监控规则\n"
    "/addchannel `<频道ID>` `<目标目录>` `[关键词...]` — 新增频道规则\n"
    "/delchannel `<规则ID>` — 删除频道规则\n"
    "/offline `<链接>` — 115 离线下载（磁力/ed2k/直链，也可直接发链接）\n"
    "/offlines — 查看离线任务队列\n"
    "/addrss `<RSS地址> [目录] [关键词...]` — 订阅 RSS 自动离线\n"
    "/rsss — 查看订阅 / `/delrss <ID>` — 退订\n"
    "/sub `<片名>` — 订阅电影（资源发布自动离线，需 nullbr API）\n"
    "/subs — 订阅列表 / `/unsub <ID>` — 取消订阅"
)


def _authorized(message: Message) -> bool:
    cfg = get_config()
    if not cfg.telegram.allowed_users:
        return True
    uid = message.from_user.id if message.from_user else None
    return uid in cfg.telegram.allowed_users


def register(app) -> None:
    @app.on_message(filters.command(["start", "help"]))
    async def _help(_, message: Message):
        await message.reply_text(HELP)

    @app.on_message(filters.command("setdir"))
    async def _setdir(_, message: Message):
        if not _authorized(message):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            cur = state.user_target_dirs.get(message.from_user.id, "(默认)")
            await message.reply_text(f"用法: /setdir /tg115bot/movies\n当前: {cur}")
            return
        target = parts[1].strip()
        state.user_target_dirs[message.from_user.id] = target
        await message.reply_text(f"✅ 目标目录已设为: {target}")

    @app.on_message(filters.command("auth"))
    async def _auth(_, message: Message):
        if not _authorized(message):
            return
        if state.accounts is None:
            await message.reply_text("⚠️ 115 客户端未初始化")
            return
        # 未授权账号 -> 发起扫码授权；已授权 -> 探活报告
        results = await state.accounts.check_all()
        to_auth = [n for n, ok in results.items() if not ok]
        if to_auth:
            await _start_qr_auth(message, to_auth[0])
            return
        lines = ["**115 账号状态**"]
        for name, ok in results.items():
            lines.append(f"{'✅' if ok else '❌'} {name}")
        await message.reply_text("\n".join(lines))

    async def _start_qr_auth(message: Message, account_name: str) -> None:
        """对指定账号发起 115 扫码授权：发二维码图 -> 轮询 -> 换 token。"""
        import asyncio
        import io

        client = state.accounts.get_client(account_name)
        if client is None:
            await message.reply_text(f"❌ 账号 {account_name} 不存在")
            return
        api = client.raw
        try:
            qr = await api.start_qr_auth()
        except Exception as e:  # noqa: BLE001
            await message.reply_text(f"❌ 发起授权失败: {e}")
            return

        try:
            import qrcode
            img = qrcode.make(qr["qrcode"])
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
        except ImportError:
            # 无 qrcode 库时降级为文本链接
            await message.reply_text(
                f"🔐 请用 115 APP 扫码授权（账号 {account_name}）：\n{qr['qrcode']}"
            )
        else:
            await message.reply_photo(
                buf, caption=f"🔐 请用 115 APP 扫码授权（账号 {account_name}），2 分钟内有效"
            )

        # 轮询扫码状态（先等 5s 再轮询；最长 ~2 分钟）
        await asyncio.sleep(5)
        deadline = asyncio.get_event_loop().time() + 120
        while asyncio.get_event_loop().time() < deadline:
            st = await api.poll_qr_status(qr["uid"], qr["time"], qr["sign"])
            if st == 2:
                try:
                    ok = await api.exchange_qr_token(qr["uid"], qr["verifier"])
                except Exception as e:  # noqa: BLE001
                    await message.reply_text(f"❌ 换取 token 失败: {e}")
                    return
                if ok:
                    state.accounts.mark_authorized(account_name)
                    await message.reply_text(f"✅ 账号 {account_name} 授权成功，可以开始上传了")
                return
            if st == -1:
                await message.reply_text("⌛ 二维码已过期，请重新 /auth")
                return
            if st == -2:
                await message.reply_text("🚫 你在 APP 里取消了授权，请重新 /auth")
                return
            await asyncio.sleep(2)
        await message.reply_text("⌛ 授权超时，请重新 /auth")

    @app.on_message(filters.command("cancel"))
    async def _cancel(_, message: Message):
        if not _authorized(message):
            return
        ok = state.cancel_latest(message.from_user.id)
        await message.reply_text(
            "🚫 已请求取消最近一个任务" if ok else "没有进行中的任务"
        )

    @app.on_message(filters.command("channels"))
    async def _channels(_, message: Message):
        if not _authorized(message):
            return
        if state.db is None:
            await message.reply_text("⚠️ 持久化未启用")
            return
        rules = await state.db.list_rules()
        if not rules:
            await message.reply_text("暂无频道规则。用法: /addchannel <频道ID> <目标目录> [关键词...]")
            return
        lines = ["**频道规则**"]
        for r in rules:
            wl = "/".join(r.whitelist) or "(全部)"
            lines.append(f"`{r.id}` · `{r.channel_id}` {r.title} -> {r.target_dir or '(默认)'}\n   关键词: {wl}")
        await message.reply_text("\n".join(lines))

    @app.on_message(filters.command("addchannel"))
    async def _addchannel(_, message: Message):
        if not _authorized(message):
            return
        if state.db is None:
            await message.reply_text("⚠️ 持久化未启用")
            return
        parts = (message.text or "").split()
        if len(parts) < 3:
            await message.reply_text("用法: /addchannel <频道ID> <目标目录> [关键词...]")
            return
        try:
            channel_id = int(parts[1])
        except ValueError:
            await message.reply_text("频道ID 必须是数字")
            return
        target_dir = parts[2]
        keywords = parts[3:]
        title = message.chat.title or "" if message.chat else ""
        await state.db.upsert_rule(channel_id, title, keywords, [], target_dir, True)
        if state.monitor is not None:
            await state.monitor.reload()
        await message.reply_text(f"✅ 已添加/更新频道规则: {channel_id} -> {target_dir}")

    @app.on_message(filters.command("delchannel"))
    async def _delchannel(_, message: Message):
        if not _authorized(message):
            return
        if state.db is None:
            await message.reply_text("⚠️ 持久化未启用")
            return
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.reply_text("用法: /delchannel <规则ID>")
            return
        try:
            rule_id = int(parts[1])
        except ValueError:
            await message.reply_text("规则ID 必须是数字")
            return
        await state.db.delete_rule(rule_id)
        if state.monitor is not None:
            await state.monitor.reload()
        await message.reply_text(f"✅ 已删除规则 {rule_id}")

    @app.on_message(filters.command("offline"))
    async def _offline(_, message: Message):
        if not _authorized(message):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await with_flood_wait(lambda: message.reply_text(
                "用法: /offline <magnet/ed2k/链接> （也可直接发链接给我）\n"
                "保存目录: /setdir 设置的目录或默认 /tg115bot"
            ))
            return
        await _do_offline(message, parts[1].strip())

    @app.on_message(filters.command("offlines"))
    async def _offlines(_, message: Message):
        if not _authorized(message):
            return
        if state.db is None:
            await message.reply_text("持久化未启用")
            return
        from persistence.models import OFFLINE_DONE, OFFLINE_FAILED
        rows = await state.db.offline_by_status(
            "pending", "running", "retrying", OFFLINE_DONE, OFFLINE_FAILED)
        if not rows:
            await message.reply_text("暂无离线任务")
            return
        icon = {"pending": "⏳", "running": "⬇️", "retrying": "🔁", "done": "✅", "failed": "❌"}
        lines = ["**离线任务**"]
        for r in rows[-15:]:   # 最近 15 条
            pct = f" {r.percent}%" if r.status == "running" else ""
            name = r.name or r.url[:50]
            lines.append(f"{icon.get(r.status, '•')} {name}{pct} → {r.save_path}")
        await message.reply_text("\n".join(lines))

    async def _do_offline(message: Message, url: str):
        cfg = get_config()
        target = state.user_target_dirs.get(message.from_user.id) or cfg.upload.target_dir
        ok, msg = await submit(url, target, source="manual", chat_id=message.chat.id)
        await with_flood_wait(lambda: message.reply_text(
            f"🚀 {msg}\n📁 {target}" if ok else f"⚠️ {msg}"
        ))

    media_filter = (
        filters.video | filters.animation | filters.audio | filters.voice
        | filters.video_note | filters.document
    )

    @app.on_message(filters.command("addrss"))
    async def _addrss(_, message: Message):
        if not _authorized(message):
            return
        if state.db is None:
            await message.reply_text("⚠️ 持久化未启用")
            return
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.reply_text(
                "用法: /addrss <RSS地址> [保存目录] [关键词...]\n"
                "例: /addrss https://x.com/feed.xml /tg115bot/pt 4K HEVC\n"
                "关键词为标题白名单（留空=全部条目）"
            )
            return
        url = parts[1]
        save_path = parts[2] if len(parts) > 2 and parts[2].startswith("/") else ""
        kw_start = 3 if save_path else 2
        keywords = parts[kw_start:]
        from urllib.parse import urlparse as _up
        if _up(url).scheme not in ("http", "https"):
            await message.reply_text("RSS 地址需以 http(s):// 开头")
            return
        feed = await state.db.add_feed(url, "", keywords, save_path, message.chat.id)
        if feed is None:
            await message.reply_text("⚠️ 该 RSS 已订阅过")
            return
        kw = "/".join(keywords) or "(全部)"
        await message.reply_text(
            f"✅ 已订阅 RSS #{feed.id}\n{url}\n关键词: {kw}\n📁 {save_path or '(默认目录)'}\n"
            f"每 10 分钟检查一次，新条目自动离线下载"
        )

    @app.on_message(filters.command("rsss"))
    async def _rsss(_, message: Message):
        if not _authorized(message):
            return
        if state.db is None:
            await message.reply_text("⚠️ 持久化未启用")
            return
        feeds = await state.db.list_feeds()
        if not feeds:
            await message.reply_text("暂无订阅。用法: /addrss <RSS地址> [目录] [关键词...]")
            return
        lines = ["**RSS 订阅**"]
        for f in feeds:
            kw = "/".join(f.whitelist) or "(全部)"
            err = f" ⚠️{f.last_error[:30]}" if f.last_error else ""
            lines.append(f"`{f.id}` {f.name or f.url[:60]} 关键词:{kw}{err}")
        await message.reply_text("\n".join(lines))

    @app.on_message(filters.command("delrss"))
    async def _delrss(_, message: Message):
        if not _authorized(message):
            return
        if state.db is None:
            await message.reply_text("⚠️ 持久化未启用")
            return
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            await message.reply_text("用法: /delrss <订阅ID>")
            return
        await state.db.delete_feed(int(parts[1]))
        await message.reply_text(f"✅ 已退订 {parts[1]}")

    @app.on_message(filters.command("sub"))
    async def _sub(_, message: Message):
        if not _authorized(message):
            return
        if state.db is None:
            await message.reply_text("⚠️ 持久化未启用")
            return
        cfg = get_config()
        if not (cfg.movie_sub.app_id and cfg.movie_sub.api_key):
            await message.reply_text(
                "⚠️ 未配置 nullbr API 授权（config.yaml 的 movie_sub.app_id/api_key），\n"
                "申请: https://nullbr.online/api"
            )
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.reply_text("用法: /sub 流浪地球")
            return
        movie_name = parts[1].strip()
        notice = await with_flood_wait(lambda: message.reply_text(f"🔍 TMDB 搜索: {movie_name} …"))
        from core.movie_sub import tmdb_search_id
        tmdb_id = await tmdb_search_id(movie_name)
        if not tmdb_id:
            await message.reply_text(f"❌ TMDB 未找到: {movie_name}（换个名字/英文名试试）")
            return
        save_path = cfg.movie_sub.target_dir
        sub = await state.db.add_movie_sub(tmdb_id, movie_name, save_path, message.chat.id)
        if sub is None:
            await message.reply_text(f"⚠️ 已订阅过该电影（TMDB {tmdb_id}）")
            return
        await message.reply_text(
            f"✅ 已订阅《{movie_name}》\nTMDB: {tmdb_id}\n📁 {save_path}\n"
            f"每 4 小时检查一次，资源发布自动离线下载"
        )

    @app.on_message(filters.command("subs"))
    async def _subs(_, message: Message):
        if not _authorized(message):
            return
        if state.db is None:
            await message.reply_text("⚠️ 持久化未启用")
            return
        subs = await state.db.list_movie_subs()
        if not subs:
            await message.reply_text("暂无电影订阅。用法: /sub <片名>")
            return
        lines = ["**电影订阅**"]
        for sub in subs:
            mark = "✅已下载" if sub.downloaded else "⏳等资源"
            lines.append(f"`{sub.id}` {sub.movie_name} [{mark}]")
        await message.reply_text("\n".join(lines))

    @app.on_message(filters.command("unsub"))
    async def _unsub(_, message: Message):
        if not _authorized(message):
            return
        if state.db is None:
            await message.reply_text("⚠️ 持久化未启用")
            return
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            await message.reply_text("用法: /unsub <订阅ID>")
            return
        await state.db.delete_movie_sub(int(parts[1]))
        await message.reply_text(f"✅ 已取消订阅 {parts[1]}")

    @app.on_message(filters.text & ~filters.command(
        ["start", "help", "setdir", "auth", "cancel", "channels",
         "addchannel", "delchannel", "offline", "offlines"]))
    async def on_link(_, message: Message):
        """纯文本链接（magnet/ed2k/直链）自动识别为离线下载。"""
        if not _authorized(message):
            return
        kind = classify_link(message.text or "")
        if kind is None:
            return   # 普通文本，忽略
        await _do_offline(message, (message.text or "").strip())

    @app.on_message(media_filter)
    async def on_media(_, message: Message):
        if not _authorized(message):
            return
        name, size = media_info(message)
        cfg = get_config()
        target_dir = (
            state.user_target_dirs.get(message.from_user.id) or cfg.upload.target_dir
        )

        # 磁盘水位预检：不足则告警一次并拒绝入队
        ws = state.workspace
        if ws is not None and not ws.has_enough_space(size):
            await with_flood_wait(lambda: message.reply_text(
                f"⚠️ 磁盘空间不足（需预留 {cfg.storage.min_free_gb}GB），暂停接新任务。\n"
                f"当前剩余 {human_bytes(ws.free_bytes())}"
            ))
            state.low_disk_alerted = True
            return

        tracking = await with_flood_wait(lambda: message.reply_text(
            f"📥 已加入队列\n📄 {name}\n📦 {human_bytes(size)}\n📁 {target_dir}"
        ))

        task = Task(
            user_id=message.from_user.id if message.from_user else 0,
            message=message,
            filename=name,
            size=size,
            target_dir=target_dir,
            tracking_chat_id=tracking.chat.id,
            tracking_message_id=tracking.id,
        )
        state.register_task(task)        # 注册取消表（即便还在排队也可取消）
        await state.queue.put(task)
        if state.low_disk_alerted and ws is not None and ws.has_enough_space(size):
            state.low_disk_alerted = False   # 恢复
        log.info("入队: %s (%d bytes) -> %s", name, size, target_dir)
