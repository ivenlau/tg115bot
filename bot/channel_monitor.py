"""频道监控：订阅频道 -> 关键词白/黑名单匹配 -> 命中则投递上传任务。

规则存于 persistence（``channel_rules`` 表），由 Web 台或 /addchannel 命令维护。
bot 需以成员/管理员身份加入目标频道才能收到消息。

匹配语义（``matches`` 为纯函数，便于单测）：
  - 黑名单命中 -> 直接不处理
  - 白名单为空 -> 允许（即"该频道所有媒体"）
  - 白名单非空 -> 文本须含至少一个白名单关键词（大小写不敏感子串匹配）

文本来源：caption + text + 媒体 file_name 拼接。
"""
from __future__ import annotations

import logging
from typing import List, Optional

from pyrogram import filters
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

from config import get_config
from core.app import state
from core.downloader import media_info
from core.organize import organize
from core.progress import human_bytes
from core.queue import Task, TaskCancelled  # noqa: F401  (TaskCancelled 复用于取消语义)
from persistence.models import SOURCE_CHANNEL
from utils.rate import with_flood_wait

log = logging.getLogger(__name__)

# 频道消息里关心的媒体类型
_MEDIA_FILTER = (
    filters.video | filters.animation | filters.audio | filters.voice
    | filters.video_note | filters.document
) & filters.channel


def matches(text: str, whitelist: Optional[List[str]], blacklist: Optional[List[str]]) -> bool:
    """纯函数：根据白/黑名单判断文本是否命中。"""
    text_lower = (text or "").lower()
    bl = [b for b in (blacklist or []) if b]
    if bl and any(b.lower() in text_lower for b in bl):
        return False
    wl = [w for w in (whitelist or []) if w]
    if not wl:
        return True
    return any(w.lower() in text_lower for w in wl)


def extract_text(message: Message) -> str:
    """从消息提取可匹配文本：caption + text + 媒体文件名。"""
    parts: List[str] = []
    if getattr(message, "caption", None):
        parts.append(message.caption)
    if getattr(message, "text", None):
        parts.append(message.text)
    for attr in ("video", "animation", "audio", "voice", "video_note", "document"):
        m = getattr(message, attr, None)
        if m and getattr(m, "file_name", None):
            parts.append(m.file_name)
    return " ".join(parts)


class ChannelMonitor:
    """频道规则内存缓存 + 消息处理。``reload`` 从 DB 重建缓存。"""

    def __init__(self):
        self._rules: dict = {}  # channel_id -> ChannelRuleRow
        self._handler = None

    async def reload(self) -> None:
        if state.db is None:
            self._rules = {}
            return
        rules = await state.db.list_rules()
        self._rules = {r.channel_id: r for r in rules if r.enabled}
        log.info("频道规则已加载: %d 条", len(self._rules))

    def register(self, app) -> None:
        """注册 Pyrogram 消息处理器（频道媒体消息）。"""
        self._handler = MessageHandler(self._on_message, _MEDIA_FILTER)
        app.add_handler(self._handler)

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    async def _on_message(self, client, message: Message) -> None:
        try:
            await self._handle(client, message)
        except Exception:  # noqa: BLE001 -- 监控异常不应影响 bot
            log.exception("频道消息处理异常: chat=%s", getattr(message.chat, "id", None))

    async def _handle(self, client, message: Message) -> None:
        chat_id = message.chat.id if message.chat else 0
        rule = self._rules.get(chat_id)
        if rule is None:
            return  # 未订阅该频道
        if not rule.enabled:
            return

        text = extract_text(message)
        if not matches(text, rule.whitelist, rule.blacklist):
            return

        name, size = media_info(message)
        if not size:
            log.debug("频道消息无文件大小，跳过: %s", name)
            return

        cfg = get_config()
        base_dir = rule.target_dir or cfg.channel_monitor.default_target_dir or cfg.upload.target_dir
        # 频道任务同样走整理（重命名/归类）
        final_name, final_dir = organize(
            name, base_dir,
            rename_template=cfg.organize.rename_template,
            classify_by_ext=cfg.organize.classify_by_ext,
            size=size,
        )

        notify_chat = cfg.notify_chat_id
        try:
            if notify_chat:
                tracking = await with_flood_wait(lambda: client.send_message(
                    notify_chat,
                    f"📡 频道任务\n📄 {final_name}\n📦 {human_bytes(size)}\n"
                    f"📁 {final_dir}\n👤 来源: {message.chat.title or chat_id}",
                ))
            else:
                tracking = await with_flood_wait(lambda: message.reply_text(
                    f"📡 频道任务\n📄 {final_name}\n📦 {human_bytes(size)}\n📁 {final_dir}"
                ))
        except Exception as e:  # noqa: BLE001
            log.warning("频道任务跟踪消息发送失败: %r", e)
            return

        task = Task(
            user_id=notify_chat or 0,
            message=message,
            filename=final_name,
            size=size,
            target_dir=final_dir,
            tracking_chat_id=tracking.chat.id,
            tracking_message_id=tracking.id,
            source=SOURCE_CHANNEL,
            channel_id=chat_id,
        )
        state.register_task(task)
        await state.queue.put(task)
        log.info("频道入队: %s (%d bytes) from %s -> %s", final_name, size, chat_id, final_dir)
