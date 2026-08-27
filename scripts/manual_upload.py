"""手动上传本地文件到 115（独立于 Telegram，走与 bot 相同的上传链路）。

用法：
    python scripts/manual_upload.py <文件或目录> [-d /tg115bot/目标目录]

    文件：直接上传（自动算 SHA1，秒传命中时秒完成）
    目录：递归扫描全部文件，按相对路径逐个上传到 目标目录/<相对路径>/
    账号：用 config.accounts 的第一个（primary）；未授权时先跑
          python scripts/check115.py --auth 扫码授权

示例：
    python scripts/manual_upload.py movie.mkv
    python scripts/manual_upload.py /data/photos -d /tg115bot/photos
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config                      # noqa: E402
from utils.rate import RateLimiter                  # noqa: E402
from cloud115.client import Cloud115Client          # noqa: E402
from cloud115.filesystem import mkdir_p             # noqa: E402
from core.progress import human_bytes               # noqa: E402
from core.uploader import upload_to_dir             # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("manual_upload")

_READ = 1024 * 1024


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


async def main() -> int:
    ap = argparse.ArgumentParser(description="手动上传本地文件到 115")
    ap.add_argument("source", help="要上传的文件或目录")
    ap.add_argument("-d", "--dir", default="", help="115 目标目录（默认 upload.target_dir）")
    ap.add_argument("--account", default="", help="用哪个账号（默认第一个）")
    args = ap.parse_args()

    src = Path(args.source).expanduser()
    if not src.exists():
        print(f"❌ 路径不存在: {src}")
        return 1

    cfg = load_config()
    if args.account:
        acct_cfg = next((a for a in cfg.accounts if a.name == args.account), None)
        if acct_cfg is None:
            print(f"❌ 账号不存在: {args.account}（可选: {', '.join(a.name for a in cfg.accounts)}）")
            return 1
    else:
        acct_cfg = cfg.primary_account
    target_root = args.dir or cfg.upload.target_dir

    rate = RateLimiter(cfg.rate115.min_interval_sec)
    cloud = Cloud115Client(acct_cfg, cfg.session_dir, rate)
    await cloud.init()
    try:
        if not await cloud.ensure_login():
            print("❌ 账号未授权/token 失效。先跑: python scripts/check115.py --auth")
            return 1
        print(f"👤 账号 {acct_cfg.name} 就绪，目标目录: {target_root}")

        files = collect_files(src)
        base = src if src.is_dir() else src.parent
        ok = fail = 0
        for i, f in enumerate(files, 1):
            rel = f.relative_to(base).parent
            # 目录上传时保持相对路径结构；文件上传用原名
            remote_dir = str(target_root.rstrip("/") + ("/" + rel.as_posix() if str(rel) != "." else ""))
            name = f.name
            size, sha1 = await sha1_of(f)
            print(f"[{i}/{len(files)}] {name} ({human_bytes(size)}) -> {remote_dir}")
            try:
                # upload_to_dir 内部已含 mkdir_p + 秒传/OSS 全链路 + 退避重试
                result = await upload_to_dir(cloud, f, size, sha1, remote_dir, name,
                                             oss_concurrency=cfg.upload.oss_concurrency)
                print(f"    ✅ {result.method}")
                ok += 1
            except Exception as e:  # noqa: BLE001
                print(f"    ❌ 失败: {e}")
                log.debug("上传失败: %s", f, exc_info=True)
                fail += 1
        print(f"\n完成：成功 {ok} / 失败 {fail} / 共 {len(files)}")
        return 1 if fail else 0
    finally:
        await cloud.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
