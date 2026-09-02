"""tb — tg115bot 统一命令行（Typer + Rich）。

三类命令：
  服务     start/stop/restart/status/log          → tb.service（跨平台）
  运维     init/mihomo/session/update/doctor/version → tb.ops
  115 操作 ls/info/search/upload/download/offline/rm/mv/mkdir/rename/df/share/auth
           → tb.bridge → scripts/manual.py 命令层（与 manual.py 行为一致）

裸 `tb` 进交互模式：优先 Textual TUI（P2），TB_TUI=0 或导入失败回退 Rich 菜单。
"""
from __future__ import annotations

import os

import typer
from rich.console import Console

from tb import VERSION
from tb import bridge, ops, service

console = Console()

app = typer.Typer(
    help="tg115bot 统一命令行：Telegram → 115 网盘机器人的安装、服务与日常操作。",
    no_args_is_help=False,
    rich_markup_mode="rich",
)
offline_app = typer.Typer(help="115 离线下载任务（磁力/ed2k/直链，115 服务器下载）")
share_app = typer.Typer(help="115 分享链接转存（需 config.share.cookies）")
app.add_typer(offline_app, name="offline")
app.add_typer(share_app, name="share")

_ACCOUNT = {"name": ""}


def _rc(code: int) -> None:
    raise typer.Exit(code=code)


def _launch_default() -> int:
    """裸 tb：TUI 优先，禁用/不可用回退 Rich 菜单。"""
    if os.environ.get("TB_TUI", "1") != "0":
        try:
            from tb.tui import run as run_tui
            return run_tui(_ACCOUNT["name"])
        except ImportError:
            pass
        except Exception as e:  # noqa: BLE001 -- TUI 崩溃不该没出口
            console.print(f"[yellow]TUI 启动失败（{e}），回退菜单模式（TB_TUI=0 可跳过）[/]")
    from tb.menu import run as run_menu
    return run_menu()


# ── 全局选项 ────────────────────────────────────────────────────────────

@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    account: str = typer.Option("", "--account", "-a", envvar="TB_ACCOUNT",
                                help="115 账号名（默认 config 第一个）"),
    version: bool = typer.Option(False, "--version", "-V", help="打印版本并退出"),
):
    if version:
        _rc(ops.cmd_version())
    _ACCOUNT["name"] = account or ""
    if ctx.invoked_subcommand is None:
        _rc(_launch_default())


# ── 服务管理 ────────────────────────────────────────────────────────────

@app.command(help="启动后台服务（bot 常驻，关终端/SSH 断开不影响）")
def start():
    _rc(service.do_start())


@app.command(help="停止（先优雅等待 10s，超时强杀进程树）")
def stop():
    _rc(service.do_stop())


@app.command(help="重启（更新代码/改配置后用这个）")
def restart():
    _rc(service.do_restart())


@app.command(help="运行状态：PID / 内存 / 运行时长")
def status():
    _rc(service.do_status())


@app.command(help="查看日志（默认尾部 50 行；TTY 下持续跟踪，Ctrl+C 退出）")
def log(
    tail: int = typer.Option(50, "--tail", "-n", help="尾部行数"),
):
    _rc(service.do_log(tail))


# ── 运维 ────────────────────────────────────────────────────────────────

@app.command(help="一键初始化（依赖/代理/配置/扫码授权，幂等可重跑）")
def init():
    _rc(ops.cmd_init())


@app.command(help="部署/更新 mihomo 代理（仅 Linux；含安全加固）")
def mihomo(
    sub_url: str = typer.Argument("", help="机场订阅地址（留空交互输入；也可填本地配置路径）"),
):
    _rc(ops.cmd_mihomo(sub_url))


@app.command(help="生成 TG user session（提升下载额度，Premium 可下 4GB）")
def session():
    _rc(ops.cmd_session())


@app.command(help="更新代码与依赖（git pull + pip install）")
def update():
    _rc(ops.cmd_update())


@app.command(help="打印版本 / 安装目录 / Python 信息")
def version():
    _rc(ops.cmd_version())


@app.command(help="一键体检：环境/配置/115 授权/磁盘/代理安全/服务")
def doctor():
    _rc(ops.cmd_doctor())


@app.command(help="进 Rich 交互菜单（TUI 的轻量回退形态）")
def menu():
    from tb.menu import run as run_menu
    _rc(run_menu())


@app.command(help="扫码（重新）授权 115，强刷 token")
def auth():
    _rc(bridge.run_manual(["auth"], _ACCOUNT["name"]))


# ── 115 操作（全部桥接 manual.py 命令层，行为与其一致） ───────────────────

