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


def test_split_ranges_units():
    """并行下载分片：1 MiB 片语义（offset/limit 为片数，字节偏移对齐）。"""
    import sys as _sys
    _root = Path(__file__).resolve().parent.parent
    if str(_root) not in _sys.path:
        _sys.path.insert(0, str(_root))
    from core.downloader import STREAM_UNIT, _split_ranges

    r = _split_ranges(12 * STREAM_UNIT, 8)
    assert len(r) == 8 and sum(c for _, c, _, _ in r) == 12
    assert r[0] == (0, 1, 0, STREAM_UNIT)
    assert r[7][1] == 5 and r[7][2] == 7 * STREAM_UNIT

    r2 = _split_ranges(12 * STREAM_UNIT + 100, 2)
    assert sum(l for _, _, _, l in r2) == 12 * STREAM_UNIT + 100
    assert r2[1][3] == 6 * STREAM_UNIT + 100   # 末段吃余量

    assert _split_ranges(1000, 8) == [(0, 1, 0, 1000)]   # 小文件单段
