"""TUI 集成测试：每个用例在独立子进程中跑（Textual run_test 无头）。

为什么子进程隔离：单进程内顺序跑多个 TBApp 时，「上一个 app 退出清理」与
「真实网络初始化(115/DB)/探活子进程」会累积出 teardown 竞态（卡 .result、
cloud 线程生命周期交叠）——与真实「一进程一 app」的形态不符且不定。
每用例一个子进程 = 与真实使用同构，彻底消除串扰。

用法：python tests/test_tui.py            # 全部（每用例起子进程）
      python tests/test_tui.py _case_xxx  # 直接跑单个用例（子进程模式）
"""
from __future__ import annotations

import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CASE_TIMEOUT = 120   # 单用例上限（含真实 115 初始化/探活的网络耗时）


def _run_case(name: str) -> tuple[bool, str]:
    r = subprocess.run([sys.executable, str(Path(__file__)), name],
                       capture_output=True, text=True, timeout=CASE_TIMEOUT,
                       cwd=str(ROOT))
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0, out


def test_tui_suite() -> None:
    cases = [n for n in sorted(globals()) if n.startswith("_case_")]
    assert cases, "未发现用例"
    fails = 0
    for name in cases:
        try:
            ok, out = _run_case(name)
        except subprocess.TimeoutExpired:
            ok, out = False, f"{name} 超时（>{CASE_TIMEOUT}s）"
        print(f"  {'ok' if ok else 'FAIL'}: {name}")
        if not ok:
            fails += 1
            tail = "\n".join(out.splitlines()[-12:])
            print(f"      {tail}")
    assert fails == 0, f"{fails} 个 TUI 用例失败"


# ══════════ 用例本体（仅在子进程中执行）══════════


def _case_tui_app_headless() -> None:
    """无头启动：组合/翻页不炸（数据层不可用时应优雅降级）。"""
    import asyncio
    from tb.tui import TBApp

    async def go():
        app = TBApp()
        async with app.run_test(size=(110, 32)) as pilot:
            await pilot.pause(0.3)
            assert app.query("#content DashboardPage")
            lv = app.query_one("#nav")
            for idx, expect in ((3, "LogPage"), (4, "ConfigPage"), (1, "FilesPage")):
                lv.index = idx
                await pilot.pause(0.4)
                assert app.query(f"#content {expect}"), expect
    asyncio.run(go())


def _case_files_page_duplicate_names_ok() -> None:
    """同名条目（115 递归混入形态）不得炸 DuplicateKey；.. 行可回上级。"""
    import asyncio
    from tb.tui import TBApp

    async def go():
        app = TBApp()
        async with app.run_test(size=(110, 32)) as pilot:
            await pilot.pause(0.2)
            app.query_one("#nav").index = 1
            await pilot.pause(0.5)
            page = app.query_one("#content FilesPage")
            t = page.query_one("#files")
            t.clear()
            t.add_row("📂", "..", "")
            t.add_row("📄", "same.mkv", "1G")
            t.add_row("📄", "same.mkv", "2G")
            assert t.row_count == 3
    asyncio.run(go())


def _case_files_page_action_inputs() -> None:
    """通用动作弹框：s 下载 / n 重命名（预填旧名）/ Esc 收起。"""
    import asyncio
    from tb.tui import TBApp

    async def go():
        app = TBApp()
        async with app.run_test(size=(110, 32)) as pilot:
            await pilot.pause(0.2)
            app.query_one("#nav").index = 1
            await pilot.pause(0.5)
            page = app.query_one("#content FilesPage")
            t = page.query_one("#files")
            t.clear()
            t.add_row("📄", "a.mkv", "1G")
            t.move_cursor(row=0)
            t.focus()
            inp = page.query_one("#action-input")
            assert inp.has_class("hidden")
            await pilot.press("s")
            await pilot.pause(0.2)
            assert not inp.has_class("hidden"), "s 应弹出下载输入框"
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert inp.has_class("hidden")
            await pilot.press("n")
            await pilot.pause(0.2)
            assert not inp.has_class("hidden") and inp.value == "a.mkv", \
                "n 应弹出重命名输入框并预填旧名"
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert inp.has_class("hidden")
    asyncio.run(go())


def _case_dashboard_service_buttons_stub() -> None:
    """仪表盘服务按钮：打桩 do_start/do_stop，按钮触发即调用（不真启停）。"""
    import asyncio
    from tb import service as tb_service
    from tb.tui import TBApp

    calls = []
    orig = {n: getattr(tb_service, n) for n in ("do_start", "do_stop", "do_restart")}
    tb_service.do_start = lambda: (calls.append("start"), 0)[1]
    tb_service.do_stop = lambda: (calls.append("stop"), 0)[1]
    try:
        async def go():
            from textual.widgets import Button
            app = TBApp()
            async with app.run_test(size=(110, 34)) as pilot:
                await pilot.pause(0.4)
                page = app.query_one("#content DashboardPage")
                assert app.query("#content #tasks")
                page.query_one("#btn-start", Button).press()
                await pilot.pause(0.5)
                page.query_one("#btn-stop", Button).press()
                await pilot.pause(0.5)
                assert calls == ["start", "stop"], calls
        asyncio.run(go())
    finally:
        for n, f in orig.items():
            setattr(tb_service, n, f)


def _case_dashboard_doctor_stub() -> None:
    """诊断按钮：doctor_checks 打桩后渲染到 #doctor-out。"""
    import asyncio
    from tb import ops as tb_ops
    from tb.tui import TBApp

    orig = tb_ops.doctor_checks
    tb_ops.doctor_checks = lambda: [("假项", True, "ok"), ("坏项", False, "boom")]
    try:
        async def go():
            app = TBApp()
            async with app.run_test(size=(110, 34)) as pilot:
                await pilot.pause(0.4)
                page = app.query_one("#content DashboardPage")
                out = page.query_one("#doctor-out")
                assert out.has_class("hidden")
                page.query_one("#btn-doctor").press()
                await pilot.pause(0.5)
                assert not out.has_class("hidden")
                text = str(out.render())
                assert "假项" in text and "坏项" in text and "❌" in text
        asyncio.run(go())
    finally:
        tb_ops.doctor_checks = orig


def _case_config_page_headless() -> None:
    """配置页：双 Tab、开关/编辑器就位、坏文本保存被拦且不落盘。"""
    import asyncio
    import shutil
    import tempfile
    from pathlib import Path
    from tb.tui import TBApp

    tmp = Path(tempfile.mkdtemp())
    victim = tmp / "config.yaml"
    try:
        async def go():
            from textual.widgets import TabPane
            app = TBApp()
            async with app.run_test(size=(110, 34)) as pilot:
                await pilot.pause(0.3)
                app.query_one("#nav").index = 4      # -> 配置页
                await pilot.pause(0.5)
                page = app.query_one("#content ConfigPage")
                assert len(page.query(TabPane)) == 2  # 参数 / 115 授权
                ta = page.query_one("#cfg-text")
                assert "telegram:" in ta.text
                page.cfg_path = victim                # 重定向，绝不碰真实配置
                ta.text = "telegram: [坏yaml"
                page.query_one("#cfg-save").press()
                await pilot.pause(0.3)
                assert "未保存" in str(page.query_one("#cfg-status").render())
                assert not victim.exists()
        asyncio.run(go())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) > 1:                      # 子进程模式：跑单个用例
        name = sys.argv[1]
        try:
            globals()[name]()
        except BaseException:
            traceback.print_exc()
            sys.exit(1)
    else:                                      # 主模式：套件入口
        test_tui_suite()
