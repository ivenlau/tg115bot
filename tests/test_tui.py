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
            for idx, expect in ((3, "ConfigPage"), (4, "LogPage"), (1, "FilesPage")):
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
    """操作弹窗：快捷键/按钮同路；重命名预填旧名；校验不过弹窗不关；Esc 取消。"""
    import asyncio
    from types import SimpleNamespace

    from tb.tui import ConfirmModal, PromptModal, TBApp

    async def stub_init(self):
        self.ctx = SimpleNamespace(cloud=SimpleNamespace(raw=SimpleNamespace()),
                                   account=SimpleNamespace(name="test"))
        self.db = None
    TBApp._init_all = stub_init

    async def go():
        from textual.widgets import Button, Input, Static
        app = TBApp()
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause(0.2)
            app.query_one("#nav").index = 1
            await pilot.pause(0.5)
            page = app.query_one("#content FilesPage")
            t = page.query_one("#files")
            t.clear()
            t.add_row("📄", "a.mkv", "1G")
            t.move_cursor(row=0)
            t.focus()

            # s → 下载弹 PromptModal，Esc 取消
            await pilot.press("s")
            await pilot.pause(0.2)
            assert isinstance(app.screen, PromptModal), "s 应弹下载输入框"
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, PromptModal)

            # n → 预填旧名；清空提交 → 红字校验、弹窗不关；合法值回车 → 关闭
            await pilot.press("n")
            await pilot.pause(0.2)
            m = app.screen
            assert isinstance(m, PromptModal)
            inp = m.query_one("#dlg-input", Input)
            assert inp.value == "a.mkv", "重命名应预填旧名"
            inp.value = ""
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, PromptModal), "校验失败不应关闭"
            assert str(m.query_one("#dlg-err", Static).render()).strip(), "应有红字提示"
            inp.value = "b.mkv"
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert not isinstance(app.screen, PromptModal), "合法值应提交关闭"

            # 按钮与快捷键同路：op-rename 弹 PromptModal
            page.query_one("#op-rename", Button).press()
            await pilot.pause(0.2)
            assert isinstance(app.screen, PromptModal)
            await pilot.press("escape")
            await pilot.pause(0.2)

            # 上传：常驻输入框 + 按钮；空输入禁用，u 聚焦，提交后清空复位
            up_btn = page.query_one("#op-upload", Button)
            up_inp = page.query_one("#upload-input", Input)
            assert up_btn.disabled, "空输入时上传按钮应禁用"
            await pilot.press("u")
            await pilot.pause(0.2)
            assert up_inp.has_focus, "u 应聚焦上传输入框"
            await pilot.press(*"/tmp/tui-no-such")
            await pilot.pause(0.2)
            assert not up_btn.disabled, "有内容时上传按钮应可用"
            up_btn.press()
            await pilot.pause(0.6)
            assert up_inp.value == "" and up_btn.disabled, "提交后应清空并禁用"

            # d → 删除走 ConfirmModal，取消不执行（焦点回表格，d 才是快捷键）
            t.focus()
            await pilot.press("d")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ConfirmModal), "删除应走确认框"
            app.screen.query_one("#dlg-cancel", Button).press()
            await pilot.pause(0.2)
            assert not isinstance(app.screen, ConfirmModal)
    asyncio.run(go())


def _case_dashboard_service_buttons_stub() -> None:
    """仪表盘服务按钮：启动直通；停止/重启走弹窗确认（不真启停）。"""
    import asyncio
    from tb import service as tb_service
    from tb.tui import TBApp, ConfirmModal

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
                page.query_one("#btn-start", Button).press()   # 启动无破坏性：直通
                await pilot.pause(0.5)
                assert calls == ["start"], calls
                await pilot.press("t")                         # 快捷键停止：弹确认
                await pilot.pause(0.3)
                assert isinstance(app.screen, ConfirmModal), "停止应弹确认框"
                app.screen.query_one("#dlg-ok", Button).press()
                await pilot.pause(0.5)
                assert calls == ["start", "stop"], calls
                page.query_one("#btn-restart", Button).press()  # 重启：弹确认后取消
                await pilot.pause(0.3)
                app.screen.query_one("#dlg-cancel", Button).press()
                await pilot.pause(0.3)
                assert calls == ["start", "stop"], "取消不应触发重启"
        asyncio.run(go())
    finally:
        for n, f in orig.items():
            setattr(tb_service, n, f)


