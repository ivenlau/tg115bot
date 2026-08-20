"""构建 Pyrogram 客户端：bot + 可选 user session。

user session 用于下载 >20MB 视频（Bot API 上限 20MB；MTProto bot 也有额度限制），
额度更高、更不易触发 FloodWait。生成方式见 scripts/make_session.py。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from pyrogram import Client

from config import AppConfig

log = logging.getLogger(__name__)


def build_bot(cfg: AppConfig) -> Client:
    return Client(
        name="tg115bot_bot",
        api_id=cfg.telegram.api_id,
        api_hash=cfg.telegram.api_hash,
        bot_token=cfg.telegram.bot_token,
        workdir=str(cfg.session_dir),
    )


def build_user(cfg: AppConfig) -> Optional[Client]:
    """根据 config.telegram.user_session 构建 user 客户端；未配置返回 None。"""
    raw = (cfg.telegram.user_session or "").strip()
    if not raw:
        return None

    if raw.endswith(".session"):
        p = Path(raw)
        if not p.is_absolute():
            p = (cfg.session_dir.parent / p).resolve() if not p.exists() else p.resolve()
        if not p.exists():
            log.warning("user session 文件不存在: %s（>20MB 视频将无法下载）", p)
        return Client(
            name=p.stem,
            api_id=cfg.telegram.api_id,
            api_hash=cfg.telegram.api_hash,
            workdir=str(p.parent),
        )

    # 否则当作 session 字符串
    log.info("使用 session 字符串创建 user 客户端")
    return Client(
        name="tg115bot_user",
        api_id=cfg.telegram.api_id,
        api_hash=cfg.telegram.api_hash,
        session_string=raw,
        workdir=str(cfg.session_dir),
    )
