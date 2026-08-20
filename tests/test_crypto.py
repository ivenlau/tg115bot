"""凭据加密测试（cryptography 已安装）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import utils.crypto as crypto  # noqa: E402


def setup_function(_fn):
    # 每个用例前重置缓存的 Fernet，确保口令生效
    crypto._fernet = None
    os.environ.pop("TG115BOT_SECRET_KEY", None)


def test_roundtrip_with_explicit_key():
    enc = crypto.encrypt("hello 115", secret_key="my-pass")
    assert enc != "hello 115"
    assert crypto.decrypt(enc, secret_key="my-pass") == "hello 115"


def test_same_passphrase_same_ciphertext_key():
    # 同口令派生同 key（密文随机含 IV，故比较 key 而非密文）
    k1 = crypto._derive_key("pass")
    k2 = crypto._derive_key("pass")
    assert k1 == k2
    assert crypto._derive_key("pass") != crypto._derive_key("other")


def test_wrong_passphrase_fails():
    enc = crypto.encrypt("secret", secret_key="right")
    crypto._fernet = None
    try:
        crypto.decrypt(enc, secret_key="wrong")
    except Exception:  # noqa: BLE001 -- 期望抛错
        return
    raise AssertionError("错误口令应解密失败")


def test_decrypt_if_possible_passthrough_plaintext():
    # 旧明文 token 应原样返回（平滑迁移）
    assert crypto.decrypt_if_possible("plain-token-not-encrypted", secret_key="k") == "plain-token-not-encrypted"
    assert crypto.decrypt_if_possible("", secret_key="k") == ""
    # 真密文应能解出
    enc = crypto.encrypt("real", secret_key="k")
    crypto._fernet = None
    assert crypto.decrypt_if_possible(enc, secret_key="k") == "real"


def test_env_var_passphrase():
    os.environ["TG115BOT_SECRET_KEY"] = "env-pass"
    crypto._fernet = None
    enc = crypto.encrypt("data")
    crypto._fernet = None
    assert crypto.decrypt(enc) == "data"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        setup_function(fn)
        fn()
        print(f"  ok: {fn.__name__}")
    print("test_crypto: ALL PASS")