def _case_dashboard_doctor_stub() -> None:
    """诊断按钮：doctor_checks 打桩后结果进模态弹窗，Esc 关闭。"""
    import asyncio
    from tb import ops as tb_ops
    from tb.tui import InfoModal, TBApp

    orig = tb_ops.doctor_checks
    tb_ops.doctor_checks = lambda: [("假项", True, "ok"), ("坏项", False, "boom")]
    try:
        async def go():
            from textual.widgets import Static
            app = TBApp()
            async with app.run_test(size=(110, 34)) as pilot:
                await pilot.pause(0.4)
                page = app.query_one("#content DashboardPage")
                page.query_one("#btn-doctor").press()
                await pilot.pause(0.5)
                assert isinstance(app.screen, InfoModal), "诊断结果应在弹窗中"
                text = str(app.screen.query_one(".dlg-msg", Static).render())
                assert "假项" in text and "坏项" in text and "❌" in text
                await pilot.press("escape")
                await pilot.pause(0.3)
                assert not isinstance(app.screen, InfoModal), "Esc 应关闭弹窗"
        asyncio.run(go())
    finally:
        tb_ops.doctor_checks = orig


def _case_offline_page_ops_stub() -> None:
    """离线页：添加输入框+按钮联动（空禁用）；a 聚焦；无选中按 d 只提示不炸。"""
    import asyncio
    from types import SimpleNamespace

    from tb.tui import TBApp

    async def stub_init(self):
        self.ctx = SimpleNamespace(cloud=SimpleNamespace(raw=SimpleNamespace()),
                                   account=SimpleNamespace(name="test"))
        self.db = None
    TBApp._init_all = stub_init

    async def go():
        from textual.widgets import Button, Input
        app = TBApp()
        async with app.run_test(size=(110, 32)) as pilot:
            await pilot.pause(0.2)
            app.query_one("#nav").index = 2
            await pilot.pause(0.5)
            page = app.query_one("#content OfflinePage")
            add_btn = page.query_one("#op-off-add", Button)
            add_inp = page.query_one("#off-add", Input)
            assert add_btn.disabled, "空输入时添加按钮应禁用"
            await pilot.press("a")
            await pilot.pause(0.2)
            assert add_inp.has_focus, "a 应聚焦添加输入框"
            await pilot.press(*"magnet:?xt=stub")
            await pilot.pause(0.2)
            assert not add_btn.disabled, "有内容时添加按钮应可用"
            # 无选中按 d：toast 提示，不炸
            page.query_one("#off-t").focus()
            await pilot.press("d")
            await pilot.pause(0.3)
    asyncio.run(go())


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
                app.query_one("#nav").index = 3      # -> 配置页
                await pilot.pause(0.5)
                page = app.query_one("#content ConfigPage")
                assert len(page.query(TabPane)) == 2  # 参数 / 115 授权
                ta = page.query_one("#cfg-text")
                assert "telegram:" in ta.text
                page.cfg_path = victim                # 重定向，绝不碰真实配置
                ta.text = "telegram: [坏yaml"
                ta.focus()                            # 编辑区内按 s：应输入字母不触发保存
                await pilot.pause(0.1)
                await pilot.press("s")
                await pilot.pause(0.3)
                assert ta.text != "telegram: [坏yaml" and "s" in ta.text, \
                    "编辑区按键应输入字母"
                assert "未保存" not in str(page.query_one("#cfg-status").render())
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
