"""115 手动运维工具（独立于 Telegram，复用与 bot 相同的 cloud115 适配层）。

两种用法：
    1) 交互菜单：python scripts/manual.py            （无参数，编号选择，循环操作）
    2) 子命令：  python scripts/manual.py <命令> …    （一次性执行，可 cron/脚本编排）

子命令一览：
    查看      ls <路径> [--limit N] [--all]      列目录（--all 翻页取全部）
              info <路径>                        文件/目录详情（含 pickcode）
              search <关键词>                    全盘搜索
    传输      upload <本地路径>… [-d 115目录]     上传（秒传/OSS 全链路；目录递归，支持通配符）
              download <115路径> [-o 本地目录]   下载单文件到本地（sha1 校验）
    离线      offline add <url>… [-d 目录]       添加离线任务（magnet/ed2k/直链）
              offline list [-a]                  任务列表（-a 翻页全部）
              offline del <info_hash> [--purge]  删任务（--purge 连已下载文件删）
    文件管理  mkdir <路径> / mv <源> <目标目录> / rename <路径> <新名>
              rm <路径>… [--yes]                 删除（入回收站，默认需确认）
    其他      df                                 空间/离线配额/风控水位
              share save <链接> [-d 目录] [-p 访问码]  分享转存（需 share.cookies）
              auth                               扫码（重新）授权，强刷 token

全局参数 --account <名>（默认 config.accounts 第一个）；未授权/token 失效（如
40140116 授权已解除）时跑 python scripts/manual.py auth 扫码续上。
退出码：0 成功 / 1 失败 / 2 需重新授权。

示例：
    python scripts/manual.py                          # 进交互菜单
    python scripts/manual.py ls /tg115bot             # 列目录
    python scripts/manual.py --account b2 df          # 指定账号查空间
    python scripts/manual.py download /tg115bot/a.mkv -o ~/Downloads
    python scripts/manual.py offline add "magnet:?xt=…" -d /tg115bot/bt
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import hashlib
import logging
import os
import sys
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config                      # noqa: E402
from utils.rate import RateLimiter                  # noqa: E402
from cloud115.client import Cloud115Client          # noqa: E402
from cloud115.openapi import AuthRequiredError      # noqa: E402
from cloud115.filesystem import (                   # noqa: E402
    find_entry, list_dir_all, mkdir_p, resolve_cid,
)
from cloud115.download import (                     # noqa: E402
    download_file, parse_downurl, sanitize_name, unique_dest,
)
from core.progress import human_bytes               # noqa: E402
from core.uploader import upload_to_dir             # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("manual")

_READ = 1024 * 1024

# ── 上下文与账号 ─────────────────────────────────────────────────────────


@dataclass
class Ctx:
    cfg: object
    account: object          # AccountCfg
    rate: RateLimiter
    cloud: Cloud115Client


def _pick_account(cfg, name: str):
    """按名选账号；空名取 primary。不存在返回 None。"""
    if name:
        return next((a for a in cfg.accounts if a.name == name), None)
    return cfg.primary_account


async def build_ctx(cfg, account_name: str, *, need_login: bool = True) -> Ctx:
    """建账号上下文：client + init + 登录校验（need_login=False 供 auth 扫码用）。失败抛 RuntimeError。"""
    acct = _pick_account(cfg, account_name)
    if acct is None:
        names = ", ".join(a.name for a in cfg.accounts) or "（config.accounts 为空）"
        raise RuntimeError(f"账号不存在: {account_name or '<primary>'}（可选: {names}）")
    rate = RateLimiter(cfg.rate115.min_interval_sec)
    cloud = Cloud115Client(acct, cfg.session_dir, rate)
    await cloud.init()
    if need_login:
        try:
            if not await cloud.ensure_login():
                raise RuntimeError("账号未授权/token 失效。先跑: python scripts/manual.py auth")
        except AuthRequiredError as e:
            await cloud.close()
            raise RuntimeError(f"{e}。先跑: python scripts/manual.py auth") from e
        except Exception:
            await cloud.close()
            raise
    return Ctx(cfg=cfg, account=acct, rate=rate, cloud=cloud)


async def switch_account(ctx: Ctx, name: str) -> int:
    """菜单切换账号：成功后原地替换 ctx 字段（菜单循环持有同一 ctx）。"""
    new = await build_ctx(ctx.cfg, name)
    await ctx.cloud.close()
    ctx.account, ctx.rate, ctx.cloud = new.account, new.rate, new.cloud
    print(f"👤 已切换账号: {ctx.account.name}")
    return 0


# ── 纯工具（自 manual_upload 迁入 / 新增） ─────────────────────────────


async def sha1_of(path: Path) -> tuple[int, str]:
    """算文件 (大小, sha1)；流式读，不占大内存。"""
    sha = hashlib.sha1()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_READ)
            if not chunk:
                break
            sha.update(chunk)
            size += len(chunk)
    return size, sha.hexdigest()


def collect_files(src: Path) -> list[Path]:
    """文件 -> [自身]；目录 -> 递归全部文件（排序保证顺序稳定）。"""
    if src.is_file():
        return [src]
    return sorted(p for p in src.rglob("*") if p.is_file())


def _has_wildcard(s: str) -> bool:
    return any(c in s for c in "*?[")


def expand_sources(raws: list[str]) -> tuple[list[Path], dict[Path, Path], list[str]]:
    """多来源展开为待传文件：通配符 glob、目录递归、跨来源去重保序。

    返回 (文件列表, file->base 映射, 未命中来源说明)。base 决定远端相对路径：
    目录来源 = 目录自身（保留内部结构），文件来源 = 父目录（直接用原名）。
    """
    files: list[Path] = []
    bases: dict[Path, Path] = {}
    seen: set[Path] = set()
    missing: list[str] = []
    for raw in raws:
        if _has_wildcard(raw):
            srcs = [Path(m) for m in sorted(glob.glob(str(Path(raw).expanduser())))]
            if not srcs:
                missing.append(f"无匹配: {raw}")
                continue
        else:
            src = Path(raw).expanduser()
            if not src.exists():
                missing.append(f"路径不存在: {src}")
                continue
            srcs = [src]
        for src in srcs:
            base = src if src.is_dir() else src.parent
            for f in collect_files(src):
                key = f.resolve()
                if key not in seen:
                    seen.add(key)
                    files.append(f)
                    bases[f] = base
    return files, bases, missing


def make_progress():
    """下载进度回调工厂：TTY 单行 \\r 刷新（1s 节流）；非 TTY 按行（cron 重定向安全）。"""
    last = 0.0
    tty = sys.stdout.isatty()

    def cb(cur: int, total: int) -> None:
        nonlocal last
        now = time.monotonic()
        if now - last < 1.0 and cur != total:
            return
        last = now
        pct = f" ({cur * 100 // total}%)" if total else ""
        line = f"  📥 {human_bytes(cur)}{pct}"
        if tty:
            print("\r" + line.ljust(36), end="", flush=True)
        else:
            print(line, flush=True)

    return cb


# ── 条目/任务格式化（字段缩写容错，参照 bot /ls /status） ───────────────


def entry_name(it: dict) -> str:
    return it.get("fn") or it.get("n") or it.get("file_name") or "?"


def entry_is_dir(it: dict) -> bool:
    raw = it.get("fc")
    if raw is None or raw == "":
        raw = it.get("file_category", "1")
    return str(raw) == "0"


def entry_size(it: dict) -> int:
    return int(it.get("fs") or it.get("size") or 0)


def entry_fid(it: dict) -> str:
    return str(it.get("fid") or it.get("file_id") or "")


def fmt_ls_line(it: dict) -> str:
    name = entry_name(it)
    if entry_is_dir(it):
        return f"📂 {name}/"
    return f"📄 {name}  {human_bytes(entry_size(it))}"


def sort_entries(items: list) -> list:
    """目录排前，再按名字排序（与网盘客户端习惯一致）。"""
    return sorted(items, key=lambda it: (not entry_is_dir(it), entry_name(it)))


OFFLINE_ICON = {-1: "❌", 1: "📥", 2: "✅"}   # 图标须 EAW=W（用 ⬇️ 这类 EAW=N 字符部分终端会错位）


def fmt_offline_line(t: dict) -> str:
    status = t.get("status")
    icon = OFFLINE_ICON.get(status, "•")
    name = t.get("name") or (t.get("url") or "?")[:60]
    pct = f" {t.get('percentDone', 0)}%" if status == 1 else ""
    ih = str(t.get("info_hash") or "")[:8]
    return f"{icon} {name}{pct}  [{ih}]"


# ── 命令实现（CLI 与菜单共用；签名统一 (ctx, args) -> 退出码） ─────────


async def cmd_ls(ctx: Ctx, args) -> int:
    path = args.path.strip() or "/"
    cid = await resolve_cid(ctx.cloud, path)
    if args.all:
        items = await list_dir_all(ctx.cloud, cid)
    else:
        items = (await ctx.cloud.raw.list_files(int(cid), limit=args.limit)).get("list") or []
    if not items:
        print(f"📁 {path}（空目录）")
        return 0
    items = sort_entries(items)
    if not args.all:
        items = items[: args.limit]
    note = "" if args.all else "，--all 看全部"
    print(f"📁 {path}（{len(items)} 项{note}）")
    for it in items:
        print("  " + fmt_ls_line(it))
    return 0


async def cmd_info(ctx: Ctx, args) -> int:
    entry = await find_entry(ctx.cloud, args.path)
    print(f"📄 {entry_name(entry)}" + ("/（目录）" if entry_is_dir(entry) else ""))
    for k in ("fid", "fc", "fs", "pc", "sha1", "pid"):
        if entry.get(k) is not None:
            print(f"  {k:5} = {entry.get(k)}")
    if not entry_is_dir(entry):
        info = await ctx.cloud.raw.get_file_info(args.path, use_cache=False)
        if info:
            print("  ── folder/get_info 原始返回 ──")
            for k, v in info.items():
                print(f"  {k:12} = {v}")
    return 0


async def cmd_search(ctx: Ctx, args) -> int:
    data = await ctx.cloud.raw.search_files(args.keyword, limit=20)
    items = data.get("list") or []
    if not items:
        print(f"🔍 未找到: {args.keyword}")
        return 0
    print(f"🔍 {args.keyword}（{len(items)} 项）")
    for it in items:
        is_dir = entry_is_dir(it)
        line = f"  {'📂' if is_dir else '📄'} {entry_name(it)}"
        if is_dir:
            print(line)
            continue
        size = entry_size(it)
        sha1 = str(it.get("sha1") or "")
        extra = f"  {human_bytes(size)}" if size else ""
        if sha1:
            extra += f"  sha1={sha1[:8]}"
        print(line + extra)
    return 0


async def cmd_upload(ctx: Ctx, args) -> int:
    target_root = args.dir or ctx.cfg.upload.target_dir
    files, bases, missing = expand_sources(args.sources)
    for m in missing:
        print(f"❌ {m}")
    print(f"👤 账号 {ctx.account.name}，目标目录: {target_root}，共 {len(files)} 个文件")
    ok = fail = 0
    for i, f in enumerate(files, 1):
        rel = f.relative_to(bases[f]).parent
        # 目录上传时保持相对路径结构；文件上传用原名
        remote_dir = str(target_root.rstrip("/") + ("/" + rel.as_posix() if str(rel) != "." else ""))
        name = f.name
        size, sha1 = await sha1_of(f)
        print(f"[{i}/{len(files)}] {name} ({human_bytes(size)}) -> {remote_dir}")
        try:
            # upload_to_dir 内部已含 mkdir_p + 秒传/OSS 全链路 + 退避重试
            result = await upload_to_dir(ctx.cloud, f, size, sha1, remote_dir, name,
                                         oss_concurrency=ctx.cfg.upload.oss_concurrency)
            print(f"    ✅ {result.method}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"    ❌ 失败: {e}")
            log.debug("上传失败: %s", f, exc_info=True)
            fail += 1
    print(f"\n完成：成功 {ok} / 失败 {fail} / 共 {len(files)}")
    return 1 if (fail or missing) else 0


async def cmd_download(ctx: Ctx, args) -> int:
    entry = await find_entry(ctx.cloud, args.path)
    if entry_is_dir(entry):
        print("❌ 目录下载 v1 暂不支持：递归需逐层列目录 + 逐文件取直链，"
              "大目录会快速烧穿日 API 配额（df 可查余量）。请对单文件调用")
        return 1
    pc = str(entry.get("pc") or "")
    if not pc:
        print(f"❌ 条目缺 pickcode（pc），无法取下载直链: {entry_name(entry)}")
        return 1
    info = parse_downurl(await ctx.cloud.raw.get_download_url(pc))
    dest_dir = Path(args.out or ".").expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = unique_dest(dest_dir, sanitize_name(info["file_name"] or entry_name(entry)))
    print(f"📥 {info['file_name']}  {human_bytes(info['file_size'])}  ->  {dest}")
    size, sha1 = await download_file(info["url"], dest, expected_size=info["file_size"],
                                     on_progress=make_progress())
    print()
    part = dest.with_name(dest.name + ".part")
    if info["sha1"] and sha1 != info["sha1"]:
        print(f"❌ SHA1 不符（本地 {sha1} ≠ 115 {info['sha1']}），现场保留: {part.name}")
        return 1
    part.rename(dest)
    print(f"✅ 完成 {human_bytes(size)}  sha1={sha1}")
    return 0


async def cmd_offline(ctx: Ctx, args) -> int:
    return await args.offline_run(ctx, args)


async def cmd_offline_add(ctx: Ctx, args) -> int:
    target = args.dir or ctx.cfg.upload.target_dir
    ok = fail = 0
    for url in args.urls:
        try:
            await ctx.cloud.raw.offline_add(url, target)
            print(f"🚀 已添加: {url[:80]}")
            print(f"📁 {target}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"❌ 失败: {url[:80]}\n    {e}")
            fail += 1
    if len(args.urls) > 1:
        print(f"完成：成功 {ok} / 失败 {fail}")
    return 1 if fail else 0


async def cmd_offline_list(ctx: Ctx, args) -> int:
    if args.all:
        tasks = await ctx.cloud.raw.offline_list_all()
    else:
        tasks = (await ctx.cloud.raw.offline_list(1)).get("tasks") or []
    if not tasks:
        print("📭 暂无离线任务")
        return 0
    print(f"📦 离线任务（{len(tasks)} 条，{'全部' if args.all else '第 1 页'}）")
    for t in tasks:
        print("  " + fmt_offline_line(t))
    return 0


async def cmd_offline_del(ctx: Ctx, args) -> int:
    if args.purge and not args.yes:
        try:
            ans = input("❗ purge 会连已下载文件一起删，输入 y 确认: ").strip().lower()
        except EOFError:
            ans = ""
        if ans != "y":
            print("🚫 已取消")
            return 1
    if await ctx.cloud.raw.offline_del(args.info_hash,
                                       del_source_file=1 if args.purge else 0):
        print(f"🧹 已删除离线任务: {args.info_hash}" + ("（含文件）" if args.purge else ""))
        return 0
    return 1


async def cmd_rm(ctx: Ctx, args) -> int:
    entries = []
    for p in args.paths:
        entries.append(await find_entry(ctx.cloud, p))
    print(f"❗ 将删除以下 {len(entries)} 项（移入回收站）:")
    for it in entries:
        kind = "📂" if entry_is_dir(it) else "📄"
        size = "" if entry_is_dir(it) else f"  {human_bytes(entry_size(it))}"
        print(f"  {kind} {entry_name(it)}{size}")
    if not args.yes:
        try:
            ans = input("输入 y 确认: ").strip().lower()
        except EOFError:
            ans = ""
        if ans != "y":
            print("🚫 已取消")
            return 1
    fids = [entry_fid(it) for it in entries]
    await ctx.cloud.raw.delete_files(fids)
    ctx.cloud.raw.invalidate_path_cache()
    print(f"🧹 已删除 {len(fids)} 项（可在 115 回收站恢复）")
    return 0


async def cmd_mv(ctx: Ctx, args) -> int:
    src_entry = await find_entry(ctx.cloud, args.src)
    di = await ctx.cloud.raw.get_file_info(args.dst)
    if di and di.get("file_id") is not None:
        to_cid = int(di["file_id"])
    else:
        to_cid = await ctx.cloud.raw.create_dir_recursive(args.dst)
    await ctx.cloud.raw.move_files(entry_fid(src_entry), to_cid)
    ctx.cloud.raw.invalidate_path_cache()
    print(f"✅ 已移动\n  {args.src}\n→ {args.dst}")
    return 0


async def cmd_mkdir(ctx: Ctx, args) -> int:
    cid = await mkdir_p(ctx.cloud, args.path)
    print(f"✅ 目录就绪: {args.path}（cid={cid}）")
    return 0


async def cmd_rename(ctx: Ctx, args) -> int:
    new_name = args.new_name.strip()
    if not new_name or "/" in new_name:
        print("❌ 新名不能为空且不含 /")
        return 1
    entry = await find_entry(ctx.cloud, args.path)
    await ctx.cloud.raw.rename_file(entry_fid(entry), new_name)
    print(f"✅ 已重命名\n  {entry_name(entry)}\n→ {new_name}")
    return 0


async def cmd_df(ctx: Ctx, args) -> int:
    print(f"👤 账号: {ctx.account.name}")
    space = await ctx.cloud.raw.user_space()
    if space.get("total"):
        pct = space["used"] * 100 / space["total"]
        print(f"📦 115 空间：{human_bytes(space['used'])} / "
              f"{human_bytes(space['total'])}（{pct:.1f}%）")
    quota = await ctx.cloud.raw.offline_quota()
    if quota:
        print(f"📥 离线配额：已用 {quota.get('used', '?')} / {quota.get('count', '?')}")
    print(f"🧮 今日 115 API 请求：{ctx.cloud.raw.request_count}"
          f"（风控阈值 {ctx.cloud.raw.daily_limit}）")
    return 0


def _print_qr(data: str) -> None:
    """终端展示二维码（与 scripts/check115.py 同款）。

    115 的 authDeviceCode 直接返回 QR 数据（URL 字符串），qrcode.print_ascii
    纯核心功能即可渲染；未装 qrcode 库降级为打印链接。
    深色终端背景扫不动时，设环境变量 QR_INVERT=0 切换反色。
    """
    invert = os.environ.get("QR_INVERT", "1") != "0"
    try:
        import qrcode
    except ImportError:
        print("    未装 qrcode 库：请用手机浏览器打开此链接完成授权")
        print("   ", data)
        return
    q = qrcode.QRCode()
    q.add_data(data)
    q.make(fit=True)
    print("\n    请用 115 APP 扫描（深色终端背景扫不动时: QR_INVERT=0 重跑）\n")
    q.print_ascii(invert=invert)
    print("\n    扫不了就用手机浏览器打开：")
    print("   ", data, "\n")


async def cmd_auth(ctx: Ctx, args) -> int:
    """扫码（重新）授权：无视现有 token 强刷 —— 出码 -> 轮询 -> 换新 token 落盘。

    适用：token 彻底失效（40140116 授权不存在或已解除等，refresh_token 也救不回）。
    """
    api = ctx.cloud.raw
    print(f"👤 账号 {ctx.account.name}：发起扫码授权（旧 token 将被覆盖）")
    qr = await api.start_qr_auth()
    _print_qr(qr["qrcode"])
    await asyncio.sleep(5)          # 官方实现：出码后先等 5s 再开始轮询
    last = None
    while True:
        st = await api.poll_qr_status(qr["uid"], qr["time"], qr["sign"])
        if st == 2:
            await api.exchange_qr_token(qr["uid"], qr["verifier"])
            print("    ✅ 授权成功，token 已保存")
            break
        if st == -1:
            print("    ❌ 二维码已过期，请重跑 auth")
            return 1
        if st == -2:
            print("    ❌ 你在 APP 里取消了授权")
            return 1
        if st == 1 and last != 1:
            print("    📱 已扫码，请在手机上点确认 …", flush=True)
        last = st
        await asyncio.sleep(2)
    if not await ctx.cloud.ensure_login():
        print("    ❗ token 已保存，但探活失败（稍后可用 df 复查）")
        return 1
    print("    ✅ 探活成功，授权已恢复")
    return 0


async def cmd_share(ctx: Ctx, args) -> int:
    return await args.share_run(ctx, args)


async def cmd_share_save(ctx: Ctx, args) -> int:
    from cloud115.share import parse_share_link, share_list, share_receive
    cookies = ctx.cfg.share.cookies
    if not cookies:
        print("❗ 未配置转存凭据（config.yaml 的 share.cookies）\n"
              "浏览器登录 115 后复制 Cookie 填入即可，不影响其他功能")
        return 1
    parsed = parse_share_link(args.link)
    if not parsed:
        print("❌ 无法解析分享链接（形如 https://115.com/s/xxx?password=访问码）")
        return 1
    share_code, receive_code = parsed
    if not receive_code and args.password:
        receive_code = args.password
    if not receive_code:
        print("❌ 链接缺访问码，用 -p 补充: share save <链接> -p 访问码")
        return 1
    info, files = await share_list(cookies, share_code, receive_code)
    if not files:
        print("📁 分享为空")
        return 0
    names = ", ".join((f.get("n") or f.get("fn") or "?") for f in files[:5])
    more = f" 等 {len(files)} 项" if len(files) > 5 else ""
    print(f"📥 转存中: {names}{more}")
    target = args.dir or ctx.cfg.share.target_dir
    cid = await ctx.cloud.raw.create_dir_recursive(target)
    fids = [str(f.get("fid") or f.get("f") or "") for f in files]
    fids = [f for f in fids if f]
    await share_receive(cookies, share_code, receive_code, fids, cid)
    print(f"✅ 转存完成（{len(fids)} 项）\n📁 {target}\n来自: {info.get('share_title') or share_code}")
    return 0


# ── 命令注册表 + 参数定义 ───────────────────────────────────────────────


@dataclass
class Command:
    run: object
    help: str
    add_args: object = None      # callable(parser) -> None


def _add_offline(sp: argparse.ArgumentParser) -> None:
    sub = sp.add_subparsers(dest="offline_cmd", required=True)
    p = sub.add_parser("add", help="添加离线任务")
    p.add_argument("urls", nargs="+", help="magnet/ed2k/直链，可多个")
    p.add_argument("-d", "--dir", default="", help="保存目录（默认 upload.target_dir）")
    p.set_defaults(offline_run=cmd_offline_add)
    p = sub.add_parser("list", help="任务列表")
    p.add_argument("-a", "--all", action="store_true", help="翻页取全部")
    p.set_defaults(offline_run=cmd_offline_list)
    p = sub.add_parser("del", help="删除任务")
    p.add_argument("info_hash", help="任务 info_hash（见 list 输出）")
    p.add_argument("--purge", action="store_true", help="连已下载文件一起删")
    p.add_argument("--yes", action="store_true", help="purge 免确认")
    p.set_defaults(offline_run=cmd_offline_del)


def _add_share(sp: argparse.ArgumentParser) -> None:
    sub = sp.add_subparsers(dest="share_cmd", required=True)
    p = sub.add_parser("save", help="转存分享链接")
    p.add_argument("link", help="分享链接（含 ?password= 访问码，或用 -p）")
    p.add_argument("-d", "--dir", default="", help="保存目录（默认 share.target_dir）")
    p.add_argument("-p", "--password", default="", help="访问码（链接里没有时补充）")
    p.set_defaults(share_run=cmd_share_save)


def _add_ls(sp):
    sp.add_argument("path", nargs="?", default="/", help="115 目录路径")
    sp.add_argument("--limit", type=int, default=50, help="条数上限（默认 50）")
    sp.add_argument("--all", action="store_true", help="翻页取全部")


def _add_info(sp):
    sp.add_argument("path", help="115 文件/目录路径")


def _add_search(sp):
    sp.add_argument("keyword", help="搜索关键词")


def _add_upload(sp):
    sp.add_argument("sources", nargs="+", help="本地文件/目录/通配符（空格分隔多个）")
    sp.add_argument("-d", "--dir", default="", help="115 目标目录（默认 upload.target_dir）")


def _add_download(sp):
    sp.add_argument("path", help="115 文件路径（单文件）")
    sp.add_argument("-o", "--out", default="", help="本地保存目录（默认当前目录）")


def _add_rm(sp):
    sp.add_argument("paths", nargs="+", help="要删除的 115 路径（多个）")
    sp.add_argument("--yes", action="store_true", help="跳过确认")


def _add_mv(sp):
    sp.add_argument("src", help="源 115 路径")
    sp.add_argument("dst", help="目标目录（不存在自动创建）")


def _add_mkdir(sp):
    sp.add_argument("path", help="要创建的 115 目录路径")


def _add_rename(sp):
    sp.add_argument("path", help="115 文件/目录路径")
    sp.add_argument("new_name", help="新名字（不含 /）")


COMMANDS: dict[str, Command] = {
    "ls":      Command(cmd_ls, "列目录", _add_ls),
    "info":    Command(cmd_info, "文件/目录详情（含 pickcode）", _add_info),
    "search":  Command(cmd_search, "全盘搜索", _add_search),
    "upload":  Command(cmd_upload, "上传本地文件/目录", _add_upload),
    "download": Command(cmd_download, "下载 115 文件到本地", _add_download),
    "offline": Command(cmd_offline, "离线任务 add/list/del", _add_offline),
    "rm":      Command(cmd_rm, "删除（入回收站）", _add_rm),
    "mv":      Command(cmd_mv, "移动到目标目录", _add_mv),
    "mkdir":   Command(cmd_mkdir, "递归建目录", _add_mkdir),
    "rename":  Command(cmd_rename, "重命名", _add_rename),
    "df":      Command(cmd_df, "空间/离线配额/风控水位"),
    "share":   Command(cmd_share, "分享链接转存", _add_share),
    "auth":    Command(cmd_auth, "扫码（重新）授权，强刷 token"),
}


def build_parser() -> argparse.ArgumentParser:
    """顶层 + 子命令解析器。

    ❗ --account 用 parents 挂两处：子解析器里必须 default=SUPPRESS，否则会把
    顶层已解析的 --account 值覆盖回默认空串（argparse 子解析器默认值会回写同名属性）。
    """
    ap = argparse.ArgumentParser(
        description="115 手动运维工具（无参数进入交互菜单）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--account", default=argparse.SUPPRESS,
                        help="账号名（默认 config.accounts 第一个）")
    ap.add_argument("--account", default="", help="账号名（默认第一个）")
    sub = ap.add_subparsers(dest="cmd")          # 不设 required：None -> 交互菜单
    for name, c in COMMANDS.items():
        sp = sub.add_parser(name, help=c.help, parents=[common])
        if c.add_args:
            c.add_args(sp)
        sp.set_defaults(run=c.run)
    return ap


async def run_cmd(ctx: Ctx, args) -> int:
    """统一错误包装：菜单与 CLI 共用。0/1/2 退出码。"""
    run = getattr(args, "run", None)
    if run is None:
        print("❌ 未指定命令")
        return 1
    try:
        return await run(ctx, args)
    except AuthRequiredError:
        print("❌ 115 令牌已彻底失效，请重新扫码: python scripts/manual.py auth")
        return 2
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return 1
    except KeyboardInterrupt:
        print("\n🚫 已中断")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"❌ 失败: {e}")
        log.debug("命令失败", exc_info=True)
        return 1


# ── 交互菜单（数据驱动；与 CLI 经同一 parser 构造 Namespace，天然同构） ──


@dataclass
class MenuField:
    dest: str              # 存答案的 key
    prompt: str
    default: str = ""
    flag: str = ""         # 非空 -> 命令行选项（-d xxx）；空且 positional -> 位置参数
    positional: bool = False
    multi: bool = False    # 按空格拆多个值
    boolean: bool = False  # y/N 开关 -> 有选项名才追加


@dataclass
class MenuItem:
    num: int
    group: str
    label: str
    action: str            # 顶层子命令（可含二级，如 "offline add"）
    fields: list = dc_field(default_factory=list)


SWITCH = "__switch__"      # 切换账号哨兵（非 COMMANDS 成员）

MENU: list[MenuItem] = [
    MenuItem(1, "查看", "列目录 ls", "ls", [
        MenuField("path", "115 路径", "/", positional=True),
        MenuField("all", "翻页取全部? y/N", "", flag="--all", boolean=True),
    ]),
    MenuItem(2, "查看", "详情 info", "info", [
        MenuField("path", "115 路径", "", positional=True),
    ]),
    MenuItem(3, "查看", "搜索 search", "search", [
        MenuField("keyword", "关键词", "", positional=True),
    ]),
    MenuItem(4, "传输", "上传 upload", "upload", [
        MenuField("source", "本地文件/目录/通配符（空格分隔多个）", "", positional=True, multi=True),
        MenuField("dir", "115 目标目录（空=默认）", "", flag="-d"),
    ]),
    MenuItem(5, "传输", "下载 download", "download", [
        MenuField("path", "115 文件路径", "", positional=True),
        MenuField("out", "本地保存目录（空=当前）", "", flag="-o"),
    ]),
    MenuItem(6, "离线", "添加离线任务", "offline add", [
        MenuField("urls", "链接（空格分隔多个）", "", positional=True, multi=True),
        MenuField("dir", "保存目录（空=默认）", "", flag="-d"),
    ]),
    MenuItem(7, "离线", "离线任务列表", "offline list", [
        MenuField("all", "翻页取全部? y/N", "", flag="--all", boolean=True),
    ]),
    MenuItem(8, "离线", "删除离线任务", "offline del", [
        MenuField("info_hash", "info_hash（见列表）", "", positional=True),
        MenuField("purge", "连文件一起删? y/N", "", flag="--purge", boolean=True),
    ]),
    MenuItem(9, "文件管理", "建目录 mkdir", "mkdir", [
        MenuField("path", "115 目录路径", "", positional=True),
    ]),
    MenuItem(10, "文件管理", "移动 mv", "mv", [
        MenuField("src", "源 115 路径", "", positional=True),
        MenuField("dst", "目标目录", "", positional=True),
    ]),
    MenuItem(11, "文件管理", "重命名 rename", "rename", [
        MenuField("path", "115 路径", "", positional=True),
        MenuField("new_name", "新名字", "", positional=True),
    ]),
    MenuItem(12, "文件管理", "删除 rm", "rm", [
        MenuField("paths", "要删的路径（空格分隔）", "", positional=True, multi=True),
        MenuField("yes", "跳过确认? y/N", "", flag="--yes", boolean=True),
    ]),
    MenuItem(13, "其他", "空间/配额 df", "df", []),
    MenuItem(14, "其他", "分享转存 share", "share save", [
        MenuField("link", "分享链接", "", positional=True),
        MenuField("dir", "保存目录（空=默认）", "", flag="-d"),
        MenuField("password", "访问码（链接已含则空）", "", flag="-p"),
    ]),
    MenuItem(15, "其他", "切换账号", SWITCH, [
        MenuField("account", "账号名（空=列出可选）", ""),
    ]),
    MenuItem(16, "其他", "扫码（重新）授权 auth", "auth", []),
]

MENU_BY_NUM = {str(m.num): m for m in MENU}


def build_argv(item: MenuItem, answers: dict) -> list[str]:
    """菜单答案 -> 等价命令行参数（经同一 parser 解析，保证与 CLI 同构）。纯函数。

    规则：flag 字段值为空则省略该选项（让 argparse 默认值生效）；boolean 仅在
    确认时追加选项名；multi 按空格拆分。
    """
    argv = item.action.split()
    for f in item.fields:
        v = str(answers.get(f.dest, f.default)).strip()
        if f.boolean:
            if v.lower() in ("y", "yes", "1", "true", "是"):
                argv.append(f.flag)
        elif f.positional:
            argv.extend(v.split() if f.multi else [v])
        elif v:
            argv.extend([f.flag, *v.split()] if f.multi else [f.flag, v])
    return argv


def print_menu(ctx: Ctx) -> None:
    print(f"\n══ 115 手动运维 ══  👤 账号: {ctx.account.name}")
    group = None
    for m in MENU:
        if m.group != group:
            group = m.group
            print(f"── {group} ──")
        print(f"  {m.num:>2}. {m.label}")
    print("   0. 退出")


async def menu_loop(ctx: Ctx) -> int:
    parser = build_parser()
    while True:
        print_menu(ctx)
        try:
            choice = input("编号> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice in ("0", "q", "quit", "exit"):
            return 0
        item = MENU_BY_NUM.get(choice)
        if item is None:
            print("❌ 无效编号")
            continue
        answers = {}
        cancelled = False
        for f in item.fields:
            try:
                raw = input(f"{f.prompt}" + (f" [{f.default}]" if f.default else "") + ": ")
            except (EOFError, KeyboardInterrupt):
                print()
                cancelled = True
                break
            answers[f.dest] = raw.strip()
        if cancelled:                       # 输入阶段 Ctrl+C/EOF -> 回菜单
            continue
        if item.action == SWITCH:
            name = answers.get("account", "")
            if not name:
                names = ", ".join(a.name for a in ctx.cfg.accounts)
                print(f"👤 当前: {ctx.account.name}（可选: {names}）")
                continue
            try:
                await switch_account(ctx, name)
            except Exception as e:  # noqa: BLE001
                print(f"❌ 切换失败: {e}")
            continue
        argv = build_argv(item, answers)
        try:
            args = parser.parse_args(argv)
        except SystemExit:                  # 参数缺失等 -> 回菜单不退出
            print("❌ 参数不完整，已返回菜单")
            continue
        await run_cmd(ctx, args)            # 错误已打印，回菜单继续


# ── 入口 ───────────────────────────────────────────────────────────────


def main() -> int:
    args = build_parser().parse_args()
    account = getattr(args, "account", "") or ""
    cfg = load_config()

    async def runner() -> int:
        # auth 不要求已登录（token 失效时正是要用它续授权），其余命令均需探活通过
        need_login = getattr(args, "cmd", None) != "auth"
        try:
            ctx = await build_ctx(cfg, account, need_login=need_login)
        except RuntimeError as e:
            print(f"❌ {e}")
            return 2
        try:
            if getattr(args, "cmd", None):
                return await run_cmd(ctx, args)
            return await menu_loop(ctx)
        finally:
            await ctx.cloud.close()

    return asyncio.run(runner())


if __name__ == "__main__":
    sys.exit(main())
