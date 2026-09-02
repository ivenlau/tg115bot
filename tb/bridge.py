"""桥接 scripts/manual.py 命令层。

tb 的 CLI 子命令 / 交互菜单 / TUI 全部经此驱动同一份实现——与
`python scripts/manual.py <子命令>` 行为完全一致（同一 parser、同一 run_cmd、
同一退出码语义 0/1/2），不会出现两套行为。
"""
from __future__ import annotations

import asyncio

from tb import INSTALL_DIR  # noqa: F401  -- 触发 sys.path 引导

from scripts import manual  # noqa: E402


def run_manual(argv: list[str], account: str = "") -> int:
    """以等价命令行驱动 manual 命令层，返回退出码（0/1/2）。

    argv 形如 ["ls", "/tg115bot", "--all"]；account 为空用 config 第一个账号。
    """
    full = (["--account", account] if account else []) + list(argv)
    args = manual.build_parser().parse_args(full)
    try:
        cfg = manual.load_config()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 -- argparse 的 usage 退出原样放行
        print(f"❌ 配置加载失败: {e}\n（还没初始化？先跑: tb init）")
        return 2

    async def runner() -> int:
        # auth 不要求已登录（token 失效时正是要用它续授权）
        need_login = getattr(args, "cmd", None) != "auth"
        try:
            ctx = await manual.build_ctx(cfg, args.account or "", need_login=need_login)
        except RuntimeError as e:
            print(f"❌ {e}")
            return 2
        try:
            return await manual.run_cmd(ctx, args)
        finally:
            await ctx.cloud.close()

    try:
        return asyncio.run(runner())
    except KeyboardInterrupt:
        print("\n🚫 已中断")
        return 1


async def build_ctx_live(account: str = "") -> "manual.Ctx":
    """TUI/长驻场景用：建好并保持存活的上下文（调用方负责 close）。"""
    cfg = manual.load_config()
    return await manual.build_ctx(cfg, account or "", need_login=False)
