"""115 客户端封装：Open115Client（手写开放平台，零 p115 依赖）的账号级薄封装。

- 每账号一个 token 文件：config/open_token_<账号名>.json（access_token/refresh_token，
  可经 utils.crypto 加密落盘）
- 上传/目录/鉴权全部走开放平台 proapi.115.com（协议见 cloud115/openapi.py）
- 旧版基于 p115client 的实现（含 cookie 模式上传）已整体移除：该生态依赖
  双 monorepo 且版本互锁，装环境极脆弱；cookie 上传接口还有 ECDH 加密，手写代价高。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from cloud115.openapi import AuthRequiredError, Open115Client
from config import AccountCfg
from utils.rate import RateLimiter

log = logging.getLogger(__name__)

__all__ = ["Cloud115Client", "Cloud115Error", "AuthRequiredError"]


class Cloud115Error(RuntimeError):
    pass


def _secret_key() -> str:
    """从全局配置取凭据加密口令（可能为空，由 crypto 退回环境变量/开发态默认）。"""
    try:
        from config import get_config
        return get_config().security.secret_key or ""
    except Exception:  # noqa: BLE001
        return ""


class Cloud115Client:
    """单账号客户端。生命周期：init() -> 使用 -> close()。"""

    def __init__(self, account: AccountCfg, session_dir: Path, rate: RateLimiter):
        self.account = account
        try:
            app_id = int(account.app_id) if account.app_id else 0
        except (TypeError, ValueError):
            app_id = 0
        token_file = Path(session_dir) / f"open_token_{account.name}.json"
        self.api = Open115Client(
            token_file, app_id=app_id, secret_key=_secret_key(), rate=rate,
        )

    async def init(self) -> None:
        await self.api.start()
        log.info("115 客户端已初始化（账号=%s, 模式=open）", self.account.name)

    async def ensure_login(self) -> bool:
        """有 token 则探活；无 token 返回 False（等待 /auth 扫码）。"""
        if not self.api.has_token():
            log.warning("账号 %s 尚未授权（无 token），请 /auth 扫码", self.account.name)
            return False
        return await self.api.check_login()

    async def close(self) -> None:
        await self.api.close()

    # 兼容旧调用点（scripts/check115.py 等）
    @property
    def raw(self) -> Open115Client:
        return self.api
