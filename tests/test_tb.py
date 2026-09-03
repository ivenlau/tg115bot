"""tb 包纯逻辑测试：服务 PID 身份校验、mihomo 配置解析、菜单表渲染、CLI 入口。

不触网：115 命令走 bridge→manual→cloud115（需真实授权），只在用户机上验证。
"""
from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 依赖桩（沙箱未装时注入；真实环境直接用真包）——仿 test_manual_tools
import types
try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    _aio = types.ModuleType("aiohttp")
    _aio.ClientSession = type("ClientSession", (), {"__init__": lambda self, *a, **k: None})
    _aio.ClientTimeout = lambda **k: None
    _aio.ClientError = Exception
    sys.modules["aiohttp"] = _aio
try:
    import aiofiles  # noqa: F401
except ModuleNotFoundError:
    _af = types.ModuleType("aiofiles")
    _af.open = lambda *a, **k: None
    sys.modules["aiofiles"] = _af

from tb import service, ops        # noqa: E402
import tb.menu as tbmenu           # noqa: E402


def test_live_pid_identity():
    """PID 校验：命令行含安装目录才认（防 PID 复用误杀），无关进程不认。"""
    tmp = Path(tempfile.mkdtemp())
    old = service.PID_FILE
    service.PID_FILE = tmp / "t.pid"
    good = bad = None
    try:
        # 命令行含「安装目录 + main.py」的子进程 -> 认（-c 里带路径与 main.py 标记）
        code = (f"import sys,time; sys.path.insert(0, {str(service.INSTALL_DIR)!r}); "
                f"time.sleep(60)  # main.py")
        good = subprocess.Popen([sys.executable, "-c", code])
        time.sleep(0.6)
        service.PID_FILE.write_text(f"{good.pid}\n", encoding="ascii")
        assert service.live_pid() == good.pid

        # venv python 但非 main.py（如 manual.py/check115.py 同款形态）-> 不认
        bad = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        time.sleep(0.6)
        service.PID_FILE.write_text(f"{bad.pid}\n", encoding="ascii")
        assert service.live_pid() is None

        # 脏内容 / 不存在 -> None，不抛
        service.PID_FILE.write_text("not-a-pid\n", encoding="ascii")
        assert service.live_pid() is None
        service.PID_FILE.unlink()
        assert service.live_pid() is None
    finally:
        for p in (good, bad):
            if p is not None:
                p.kill()
                p.wait()
        service.PID_FILE = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_fmt_uptime():
    assert service._fmt_uptime(3661) == "01:01:01"
    assert service._fmt_uptime(90061) == "1d 01:01:01"


def test_mihomo_cfg_parse():
    tmp = Path(tempfile.mkdtemp())
    try:
        f = tmp / "config.yaml"
        f.write_text(
            "mixed-port: 7891\n"
            "allow-lan: true\n"
            "external-controller: '0.0.0.0:9090'\n"
            "proxies: []\n", encoding="utf-8")
        mi = ops._read_mihomo_cfg(f)
        assert mi["mixed-port"] == "7891"
        assert mi["external-controller"] == "0.0.0.0:9090"
        assert mi["allow-lan"] == "true"
        assert ops._read_mihomo_cfg(tmp / "none.yaml") == {}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_menu_table_renders():
    from rich.console import Console

    buf = io.StringIO()
    c = Console(file=buf, width=110, force_terminal=False)
    c.print(tbmenu._menu_table())
    out = buf.getvalue()
    assert "退出" in out and "上传" in out and "离线" in out
    # 每个菜单项的编号都在
    from scripts.manual import MENU
    for m in MENU:
        assert str(m.num) in out


def test_cli_help_and_version():
    from typer.testing import CliRunner
    from tb.cli import app

    r = CliRunner().invoke(app, ["--help"])
    assert r.exit_code == 0
    for word in ("start", "doctor", "offline", "upload", "share", "auth", "update"):
        assert word in r.output, word
    r = CliRunner().invoke(app, ["--version"])
    assert r.exit_code == 0 and "tb 1" in r.output
    # 子命令嵌套
    r = CliRunner().invoke(app, ["offline", "--help"])
    assert r.exit_code == 0 and "add" in r.output and "del" in r.output


def test_cloud_loop_single_loop_for_session():
    """回归：aiohttp 会话必须在创建它的同一循环上使用。

    旧 bug：ctx 在某次 asyncio.run() 的临时循环上创建（循环随即关闭），页面
    worker 再用新临时循环发请求 → 连环 "Event loop is closed"。
    _CloudLoop 常驻循环下两次 submit 共用一个循环 → 不再出现该错误。
    """
    import aiohttp
    from tb.tui import _CloudLoop

    async def _make():
        return aiohttp.ClientSession()

    async def _hit(s):
        try:
            async with s.get("http://127.0.0.1:9/"):
                pass
            return "ok"
        except RuntimeError as e:
            return f"runtime:{e}"        # Event loop is closed 会落在这里（先判）
        except Exception:
            return "client-error"        # 连接拒绝等——请求调度路径已走通

    cl = _CloudLoop()
    try:
        sess = cl.submit(_make()).result(10)
        r1 = cl.submit(_hit(sess)).result(10)
        r2 = cl.submit(_hit(sess)).result(10)   # 再来一次，覆盖多次提交
        assert not r1.startswith("runtime"), r1
        assert not r2.startswith("runtime"), r2

        async def _close():
            if hasattr(sess, "close"):        # 桩环境的假 session 没有 close
                await sess.close()
        cl.submit(_close()).result(5)
    finally:
        cl.stop()


