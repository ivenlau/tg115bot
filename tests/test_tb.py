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


def test_tui_app_headless():
    """Textual 无头启动：组合/翻页不炸（数据层不可用时应优雅降级）。"""
    import asyncio
    from tb.tui import TBApp

    async def go():
        app = TBApp()
        async with app.run_test(size=(110, 32)) as pilot:
            await pilot.pause(0.3)
            assert app.query("#content DashboardPage")
            lv = app.query_one("#nav")
            for idx, expect in ((3, "LogPage"), (4, "AuthPage"), (1, "FilesPage")):
                lv.index = idx
                await pilot.pause(0.4)
                assert app.query(f"#content {expect}"), expect
    asyncio.run(go())


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
