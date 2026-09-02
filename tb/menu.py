"""Rich 交互菜单（`tb menu`；裸 `tb` 在 TUI 不可用时也回退到这里）。

复用 manual.py 的 MENU 表与 build_argv→parser→run_cmd 链路，仅替换输入输出
为 Rich 渲染——与 CLI 子命令、manual.py 交互菜单三者行为同构。
"""
from __future__ import annotations

import asyncio

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from scripts import manual
from scripts.manual import (  # noqa: F401
    MENU, MENU_BY_NUM, SWITCH, build_argv, build_parser, run_cmd, switch_account,
)

console = Console()


def _menu_table() -> Table:
    table = Table.grid(padding=(0, 3))
    table.add_column(justify="right", style="bold cyan", no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(style="dim")
    last_group = None
    for m in MENU:
        if m.group != last_group:
            table.add_row("", f"[dim]── {m.group} ──[/]", "", "")
            last_group = m.group
        tips = " ".join(
            (f"--{f.flag.lstrip('-')}" if not f.positional else f"<{f.dest}>")
            for f in m.fields
        )
        table.add_row(str(m.num), m.label.split()[0] if " " in m.label else m.label,
                      " ".join(m.label.split()[1:]) if " " in m.label else "", f"[dim]{tips}[/]")
    table.add_row("q", "退出", "", "")
    return table


async def menu_loop(ctx: "manual.Ctx") -> int:
    parser = build_parser()
    while True:
        console.print()
        console.print(
            Panel(_menu_table(), title="[bold]tg115bot[/]",
                  subtitle=f"账号 [cyan]{ctx.account.name}[/] · 命令行等价: tb <子命令>",
                  border_style="cyan"))
        try:
            choice = Prompt.ask("[bold]选择编号[/]").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        if choice in ("q", "quit", "exit", ""):
            return 0
        item = MENU_BY_NUM.get(choice)
        if item is None:
            console.print("[yellow]❌ 无效编号[/]")
            continue

        answers: dict[str, str] = {}
        cancelled = False
        for f in item.fields:
            try:
                if f.boolean:
                    answers[f.dest] = "y" if Confirm.ask(f.prompt, default=False) else ""
                else:
                    answers[f.dest] = Prompt.ask(f.prompt, default=f.default or "").strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                cancelled = True
                break
        if cancelled:
            continue

        if item.action == SWITCH:
            name = answers.get("account", "")
            if not name:
                names = ", ".join(a.name for a in ctx.cfg.accounts)
                console.print(f"👤 当前: [cyan]{ctx.account.name}[/]（可选: {names}）")
                continue
            try:
                await switch_account(ctx, name)
            except Exception as e:  # noqa: BLE001
                console.print(f"[red]❌ 切换失败: {e}[/]")
            continue

        argv = build_argv(item, answers)
        try:
            args = parser.parse_args(argv)
        except SystemExit:
            console.print("[yellow]❌ 参数不完整，已返回菜单[/]")
            continue
        await run_cmd(ctx, args)


def run() -> int:
    """入口：建账号上下文（需已授权）后进菜单循环。"""
    try:
        cfg = manual.load_config()
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]❌ 配置加载失败: {e}[/]（先跑 [cyan]tb init[/]）")
        return 2

    async def runner() -> int:
        try:
            ctx = await manual.build_ctx(cfg, "")
        except RuntimeError as e:
            console.print(f"[red]❌ {e}[/]")
            return 2
        try:
            return await menu_loop(ctx)
        finally:
            await ctx.cloud.close()

    try:
        return asyncio.run(runner())
    except KeyboardInterrupt:
        return 0