def test_token_reload_if_disk_newer():
    """跨进程 token 轮换检测（bot + tb/TUI 共用 token 文件场景）。

    实案 2026-09-03：TUI 进程刷新 token 吊销了 bot 内存里的 token，bot 收到
    40140126 直接失败。_reload_token_if_disk_newer 让 bot 捡起磁盘新 token。
    """
    import json
    import os
    from cloud115.openapi import Open115Client
    from utils.rate import RateLimiter

    tmp = Path(tempfile.mkdtemp())
    try:
        f = tmp / "open_token_main.json"
        f.write_text(json.dumps({"access_token": "A", "refresh_token": "R1"}),
                     encoding="utf-8")
        c = Open115Client(f, app_id=0, secret_key="", rate=RateLimiter(0))
        c._load_token()
        assert c.access_token == "A"
        assert not c._reload_token_if_disk_newer()      # mtime 一致 -> 没人动过

        # 模拟另一进程写新 token（内容 + mtime 都变）
        f.write_text(json.dumps({"access_token": "B", "refresh_token": "R2"}),
                     encoding="utf-8")
        future = time.time_ns() + 10 ** 9
        os.utime(f, ns=(future, future))
        assert c._reload_token_if_disk_newer()          # 捡起新 token
        assert c.access_token == "B" and c.refresh_token == "R2"
        assert not c._reload_token_if_disk_newer()      # 已同步

        # 自己写盘后 mtime 记账（不算「别人更新」）；文件消失不炸
        c._save_token()
        assert not c._reload_token_if_disk_newer()
        f.unlink()
        assert not c._reload_token_if_disk_newer()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def test_make_db_progress_throttle():
    """进度落库节流：≥refresh 秒或到终点才写；base_cb 每次都透传。"""
    import asyncio
    from core.pipeline import make_db_progress

    ticks = [1000.0]                       # 仿真实 monotonic（首调立即落库）
    base_calls, writes = [], []

    async def base(cur, total):
        base_calls.append((cur, total))

    async def update(task, **kw):
        writes.append(kw.get("progress"))

    cb = make_db_progress(object(), base_cb=base, refresh=3.0,
                          clock=lambda: ticks[0], update=update)

    async def go():
        await cb(10, 100)     # 首调 → 落库 10
        await cb(20, 100)     # 同刻 → 节流跳过
        ticks[0] = 1002.9
        await cb(50, 100)     # <3s → 跳过
        ticks[0] = 1003.1
        await cb(80, 100)     # ≥3s → 落库 80
        await cb(100, 100)    # 终点 → 立即落库 100
        await cb(30, 0)       # total=0（未知）→ 不落库
    asyncio.run(go())
    assert writes == [10, 80, 100], writes
    assert len(base_calls) == 6


def test_config_edit_helpers():
    """validate/write/set_config_key：双重校验、备份、失败不落盘。"""
    from tb import ops

    good = ("telegram:\n"
            "  api_id: 1234567\n"
            "  api_hash: \"abc\"\n"
            "  bot_token: \"123:ABC\"\n"
            "  proxy: \"\"\n"
            "upload:\n  target_dir: /tg115bot\n"
            "web:\n  enable: false\n")
    ok, msg = ops.validate_config_text(good)
    assert ok, msg
    ok, _ = ops.validate_config_text("telegram: [unclosed")
    assert not ok                                    # YAML 语法错
    ok, _ = ops.validate_config_text("telegram: 123")
    assert not ok                                    # pydantic 类型错

    tmp = Path(tempfile.mkdtemp())
    try:
        p = tmp / "config.yaml"
        p.write_text(good, encoding="utf-8")
        # 全文写回：自动备份 + 原文落盘（含尾随换行）
        ops.write_config_text(good.rstrip("\n"), p)
        baks = list(tmp.glob("config.yaml.bak.*"))
        assert len(baks) == 1 and p.read_text(encoding="utf-8").endswith("\n")
        # 改键：布尔落盘正确
        ok, msg = ops.set_config_key("web.enable", True, p)
        assert ok, msg
        import yaml
        assert yaml.safe_load(p.read_text(encoding="utf-8"))["web"]["enable"] is True
        # 改键非法值：校验失败、文件不变
        before = p.read_text(encoding="utf-8")
        ok, _ = ops.set_config_key("web.enable", "不是布尔", p)
        assert not ok
        assert p.read_text(encoding="utf-8") == before
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  ok: {name}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  FAIL: {name}: {e!r}")
    sys.exit(1 if fails else 0)
