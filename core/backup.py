"""频道回溯备份：整频道历史媒体批量搬进 115（断点续传）。

- /backup 启动（或续传已有备份）：get_chat_history 从断点消息 id 向旧翻页，
  媒体消息直接入上传队列（复用频道监控的关键词规则，若该频道配了规则）
- 断点持久化（last_message_id），kill/中断后重跑 /backup 自动续
- 队列积压背压：进行中任务超过阈值时等待，避免一次性灌爆
- 完成/进度通过 TG 通知；/backups 查看，/backupstop 停止
"""
from __future__ import annotations

import asyncio
import logging

from core.app import state
from core.downloader import media_info
from core.organize import organize
from core.progress import human_bytes
from core.queue import Task
from bot.channel_monitor import extract_text, matches
from persistence.models import BACKUP_DONE, BACKUP_PAUSED, BACKUP_RUNNING, SOURCE_CHANNEL

log = logging.getLogger(__name__)

HISTORY_PAGE = 100            # 每次翻页条数
BACKLOG_LIMIT = 8             # 队列积压背压阈值（进行中任务数）
NOTIFY_EVERY = 20             # 每入队 N 条发一次进度通知
PAGE_PAUSE = 1.0              # 翻页间隔（避 FloodWait）

# 运行中的备份任务（backup_id -> asyncio.Task），停止用
_running: dict = {}


async def start_backup(channel_id: int, title: str, save_path: str,
                       chat_id: int) -> tuple[bool, str]:
    """启动或续传一个频道备份。返回 (成功?, 说明)。"""
    if state.db is None or state.pyro_bot is None:
        return False, "服务未就绪"
    for bid, t in list(_running.items()):
        if not t.done():
            row = await state.db.get_backup_by_channel(channel_id)
            if row and row.id == bid:
                return False, "该频道已有备份在进行中"

    row = await state.db.get_backup_by_channel(channel_id)
    resume = False
    if row and row.status in (BACKUP_RUNNING, BACKUP_PAUSED):
        resume = True
    elif row is None:
        row = await state.db.add_backup(channel_id, title, save_path, chat_id)
        if row is None:
            return False, "创建备份记录失败"
    # status==done 或重跑已完成备份 -> 重置为 running 从头/断点继续
    await state.db.update_backup(row.id, status=BACKUP_RUNNING)

    t = asyncio.create_task(_run_backup(row.id, channel_id, save_path, chat_id, resume))
    _running[row.id] = t
    return True, ("继续备份" if resume else "备份已启动")


def stop_backup(backup_id: int) -> bool:
    t = _running.get(backup_id)
    if t and not t.done():
        t.cancel()
        return True
    return False


async def _notify(chat_id: int, text: str) -> None:
    try:
        if chat_id and state.pyro_bot is not None:
            await state.pyro_bot.send_message(chat_id, text)
    except Exception:  # noqa: BLE001
        log.debug("备份通知失败", exc_info=True)


async def _rule_for(channel_id: int):
    """若该频道配置了监控规则，返回（whitelist, blacklist）；否则(None, None)=全收。"""
    if state.db is None:
        return None, None
    rule = await state.db.get_rule(channel_id)
    if rule and rule.enabled:
        return rule.whitelist, rule.blacklist
    return None, None


async def _run_backup(backup_id: int, channel_id: int, save_path: str,
                      chat_id: int, resume: bool) -> None:
    db = state.db
    client = state.pyro_bot
    assert db is not None and client is not None
    try:
        row = await db.get_backup_by_channel(channel_id)
        assert row is not None
        offset_id = row.last_message_id if resume and row.last_message_id else 0
        wl, bl = await _rule_for(channel_id)
        cfg = state.config
        done = row.total_done
        skipped = row.skipped
        notify_counter = 0

        await _notify(chat_id, f"▶️ 频道备份{'（续传）' if resume else ''}: {row.title or channel_id}\n"
                               f"📁 {save_path}\n从消息 #{offset_id or '最新'} 开始向历史回溯")

        empty_pages = 0
        while True:
            # 背压：进行中任务过多时等待
            while len(state.task_progress) >= BACKLOG_LIMIT:
                await asyncio.sleep(5)

            msgs = []
            async for m in client.get_chat_history(channel_id, limit=HISTORY_PAGE,
                                                    offset_id=offset_id):
                msgs.append(m)
            if not msgs:
                empty_pages += 1
                if empty_pages >= 2:
                    break          # 连续两页空 = 到头了
                await asyncio.sleep(PAGE_PAUSE)
                continue
            empty_pages = 0

            for m in msgs:
                name, size = media_info(m)
                if not size and not m.photo:
                    skipped += 1
                    continue
                # 关键词规则过滤（配置了才生效）
                if wl is not None:
                    text = extract_text(m)
                    if not matches(text, wl, bl):
                        skipped += 1
                        continue
                final_name, final_dir = organize(
                    name, save_path,
                    rename_template=cfg.organize.rename_template,
                    classify_by_ext=cfg.organize.classify_by_ext, size=size,
                )
                tracking = await client.send_message(
                    chat_id,
                    f"📦 备份入队 {done + 1}\n📄 {final_name}\n"
                    f"📦 {human_bytes(size)}\n📁 {final_dir}",
                )
                task = Task(
                    user_id=chat_id, message=m, filename=final_name, size=size,
                    target_dir=final_dir,
                    tracking_chat_id=tracking.chat.id,
                    tracking_message_id=tracking.id,
                    source=SOURCE_CHANNEL, channel_id=channel_id,
                )
                state.register_task(task)
                await state.queue.put(task)
                done += 1
                notify_counter += 1
                offset_id = m.id          # 断点推进

            await db.update_backup(backup_id, last_message_id=offset_id,
                                   total_done=done, skipped=skipped)
            if notify_counter >= NOTIFY_EVERY:
                notify_counter = 0
                await _notify(chat_id,
                              f"⏳ 备份进度: 已入队 {done}，跳过 {skipped}，当前消息 #{offset_id}")
            await asyncio.sleep(PAGE_PAUSE)

        await db.update_backup(backup_id, status=BACKUP_DONE,
                               total_done=done, skipped=skipped)
        await _notify(chat_id,
                      f"✅ 频道备份完成: {row.title or channel_id}\n"
                      f"共入队 {done} 个媒体，跳过 {skipped} 条\n📁 {save_path}")
        log.info("频道备份完成 %s: %d 入队", channel_id, done)
    except asyncio.CancelledError:
        await db.update_backup(backup_id, status=BACKUP_PAUSED)
        await _notify(chat_id, "⏸ 备份已暂停（进度已保存），重发 /backup 可续传")
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("频道备份异常 %s", channel_id)
        await db.update_backup(backup_id, status=BACKUP_PAUSED)
        await _notify(chat_id, f"⚠️ 备份出错已暂停（进度已保存）: {e}")
    finally:
        _running.pop(backup_id, None)
