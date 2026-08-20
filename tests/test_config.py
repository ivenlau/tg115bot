"""配置加载测试：yaml.example 解析 + 环境变量覆盖 + 新段存在性。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import AppConfig, load_config  # noqa: E402

EXAMPLE = ROOT / "config.yaml.example"


def test_example_loads():
    cfg = load_config(EXAMPLE)
    assert isinstance(cfg, AppConfig)
    assert cfg.telegram.api_id == 1234567
    assert cfg.upload.target_dir == "/tg115bot"


def test_new_sections_present():
    cfg = load_config(EXAMPLE)
    # Phase 3-4 新增段都有默认值
    assert hasattr(cfg, "channel_monitor")
    assert hasattr(cfg, "logging")
    assert hasattr(cfg, "security")
    assert cfg.logging.level.upper() in ("DEBUG", "INFO", "WARNING", "ERROR")
    assert cfg.logging.db_buffer > 0


def test_env_override_simple():
    os.environ["TG115BOT__WEB__PORT"] = "9090"
    os.environ["TG115BOT__WEB__ENABLE"] = "true"
    try:
        cfg = load_config(EXAMPLE)
        assert cfg.web.port == 9090
        assert cfg.web.enable is True
    finally:
        del os.environ["TG115BOT__WEB__PORT"]
        del os.environ["TG115BOT__WEB__ENABLE"]


def test_env_override_array_index():
    os.environ["TG115BOT__ACCOUNTS__0__NAME"] = "alt"
    os.environ["TG115BOT__ACCOUNTS__0__WEIGHT"] = "3"
    try:
        cfg = load_config(EXAMPLE)
        assert cfg.accounts[0].name == "alt"
        assert cfg.accounts[0].weight == 3
    finally:
        del os.environ["TG115BOT__ACCOUNTS__0__NAME"]
        del os.environ["TG115BOT__ACCOUNTS__0__WEIGHT"]


def test_notify_chat_id_fallback():
    cfg = load_config(EXAMPLE)
    # example allowed_users 为空、notify_chat_id 为 0 -> 回退 0
    assert cfg.notify_chat_id == 0


def test_db_path_property():
    cfg = load_config(EXAMPLE)
    p = cfg.db_path
    assert p.name == "tg115bot.db"
    assert p.parent.exists()


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print(f"  ok: {fn.__name__}")
    print("test_config: ALL PASS")
