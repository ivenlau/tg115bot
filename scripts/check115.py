"""115 适配层冒烟测试（独立于 Telegram，便于校准开放平台实现）。

用法：
    python scripts/check115.py            # 已授权：跑完整上传链路
    python scripts/check115.py --auth     # 未授权：终端打印二维码扫码授权
    python scripts/check115.py --probe    # 轻量探活：只验 token 有效性（init.sh 用）

验证链路（每步打印结果，便于定位问题）：
  [1] 初始化(加载token)  [2] 探活  [3] 列根目录  [4] mkdir 目标目录
  [5] 生成 12MB 测试文件 + sha1（>10MB 分片，可触达 multipart）
  [6] fast_upload 首次上传 —— 期望 oss（含二次区间校验）
  [7] fast_upload 同内容再传 —— 期望 秒传 命中 🎯
  [8] upload_to_dir 全链路（建目录+上传）

任一步报错：对照 cloud115/openapi.py（API 字段）或 cloud115/oss_upload.py（OSS 协议）。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from utils.rate import RateLimiter
from cloud115.client import Cloud115Client
from cloud115.filesystem import list_dir, mkdir_p
from cloud115.oss import fast_upload
from core.uploader import upload_to_dir

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

TEST_BYTES = 12 * 1024 * 1024   # 12MB，超过 10MB 分片阈值，触达 multipart


async def do_auth(cloud: Cloud115Client) -> None:
    """终端扫码授权：终端渲染二维码（首选），存 PNG / 纯文本为降级。"""
    api = cloud.raw
    qr = await api.start_qr_auth()
    _print_qr(qr["qrcode"])
    await asyncio.sleep(5)          # 官方实现：出码后先等 5s 再开始轮询
    last = None
    while True:
        st = await api.poll_qr_status(qr["uid"], qr["time"], qr["sign"])
        if st == 2:
            await api.exchange_qr_token(qr["uid"], qr["verifier"])
            print("    ✅ 授权成功，token 已保存")
            return
        if st == -1:
            print("    ❌ 二维码已过期，请重跑")
            sys.exit(1)
        if st == -2:
            print("    ❌ 你在 APP 里取消了授权")
            sys.exit(1)
        if st == 1 and last != 1:
            print("    📱 已扫码，请在手机上点确认 …", flush=True)
        last = st
        await asyncio.sleep(2)


async def _probe(cloud: Cloud115Client) -> None:
    """轻量探活：只验证 token 真实有效，不跑上传链路（init.sh [5/7] 用）。

    token 文件存在 ≠ 有效：授权被解除(40140116)/refresh_token 失效只有真发
    请求才暴露；access_token 过期(40140125)会被自动刷新，不算失效。
    退出码：0 有效 / 1 无 token 或失效（init.sh 按此分支）。
    """
    if not cloud.raw.has_token():
        print("    ❌ 无 token")
        sys.exit(1)
    if not await cloud.ensure_login():
        print("    ❌ token 已失效（授权解除/刷新失败）或 115 不可达，需重新扫码（--auth）")
        sys.exit(1)
    print("    ✅ token 有效")


def _print_qr(data: str) -> None:
    """终端展示二维码。

    注：115 的 authDeviceCode 直接返回 QR 数据（URL 字符串），我们只需
    编码渲染（qrcode.print_ascii，纯 qrcode 核心功能，无需 PIL/OpenCV）。
    深色终端背景扫不动时，设环境变量 QR_INVERT=0 切换反色。
    """
    import os
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


async def main() -> None:
    cfg = load_config()
    rate = RateLimiter(cfg.rate115.min_interval_sec)
    cloud = Cloud115Client(cfg.primary_account, cfg.session_dir, rate)
    await cloud.init()
    try:
        await _run(cloud, cfg)
    finally:
        await cloud.close()


async def _run(cloud: Cloud115Client, cfg) -> None:
    if "--auth" in sys.argv:
        await do_auth(cloud)
        return
    if "--probe" in sys.argv:
        await _probe(cloud)
        return

    print("[1] 初始化 115 客户端 … done")
    print("[2] 探活 …")
    if not await cloud.ensure_login():
        print("    ❌ 未授权/token 失效。先跑: python scripts/check115.py --auth")
        return
    print("    ✅ 探活成功")

    print("[3] 列根目录（前 3 项）…")
    try:
        items = await list_dir(cloud, "0")
        print("    ", [i.get("fn") or i.get("file_name") for i in items[:3]])
    except Exception as e:  # noqa: BLE001
        print("    list_dir 出错（VERIFY cloud115/openapi.py list_files）:", repr(e))

    target = cfg.upload.target_dir
    print(f"[4] mkdir -p {target} …")
    try:
        cid = await mkdir_p(cloud, target)
        print(f"    cid = {cid}")
    except Exception as e:  # noqa: BLE001
        print("    mkdir_p 出错:", repr(e)); return

    work = cfg.work_dir_abs; work.mkdir(parents=True, exist_ok=True)
    test = work / "_check115.bin"
    payload = b"tg115bot-smoke-" + os.urandom(TEST_BYTES)
    test.write_bytes(payload)
    size = len(payload)
    sha1 = hashlib.sha1(payload).hexdigest()
    print(f"[5] 测试文件 {size} bytes sha1={sha1[:12]}…")

    print("[6] fast_upload 首次（期望 oss，含二次区间校验）…")
    r1 = await fast_upload(cloud, test, size, sha1, cid, "_check115_a.bin", concurrency=4)
    print("    结果:", r1.method if r1 else r1)

    print("[7] fast_upload 同内容再传（期望 秒传）…")
    r2 = await fast_upload(cloud, test, size, sha1, cid, "_check115_b.bin", concurrency=4)
    print("    结果:", r2.method if r2 else r2)
    if r2 and r2.method == "秒传":
        print("    🎯 秒传命中 —— 快通道完全可用！")

    print("[8] upload_to_dir 全链路 …")
    try:
        res = await upload_to_dir(cloud, test, size, sha1, target, "_check115_full.bin")
        print("    方法:", res.method, "cid:", res.cid)
        print("✅ 完成。")
    except Exception as e:  # noqa: BLE001
        print("    upload 出错（VERIFY cloud115/oss.py / oss_upload.py）:", repr(e))
    finally:
        try:
            test.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
