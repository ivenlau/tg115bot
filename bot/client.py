"""构建 Pyrogram 客户端：bot + 可选 user session。

user session 用于提升下载额度（bot 账号易触发 FloodWait）；Premium user 可下 4GB，
bot 走 MTProto 也可达 2GB。生成方式见 scripts/make_session.py。

代理（config.telegram.proxy，国内服务器访问 TG 必需）：
  - "socks5://127.0.0.1:7891" / "http://127.0.0.1:7890"；空=直连
  - 只作用于 TG（bot/user 两个客户端）；115 与 OSS 上传不走代理（国内直连更快）
  - socks 需要依赖 python-socks（requirements 已含）
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

from pyrogram import Client

from config import AppConfig

log = logging.getLogger(__name__)


def parse_proxy(proxy: str) -> Optional[Dict[str, str]]:
    """把 "socks5://host:port" / "http://host:port" 解析为 Pyrogram 的 proxy dict。

    Pyrogram 约定：{"scheme": ..., "hostname": ..., "port": int, "username":..., "password":...}
    scheme: socks4 / socks5 / http（socks5h 由 python-socks 处理远端解析，此处归一为 socks5）。
    """
    raw = (proxy or "").strip()
    if not raw:
        return None
    from urllib.parse import urlsplit
    parts = urlsplit(raw if "://" in raw else f"http://{raw}")
    scheme = (parts.scheme or "http").lower()
    if scheme in ("socks5h", "socks4a"):
        scheme = "socks5" if scheme == "socks5h" else "socks4"
    if scheme not in ("socks4", "socks5", "http"):
        raise ValueError(f"不支持的代理 scheme: {scheme!r}（socks4/socks5/http）")
    if not parts.hostname or not parts.port:
        raise ValueError(f"代理地址缺少 host/port: {proxy!r}")
    d: Dict[str, object] = {
        "scheme": scheme,
        "hostname": parts.hostname,
        "port": parts.port,           # Pyrogram/PySocks 要求 int
    }
    if parts.username:
        d["username"] = parts.username
    if parts.password:
        d["password"] = parts.password
    log.info("TG 客户端启用代理: %s://%s:%s", scheme, d["hostname"], d["port"])
    return d


def build_bot(cfg: AppConfig) -> Client:
    return Client(
        name="tg115bot_bot",
        api_id=cfg.telegram.api_id,
        api_hash=cfg.telegram.api_hash,
        bot_token=cfg.telegram.bot_token,
        workdir=str(cfg.session_dir),
        proxy=parse_proxy(cfg.telegram.proxy),
    )


def build_user(cfg: AppConfig) -> Optional[Client]:
    """根据 config.telegram.user_session 构建 user 客户端；未配置返回 None。"""
    raw = (cfg.telegram.user_session or "").strip()
    if not raw:
        return None
    proxy = parse_proxy(cfg.telegram.proxy)

    if raw.endswith(".session"):
        p = Path(raw)
        if not p.is_absolute():
            p = (cfg.session_dir.parent / p).resolve() if not p.exists() else p.resolve()
        if not p.exists():
            log.warning("user session 文件不存在: %s（将退回 bot 下载）", p)
        return Client(
            name=p.stem,
            api_id=cfg.telegram.api_id,
            api_hash=cfg.telegram.api_hash,
            workdir=str(p.parent),
            proxy=proxy,
        )

    # 否则当作 session 字符串
    log.info("使用 session 字符串创建 user 客户端")
    return Client(
        name="tg115bot_user",
        api_id=cfg.telegram.api_id,
        api_hash=cfg.telegram.api_hash,
        session_string=raw,
        workdir=str(cfg.session_dir),
        proxy=proxy,
    )
