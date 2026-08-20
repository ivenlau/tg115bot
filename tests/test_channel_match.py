"""频道监控关键词匹配测试。

bot.channel_monitor 在模块顶层 import pyrogram（sandbox 未装），故此处先注入桩。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _install_pyrogram_stub() -> None:
    if "pyrogram" in sys.modules:
        return
    pyro = types.ModuleType("pyrogram")

    class _F:
        def __init__(self, name="f"):
            self.name = name

        def __or__(self, o):
            return _F(f"{self.name}|{o.name}")

        def __and__(self, o):
            return _F(f"{self.name}&{o.name}")

    class _Filters:
        def __getattr__(self, k):
            return _F(k)

    pyro.filters = _Filters()

    handlers = types.ModuleType("pyrogram.handlers")

    class _MessageHandler:
        def __init__(self, *a, **k):
            pass

    handlers.MessageHandler = _MessageHandler
    pyro.handlers = handlers

    types_mod = types.ModuleType("pyrogram.types")

    class Message:
        pass

    types_mod.Message = Message
    pyro.types = types_mod
    pyro.Client = object

    sys.modules["pyrogram"] = pyro
    sys.modules["pyrogram.handlers"] = handlers
    sys.modules["pyrogram.types"] = types_mod


_install_pyrogram_stub()

from bot.channel_monitor import extract_text, matches  # noqa: E402


def test_matches_no_whitelist_allows_all():
    assert matches("anything here", [], []) is True
    assert matches("", None, None) is True


def test_matches_whitelist_substring_case_insensitive():
    assert matches("My Great Movie 2026", ["movie"], []) is True
    assert matches("hello world", ["MOVIE"], []) is False
    assert matches("影片名", ["影片"], []) is True


def test_matches_blacklist_blocks():
    assert matches("movie trailer", ["movie"], ["trailer"]) is False
    # 黑名单即使无白名单也拦截
    assert matches("spam content", [], ["spam"]) is False


def test_matches_whitelist_and_blacklist_priority():
    # 命中白名单但含黑名单 -> 不处理
    assert matches("good movie cam", ["movie"], ["cam"]) is False
    # 命中白名单且无黑名单 -> 处理
    assert matches("good movie 4k", ["movie"], ["cam"]) is True


def test_extract_text_combines_sources():
    msg = types.SimpleNamespace(
        caption="见正文", text=None,
        video=types.SimpleNamespace(file_name="clip.mp4"),
        animation=None, audio=None, voice=None, video_note=None, document=None,
    )
    assert "见正文" in extract_text(msg)
    assert "clip.mp4" in extract_text(msg)

    msg2 = types.SimpleNamespace(
        caption=None, text="纯文本",
        video=None, animation=None, audio=None, voice=None, video_note=None,
        document=types.SimpleNamespace(file_name="doc.pdf"),
    )
    assert "纯文本" in extract_text(msg2)
    assert "doc.pdf" in extract_text(msg2)


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print(f"  ok: {fn.__name__}")
    print("test_channel_match: ALL PASS")
