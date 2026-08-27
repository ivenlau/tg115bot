"""Pyrogram 命令与媒体消息处理。

收到媒体 → 取文件名/大小 → 回复一条跟踪消息 → 入队 → worker 跑 pipeline → 跟踪消息被持续更新。
"""
from __future__ import annotations

import asyncio
import logging
import time

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
    "/status — 115 空间/离线配额/风控余量/账号/队列一览\n"
    "/ls `<115路径>` — 列目录　`/search <关键词>` — 全盘搜索\n"
    "/rm `<路径>` — 删除（二次确认）　`/mv <源路径> <目的目录>` — 移动\n"
    "/backup `<频道ID或@用户名> [目录]` — 整频道历史备份（断点续传）\n"
    "/backups — 备份进度　`/backupstop <ID>` — 暂停备份\n"
    "发 115 分享链接（含访问码）自动转存（需 config.share.cookies）\n"
    "/dl `<http直链>` — 本地中转下载后上传（服务器直连或走代理）\n"
    "/ai — AI 助手模式开关/状态（配置后普通文本即对话） | /aireset — 清空 AI 记忆\n"
    "/aitools — 查看/删除 AI 创建的动态工具"
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
        | filters.video_note | filters.document | filters.photo
    )

    # ── 相册聚合：同 media_group_id 的消息缓冲后逐个入队（顺序稳定） ──────
    _album_buf: dict = {}          # group_id -> {"msgs": [...], "timer": Task}
    ALBUM_WINDOW = 2.0             # 聚合窗口（秒）：同组消息到达间隔上限

    async def _flush_album(group_id):
        """聚合窗口到期：按序入队。游离 task，自身必须吞掉异常。"""
        try:
            await _flush_album_inner(group_id)
        except Exception:  # noqa: BLE001 -- call_later 派生 task 无人 await，异常必须自捕
            log.exception("相册聚合入队失败: %r", group_id)

    async def _flush_album_inner(group_id):
        buf = _album_buf.pop(group_id, None)
        if not buf:
            return
        msgs = sorted(buf["msgs"], key=lambda m: m.id)   # 按消息序号保序
        first = msgs[0]
        total = sum(media_info(m)[1] for m in msgs)
        await with_flood_wait(lambda: first.reply_text(
            f"📸 相册已加入队列（{len(msgs)} 项 / {human_bytes(total)}），将按顺序上传"
        ))
        gid_tail = str(group_id)[-6:]
        for m in msgs:
            await _enqueue_media(m, album_note=f"相册 {gid_tail}")
        log.info("相册入队: %s (%d 项)", group_id, len(msgs))

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

    @app.on_message(filters.command("status"))
    async def _status(_, message: Message):
        if not _authorized(message):
            return
        if state.accounts is None:
            await message.reply_text("⚠️ 115 未初始化")
            return
        cfg = get_config()
        lines = ["**📊 tg115bot 状态**", ""]

        # 账号
        for a in state.accounts.status_list():
            cd = f" (冷却 {a['cooldown_sec']}s)" if a.get("cooldown_sec") else ""
            lines.append(f"👤 {a['name']}: {a['status']}{cd}")
        lines.append("")

        # 队列
        if state.queue is not None:
            lines.append(f"🌀 队列：进行中/待处理 {len(state.task_progress)} / {state.queue.qsize()}")
        if state.workspace is not None:
            lines.append(f"💾 本地磁盘剩余：{human_bytes(state.workspace.free_bytes())}"
                         f"（下限 {cfg.storage.min_free_gb}GB）")

        # 115 侧（尽力而为，任一失败不阻断）
        try:
            cloud = await state.accounts.get()
            space = await cloud.raw.user_space()
            if space.get("total"):
                used_pct = space["used"] * 100 / space["total"]
                lines.append(f"☁️ 115 空间：{human_bytes(space['used'])} / "
                             f"{human_bytes(space['total'])}（{used_pct:.1f}%）")
        except Exception as e:  # noqa: BLE001
            lines.append(f"☁️ 115 空间：获取失败（{e}）")
        try:
            quota = await cloud.raw.offline_quota()
            if quota:
                lines.append(f"⬇️ 离线配额：已用 {quota.get('used', '?')} / {quota.get('count', '?')}")
        except Exception:  # noqa: BLE001
            pass
        try:
            cnt = cloud.raw.request_count
            lines.append(f"🛡️ 今日 115 API 请求：{cnt}（风控阈值 {cloud.raw.daily_limit}）")
        except Exception:  # noqa: BLE001
            pass

        # 离线任务统计
        if state.db is not None:
            from persistence.models import OFFLINE_DONE, OFFLINE_FAILED
            pend = await state.db.offline_by_status("pending", "running", "retrying")
            lines.append(f"📦 离线任务：进行中 {len(pend)}")

        await message.reply_text("\n".join(lines))

    @app.on_message(filters.command("ls"))
    async def _ls(_, message: Message):
        if not _authorized(message):
            return
        if state.accounts is None:
            await message.reply_text("⚠️ 115 未初始化")
            return
        parts = (message.text or "").split(maxsplit=1)
        path = parts[1].strip() if len(parts) > 1 else "/"
        try:
            cloud = await state.accounts.get()
            if path.strip() != "/":
                info = await cloud.raw.get_file_info(path)
                if not info or info.get("file_id") is None:
                    await message.reply_text(f"❌ 路径不存在: {path}")
                    return
                cid = int(info["file_id"])
            else:
                cid = 0
            data = await cloud.raw.list_files(cid, limit=50)
            items = data.get("list") or []
        except Exception as e:  # noqa: BLE001
            await message.reply_text(f"❌ 列目录失败: {e}")
            return
        if not items:
            await message.reply_text(f"📁 {path}（空目录）")
            return
        from core.progress import human_bytes as _hb
        lines = [f"📁 **{path}**（{len(items)} 项）"]
        for it in items[:30]:
            name = it.get("fn") or it.get("n") or it.get("file_name") or "?"
            is_dir = str(it.get("fc") or it.get("file_category") or "1") == "0"
            size = it.get("fs") or it.get("size") or 0
            lines.append(f"{'📂' if is_dir else '📄'} {name}" + ("" if is_dir else f" `{_hb(size)}`"))
        if len(items) > 30:
            lines.append(f"… 等共 {len(items)} 项")
        await message.reply_text("\n".join(lines))

    @app.on_message(filters.command("search"))
    async def _search(_, message: Message):
        if not _authorized(message):
            return
        if state.accounts is None:
            await message.reply_text("⚠️ 115 未初始化")
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.reply_text("用法: /search 关键词")
            return
        keyword = parts[1].strip()
        try:
            cloud = await state.accounts.get()
            data = await cloud.raw.search_files(keyword, limit=20)
            items = data.get("list") or []
        except Exception as e:  # noqa: BLE001
            await message.reply_text(f"❌ 搜索失败: {e}")
            return
        if not items:
            await message.reply_text(f"🔍 未找到: {keyword}")
            return
        from core.progress import human_bytes as _hb
        lines = [f"🔍 **{keyword}**（{len(items)} 项）"]
        for it in items[:20]:
            name = it.get("fn") or it.get("n") or it.get("file_name") or "?"
            size = it.get("fs") or it.get("size") or 0
            lines.append(f"📄 {name} `{_hb(size)}`" if size else f"📄 {name}")
        await message.reply_text("\n".join(lines))

    @app.on_message(filters.command("rm"))
    async def _rm(_, message: Message):
        if not _authorized(message):
            return
        if state.accounts is None:
            await message.reply_text("⚠️ 115 未初始化")
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.reply_text("用法: /rm /tg115bot/旧文件.mp4（入回收站）")
            return
        path = parts[1].strip()
        from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        key = f"rm:{message.from_user.id}:{path}"
        state.pending_confirm = getattr(state, "pending_confirm", {})
        state.pending_confirm[key] = time.time()
        await message.reply_text(
            f"⚠️ 确认删除（移入回收站）？\n📄 {path}\n\n30 秒内回复 /yes 确认",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ 确认删除", callback_data=f"rmok|{key}"),
                InlineKeyboardButton("取消", callback_data="rmno"),
            ]]),
        )

    @app.on_callback_query()
    async def _on_confirm(client, query):
        data = query.data or ""
        if data.startswith("aitool|"):
            # 动态工具启用确认
            _, row_id, ok = data.split("|")
            from ai.dynamic import confirm as ai_confirm
            result = await ai_confirm(int(row_id), ok == "1")
            await query.answer()
            await query.message.edit_text(f"🛠 {result}")
            return
        if data.startswith("aitooldel|"):
            row_id = int(data.split("|")[1])
            if state.db is None:
                return
            rows = await state.db.ai_tool_list()
            row = next((r for r in rows if r["id"] == row_id), None)
            if row:
                from ai.dynamic import delete_by_name
                result = await delete_by_name(row["name"])
                await query.answer()
                await query.message.edit_text(result)
            return
        if data == "rmno":
            await query.answer("已取消")
            await query.message.edit_text("🚫 已取消删除")
            return
        if not data.startswith("rmok|"):
            return
        key = data[4:]
        pend = getattr(state, "pending_confirm", {})
        ts = pend.pop(key, None)
        if ts is None or time.time() - ts > 60:
            await query.answer("已过期")
            await query.message.edit_text("⌛ 确认已过期，请重新 /rm")
            return
        path = key.split(":", 2)[2]
        try:
            cloud = await state.accounts.get()
            info = await cloud.raw.get_file_info(path)
            if not info or info.get("file_id") is None:
                await query.message.edit_text(f"❌ 路径不存在: {path}")
                return
            await cloud.raw.delete_files(str(info["file_id"]))
            await query.message.edit_text(f"🗑 已删除（回收站）: {path}")
        except Exception as e:  # noqa: BLE001
            await query.message.edit_text(f"❌ 删除失败: {e}")

    @app.on_message(filters.command("mv"))
    async def _mv(_, message: Message):
        if not _authorized(message):
            return
        if state.accounts is None:
            await message.reply_text("⚠️ 115 未初始化")
            return
        parts = (message.text or "").split()
        if len(parts) < 3:
            await message.reply_text("用法: /mv /tg115bot/旧目录/文件.mp4 /tg115bot/新目录")
            return
        src, dst = parts[1], parts[2]
        try:
            cloud = await state.accounts.get()
            si = await cloud.raw.get_file_info(src)
            if not si or si.get("file_id") is None:
                await message.reply_text(f"❌ 源不存在: {src}")
                return
            di = await cloud.raw.get_file_info(dst)
            if not di or di.get("file_id") is None:
                to_cid = await cloud.raw.create_dir_recursive(dst)
            else:
                to_cid = int(di["file_id"])
            await cloud.raw.move_files(str(si["file_id"]), to_cid)
            await message.reply_text(f"✅ 已移动\n{src}\n→ {dst}")
        except Exception as e:  # noqa: BLE001
            await message.reply_text(f"❌ 移动失败: {e}")

    @app.on_message(filters.command("backup"))
    async def _backup(_, message: Message):
        if not _authorized(message):
            return
        if state.db is None or state.accounts is None:
            await message.reply_text("⚠️ 服务未就绪")
            return
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.reply_text(
                "用法: /backup <频道ID或@频道用户名> [保存目录]\n"
                "例: /backup -1001234567890 /tg115bot/archive\n"
                "bot 需已加入该频道；中断后重发命令自动续传"
            )
            return
        chan = parts[1]
        cfg = get_config()
        save_path = parts[2] if len(parts) > 2 and parts[2].startswith("/") else cfg.upload.target_dir
        # 解析频道 -> chat
        try:
            chat = await state.pyro_bot.get_chat(chan)
        except Exception as e:  # noqa: BLE001
            await message.reply_text(f"❌ 找不到频道 {chan}: {e}")
            return
        from core.backup import start_backup
        ok, msg = await start_backup(chat.id, chat.title or str(chan), save_path,
                                     message.chat.id)
        await message.reply_text(("🚀 " if ok else "⚠️ ") + msg + f"\n频道: {chat.title or chan}\n📁 {save_path}")

    @app.on_message(filters.command("backups"))
    async def _backups(_, message: Message):
        if not _authorized(message):
            return
        if state.db is None:
            await message.reply_text("⚠️ 持久化未启用")
            return
        rows = await state.db.list_backups()
        if not rows:
            await message.reply_text("暂无备份。用法: /backup <频道ID或@用户名> [目录]")
            return
        icon = {"running": "▶️", "paused": "⏸", "done": "✅"}
        lines = ["**频道备份**"]
        for r in rows:
            lines.append(
                f"{icon.get(r.status, '•')} `{r.id}` {r.title or r.channel_id}\n"
                f"   入队 {r.total_done} / 跳过 {r.skipped} / 断点 #{r.last_message_id}"
            )
        await message.reply_text("\n".join(lines))

    @app.on_message(filters.command("backupstop"))
    async def _backupstop(_, message: Message):
        if not _authorized(message):
            return
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            await message.reply_text("用法: /backupstop <备份ID>（/backups 查看）")
            return
        from core.backup import stop_backup
        if stop_backup(int(parts[1])):
            await message.reply_text("⏸ 停止请求已发出，进度已保存")
        else:
            await message.reply_text("该备份不在运行中")

    async def _maybe_ai(message: Message, text: str):
        """AI 模式入口：配置启用则进 agent 循环；否则静默忽略（保持纯命令行为）。"""
        from ai import agent as ai_agent
        if not ai_agent.enabled():
            return
        status_msg = None

        async def _status(note: str):
            nonlocal status_msg
            try:
                if status_msg is None:
                    status_msg = await message.reply_text(note)
                else:
                    await status_msg.edit_text(note)
            except Exception:  # noqa: BLE001
                pass

        reply = await ai_agent.chat(message.chat.id,
                                    message.from_user.id if message.from_user else 0,
                                    text, on_status=_status)
        if reply is None:
            return
        try:
            if status_msg is not None:
                await status_msg.delete()
        except Exception:  # noqa: BLE001
            pass
        await message.reply_text(reply[:4000])

    @app.on_message(filters.command("ai"))
    async def _ai(_, message: Message):
        if not _authorized(message):
            return
        from config import get_config
        cfg = get_config()
        if not cfg.ai.enabled:
            await message.reply_text(
                "AI 模式未配置。\n"
                "在 config.yaml 的 ai 段填 base_url/api_key/model 后重启启用。"
            )
            return
        parts = (message.text or "").split()
        if len(parts) > 1 and parts[1].lower() in ("on", "off"):
            from core.app import state as _st
            _st.ai_runtime_enabled = parts[1].lower() == "on"
            await message.reply_text(f"AI 模式：{'✅ 开' if _st.ai_runtime_enabled else '⏸ 关'}")
            return
        model = cfg.ai.model
        await message.reply_text(
            f"🤖 AI 模式已启用（{model}）\n\n"
            f"直接发普通文本即可对话，我会调用工具帮你操作网盘。\n"
            f"/aireset 清空对话记忆 | /ai off 临时关闭"
        )

    @app.on_message(filters.command("aitools"))
    async def _aitools(_, message: Message):
        if not _authorized(message):
            return
        if state.db is None:
            await message.reply_text("⚠️ 持久化未启用")
            return
        rows = await state.db.ai_tool_list()
        if not rows:
            await message.reply_text("暂无动态工具。AI 对话中可让它用 create_dynamic_tool 创建。")
            return
        from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb = [[InlineKeyboardButton(f"🗑 {r['name']}", callback_data=f"aitooldel|{r['id']}")]
              for r in rows]
        lines = ["**动态工具**"]
        for r in rows:
            mark = "✅" if r["enabled"] else "⏳待确认"
            lines.append(f"{mark} {r['name']}: {(r['description'] or '')[:50]}")
        lines.append("\n点下方按钮删除")
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))

    @app.on_message(filters.command("aireset"))
    async def _aireset(_, message: Message):
        if not _authorized(message):
            return
        from ai.agent import reset_session
        await reset_session(message.chat.id)
        await message.reply_text("🧹 AI 对话记忆已清空")

    async def _do_share(message: Message, text: str):
        """转存 115 分享链接到默认目录。"""
        cfg = get_config()
        if not cfg.share.cookies:
            await message.reply_text(
                "⚠️ 未配置转存凭据（config.yaml 的 share.cookies），\n"
                "浏览器登录 115 后复制 Cookie 填入并重启"
            )
            return
        from cloud115.share import parse_share_link, share_list, share_receive
        parsed = parse_share_link(text)
        if not parsed:
            await message.reply_text("❌ 无法解析分享链接")
            return
        share_code, receive_code = parsed
        if not receive_code:
            await message.reply_text("🔗 请用带访问码的完整链接（...?password=访问码）")
            return
        try:
            info, files = await share_list(cfg.share.cookies, share_code, receive_code)
        except Exception as e:  # noqa: BLE001
            await message.reply_text(f"❌ 读取分享失败: {e}")
            return
        if not files:
            await message.reply_text("📁 分享为空")
            return
        names = [f.get("n") or f.get("fn") or "?" for f in files[:5]]
        more = f" 等 {len(files)} 项" if len(files) > 5 else ""
        saving = await message.reply_text(
            f"📥 转存中: {', '.join(names)}{more}\n📁 {cfg.share.target_dir}"
        )
        try:
            cloud = await state.accounts.get()
            cid = await cloud.raw.create_dir_recursive(cfg.share.target_dir)
            fids = [str(f.get("fid") or f.get("f") or "") for f in files]
            fids = [f for f in fids if f]
            await share_receive(cfg.share.cookies, share_code, receive_code, fids, cid)
        except Exception as e:  # noqa: BLE001
            await message.reply_text(f"❌ 转存失败: {e}")
            return
        await saving.edit_text(
            f"✅ 转存完成（{len(fids)} 项）\n📁 {cfg.share.target_dir}\n"
            f"来自: {info.get('share_title') or share_code}"
        )

    @app.on_message(filters.command("dl"))
    async def _dl(_, message: Message):
        if not _authorized(message):
            return
        if state.accounts is None or state.workspace is None:
            await message.reply_text("⚠️ 服务未就绪")
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.reply_text(
                "用法: /dl <http直链>\n"
                "本地中转下载后上传 115（与离线互补：115 离线搞不定的直链源用这个）"
            )
            return
        url = parts[1].strip()
        from urllib.parse import urlparse as _up
        if _up(url).scheme not in ("http", "https"):
            await message.reply_text("链接需以 http(s):// 开头")
            return
        cfg = get_config()
        target_dir = state.user_target_dirs.get(message.from_user.id) or cfg.upload.target_dir
        from core.direct_dl import url_filename
        name = url_filename(url)
        ws = state.workspace
        if ws is not None and not ws.has_enough_space(0):
            await message.reply_text("⚠️ 磁盘空间不足，暂停接新任务")
            return
        tracking = await with_flood_wait(lambda: message.reply_text(
            f"📥 直链任务已加入队列\n📄 {name}\n🔗 {url[:80]}\n📁 {target_dir}"
        ))
        task = Task(
            user_id=message.from_user.id if message.from_user else 0,
            message=url,            # 直链任务复用 message 字段存 URL
            filename=name, size=0, target_dir=target_dir,
            tracking_chat_id=tracking.chat.id, tracking_message_id=tracking.id,
            source="direct",
        )
        state.register_task(task)
        await state.queue.put(task)
        log.info("直链入队: %s -> %s", url[:80], target_dir)

    @app.on_message(filters.text & ~filters.command(
        ["start", "help", "setdir", "auth", "cancel", "channels",
         "addchannel", "delchannel", "offline", "offlines",
         "addrss", "rsss", "delrss",
         "status", "ls", "search", "rm", "mv", "yes",
         "backup", "backups", "backupstop", "dl", "ai", "aireset",
         "aitools"]))
    async def on_link(_, message: Message):
        """纯文本：115 分享链接 -> 转存；magnet/ed2k/直链 -> 离线下载。"""
        if not _authorized(message):
            return
        text = (message.text or "").strip()
        cfg = get_config()
        if cfg.share.cookies:
            from cloud115.share import parse_share_link
            if parse_share_link(text):
                await _do_share(message, text)
                return
        kind = classify_link(text)
        if kind is None:
            await _maybe_ai(message, text)   # 普通文本 → AI 模式（未启用则忽略）
            return
        await _do_offline(message, text)

    async def _enqueue_media(message: Message, album_note: str = "") -> None:
        """单条媒体消息入队（含磁盘预检与跟踪消息）。"""
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
        log.info("入队: %s (%d bytes) -> %s%s", name, size, target_dir,
                 f" [{album_note}]" if album_note else "")

    @app.on_message(media_filter)
    async def on_media(_, message: Message):
        if not _authorized(message):
            return
        # 相册：同组消息缓冲 ALBUM_WINDOW 秒后聚合入队（保序）
        gid = getattr(message, "media_group_id", None)
        if gid:
            buf = _album_buf.get(gid)
            if buf:
                buf["msgs"].append(message)
                buf["timer"].cancel()
            else:
                buf = {"msgs": [message]}
                _album_buf[gid] = buf
            buf["timer"] = asyncio.get_event_loop().call_later(
                ALBUM_WINDOW, lambda: asyncio.ensure_future(_flush_album(gid))
            )
            return
        await _enqueue_media(message)
