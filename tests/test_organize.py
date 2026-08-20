"""core.organize 纯逻辑测试。"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.organize import (  # noqa: E402
    classify_subdir, organize, render_name,
)


def test_classify_subdir():
    assert classify_subdir("a.mp4") == "videos"
    assert classify_subdir("Archive.MKV") == "videos"   # 大小写不敏感
    assert classify_subdir("song.flac") == "audio"
    assert classify_subdir("pic.JPEG") == "images"
    assert classify_subdir("book.pdf") == "documents"
    assert classify_subdir("x.zip") == "archives"
    assert classify_subdir("noext") == "others"
    assert classify_subdir("weird.xyz") == "others"


def test_render_name_basic():
    now = datetime(2026, 8, 13, 14, 30, 5)
    assert render_name("{filename}", "movie.mp4", now=now) == "movie.mp4"
    assert render_name("{name}.{ext}", "movie.mp4", now=now) == "movie..mp4"  # ext 含点
    assert render_name("{name}{ext}", "movie.mp4", now=now) == "movie.mp4"
    assert render_name("{date}_{filename}", "movie.mp4", now=now) == "20260813_movie.mp4"
    assert render_name("{datetime}", "x", now=now) == "20260813_143005"


def test_render_name_missing_var_is_empty():
    # 模板引用未提供变量 -> 渲染为空，不抛异常
    assert render_name("{unknown}_{filename}", "a.mp4") == "_a.mp4"


def test_render_name_malformed_template_passthrough():
    assert render_name("{filename", "a.mp4") == "{filename"   # 非法格式原样返回


def test_organize_no_classify():
    name, d = organize("movie.mp4", "/tg115bot", rename_template="{filename}", classify_by_ext=False)
    assert name == "movie.mp4"
    assert d == "/tg115bot"


def test_organize_classify_adds_subdir():
    name, d = organize("movie.mp4", "/tg115bot", classify_by_ext=True)
    assert name == "movie.mp4"
    assert d == "/tg115bot/videos"


def test_organize_template_keeps_ext_when_missing():
    # 模板丢掉扩展名时应补回，避免 115 端识别错误
    name, d = organize("movie.mp4", "/tg115bot",
                       rename_template="{date}_{name}", classify_by_ext=True,
                       now=datetime(2026, 8, 13, 0, 0, 0))
    assert name == "20260813_movie.mp4"
    assert d == "/tg115bot/videos"


def test_organize_sanitizes_illegal_chars():
    name, d = organize("bad/name?:*.mp4", "/tg115bot", rename_template="{filename}")
    assert "/" not in name and "?" not in name and "*" not in name
    assert name.endswith(".mp4")


def test_organize_empty_target_with_classify():
    name, d = organize("a.mp3", "", classify_by_ext=True)
    assert d == "audio"


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print(f"  ok: {fn.__name__}")
    print("test_organize: ALL PASS")
