"""生成 Pyrogram user session 文件（用于下载 >20MB 视频）。

用法：
    python scripts/make_session.py

会交互式提示输入手机号与验证码（由 Pyrogram 处理），成功后生成：
    config/user.session
然后在 config.yaml 设置 telegram.user_session: "config/user.session" 即可。
"""
from __future__ import annotations

import asyncio

from pyrogram import Client

from bot.client import parse_proxy

from config import load_config


async def main() -> None:
    cfg = load_config()
    app = Client(
        name="user",
        api_id=cfg.telegram.api_id,
        api_hash=cfg.telegram.api_hash,
        workdir=str(cfg.session_dir),
        proxy=parse_proxy(cfg.telegram.proxy),
    )
    await app.start()
    try:
        me = await app.get_me()
        name = me.first_name or me.id
        uname = f"@{me.username}" if me.username else "(无 username)"
        print(f"✅ 登录成功：{name} ({uname})")
        print(f"✅ session 已生成：{cfg.session_dir / 'user.session'}")
        print('   请在 config.yaml 设置 telegram.user_session: "config/user.session"')
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