@app.command(help="列 115 目录（目录排前，按名排序）")
def ls(
    path: str = typer.Argument("/", help="115 目录路径"),
    all_pages: bool = typer.Option(False, "--all", "-a", help="翻页取全部"),
):
    argv = ["ls", path] + (["--all"] if all_pages else [])
    _rc(bridge.run_manual(argv, _ACCOUNT["name"]))


@app.command(help="文件/目录详情（含 pickcode）")
def info(path: str = typer.Argument(..., help="115 路径")):
    _rc(bridge.run_manual(["info", path], _ACCOUNT["name"]))


@app.command(help="115 全盘搜索")
def search(keyword: str = typer.Argument(..., help="关键词")):
    _rc(bridge.run_manual(["search", keyword], _ACCOUNT["name"]))


@app.command(help="上传本地文件/目录/通配符（目录递归保结构，跨来源去重）")
def upload(
    sources: list[str] = typer.Argument(..., help="本地路径（支持通配符，空格分隔多个）"),
    dir: str = typer.Option("", "-d", "--dir", help="115 目标目录（默认 upload.target_dir）"),
):
    argv = ["upload", *sources] + (["-d", dir] if dir else [])
    _rc(bridge.run_manual(argv, _ACCOUNT["name"]))


@app.command(help="下载 115 单文件到本地（sha1 校验）")
def download(
    path: str = typer.Argument(..., help="115 文件路径"),
    out: str = typer.Option("", "-o", "--out", help="本地保存目录（默认当前目录）"),
):
    argv = ["download", path] + (["-o", out] if out else [])
    _rc(bridge.run_manual(argv, _ACCOUNT["name"]))


@offline_app.command("add", help="添加离线任务（magnet/ed2k/直链，可多个）")
def offline_add(
    urls: list[str] = typer.Argument(..., help="链接（空格分隔多个）"),
    dir: str = typer.Option("", "-d", "--dir", help="保存目录（默认 upload.target_dir）"),
):
    argv = ["offline", "add", *urls] + (["-d", dir] if dir else [])
    _rc(bridge.run_manual(argv, _ACCOUNT["name"]))


@offline_app.command("list", help="离线任务列表")
def offline_list(
    all_pages: bool = typer.Option(False, "-a", "--all", help="翻页取全部"),
):
    argv = ["offline", "list"] + (["-a"] if all_pages else [])
    _rc(bridge.run_manual(argv, _ACCOUNT["name"]))


@offline_app.command("del", help="删除离线任务")
def offline_del(
    info_hash: str = typer.Argument(..., help="info_hash（见列表）"),
    purge: bool = typer.Option(False, "--purge", help="连已下载文件一起删"),
):
    argv = ["offline", "del", info_hash] + (["--purge"] if purge else [])
    _rc(bridge.run_manual(argv, _ACCOUNT["name"]))


@app.command(help="删除 115 文件/目录（入回收站，默认需确认）")
def rm(
    paths: list[str] = typer.Argument(..., help="115 路径（多个）"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
):
    argv = ["rm", *paths] + (["--yes"] if yes else [])
    _rc(bridge.run_manual(argv, _ACCOUNT["name"]))


@app.command(help="移动到目标目录")
def mv(src: str = typer.Argument(..., help="源 115 路径"),
       dst: str = typer.Argument(..., help="目标 115 目录")):
    _rc(bridge.run_manual(["mv", src, dst], _ACCOUNT["name"]))


@app.command(help="递归建目录")
def mkdir(path: str = typer.Argument(..., help="115 目录路径")):
    _rc(bridge.run_manual(["mkdir", path], _ACCOUNT["name"]))


@app.command(help="重命名")
def rename(path: str = typer.Argument(..., help="115 路径"),
           new_name: str = typer.Argument(..., help="新名字（不含 /）")):
    _rc(bridge.run_manual(["rename", path, new_name], _ACCOUNT["name"]))


@app.command(help="115 空间 / 离线配额 / 风控水位")
def df():
    _rc(bridge.run_manual(["df"], _ACCOUNT["name"]))


@share_app.command("save", help="转存分享链接")
def share_save(
    link: str = typer.Argument(..., help="分享链接（可含访问码）"),
    dir: str = typer.Option("", "-d", "--dir", help="保存目录（默认 share.target_dir）"),
    password: str = typer.Option("", "-p", "--password", help="访问码（链接已含则空）"),
):
    argv = ["share", "save", link]
    if dir:
        argv += ["-d", dir]
    if password:
        argv += ["-p", password]
    _rc(bridge.run_manual(argv, _ACCOUNT["name"]))


def main() -> int:
    try:
        app()
    except typer.Exit as e:
        return e.exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
