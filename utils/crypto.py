"""凭据加密：Fernet 对称加密，密钥由口令派生。

口令来源（优先级）：
  1. config.security.secret_key
  2. 环境变量 TG115BOT_SECRET_KEY
  3. 开发态默认口令（仅本机调试，打印告警）

用途：加密落盘的 115 token（config/open_token_<账号名>.json）。
解密兼容旧明文：``decrypt_if_possible`` 遇到非密文原样返回，便于平滑迁移。

Fernet 要求 key 为 url-safe base64 编码的 32 字节。这里用 SHA256(口令) 派生，
确定性、可跨进程复现（同一口令 -> 同一 key）。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Optional

from cryptography.fernet import Fernet

log = logging.getLogger(__name__)

_DEV_PASSPHRASE = "tg115bot-dev-only-not-secure"
_fernet: Optional[Fernet] = None


def _passphrase(secret_key: Optional[str] = None) -> str:
    if secret_key:
        return secret_key
    env = os.environ.get("TG115BOT_SECRET_KEY", "").strip()
    if env:
        return env
    log.warning("未配置 secret_key/TG115BOT_SECRET_KEY，使用开发态默认口令（生产环境务必配置！）")
    return _DEV_PASSPHRASE


def _derive_key(passphrase: str) -> bytes:
    digest = hashlib.sha256(passphrase.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def get_fernet(secret_key: Optional[str] = None) -> Fernet:
    """获取（并缓存）Fernet 实例。同一进程内口令不变则复用。"""
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_derive_key(_passphrase(secret_key)))
    return _fernet


def encrypt(plaintext: str, secret_key: Optional[str] = None) -> str:
    """加密为 token 字符串。"""
    return get_fernet(secret_key).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str, secret_key: Optional[str] = None) -> str:
    """解密 token。失败抛 InvalidToken。"""
    return get_fernet(secret_key).decrypt(token.encode("utf-8")).decode("utf-8")


def decrypt_if_possible(value: str, secret_key: Optional[str] = None) -> str:
    """解密；若 value 不是有效密文（如旧明文），原样返回。便于平滑迁移。"""
    if not value:
        return value
    try:
        return decrypt(value, secret_key)
    except Exception:  # noqa: BLE001 -- InvalidToken 或其他异常都退回原值（兼容旧明文）
        return value
