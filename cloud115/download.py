"""115 文件下载：downurl 响应解析 + aiohttp 流式下载到本地。

与 core/direct_dl.py 的区别：那是「HTTP 直链 -> 中转上传」的管线部件（依赖
core.app.state 走代理）；本模块是纯「115 文件 -> 本地磁盘」下载器，独立可测、
不走代理（下载直链本身是 115 CDN 地址）。

链路：find_entry 取 pc -> openapi.get_download_url(pc) -> parse_downurl 归一
-> download_file 流式写 <dest>.part（边下边算 sha1）-> 校验通过后由调用方改名落地。

download_by_pick_code 是组合入口（pc 直下，不经路径解析）：AI download_115 /
TUI 搜索下载等「搜索命中即下载」场景用它，省掉 find_entry 的逐层列目录。
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Optional

import aiofiles
import aiohttp

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_READ = 1024 * 1024


class _HttpError(RuntimeError):
    """HTTP 状态码错误（服务端明确拒绝，不值得重试）。"""


def sanitize_name(name: str) -> str:
    """净化文件名：去路径分隔符与 Windows 非法字符（空白折叠为单空格，115 文件名可含这些）。"""
    clean = re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip())
    clean = re.sub(r"\s+", " ", clean).strip().strip(".")
    return clean[:200] or "115_file"


def unique_dest(directory: Path, name: str) -> Path:
    """目录下不与现有文件重名的目标路径：重名追加 " (1)"、" (2)"…（与 workspace 副本同风格）。"""
    dest = directory / name
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    for i in range(1, 1000):
        cand = directory / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
    raise RuntimeError(f"目录下重名文件过多: {directory / name}")


def parse_downurl(item: dict) -> dict:
    """downurl 条目归一化 -> {file_name, file_size:int, pick_code, sha1:小写, url:str}。

    url 字段兼容两种形态：{"url": "..."} dict（web 形态）或纯字符串。缺 url 抛错。纯函数。
    """
    raw_url = item.get("url")
    if isinstance(raw_url, dict):
        url = str(raw_url.get("url") or "").strip()
    else:
        url = str(raw_url or "").strip()
    if not url:
        raise RuntimeError(f"downurl 条目缺 url: {str(item)[:200]}")

    def _int(v) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "file_name": str(item.get("file_name") or ""),
        "file_size": _int(item.get("file_size")),
        "pick_code": str(item.get("pick_code") or ""),
        "sha1": str(item.get("sha1") or "").lower(),
        "url": url,
    }


async def download_file(url: str, dest: Path, *, expected_size: int = 0,
                        max_attempts: int = 2, on_progress=None) -> tuple[int, str]:
    """流式下载到 <dest>.part，边下边算 sha1，返回 (字节数, sha1hex)。

    - HTTP >= 400 立即抛错（不重试，服务端明确拒绝）
    - 网络异常 / 大小不符 整体重试（默认 1 次）：115 直链带时效签名，断点续传意义
      有限，v1 整文件从头下；.part 保留在原地供排查，下次尝试覆盖重写
    - on_progress(written, total) 同步回调（total=0 表示未知）
    """
    part = dest.with_name(dest.name + ".part")
    last_err: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            sha = hashlib.sha1()
            written = 0
            timeout = aiohttp.ClientTimeout(total=None, connect=15)
            async with aiohttp.ClientSession(timeout=timeout,
                                             headers={"User-Agent": UA}) as s:
                # 若实测 403：115 可能校验 Referer，在此 headers 加
                # "Referer": "https://115.com/"
                async with s.get(url) as r:
                    if r.status >= 400:
                        raise _HttpError(f"HTTP {r.status}: {(await r.text())[:150]}")
                    total = int(r.headers.get("Content-Length") or expected_size or 0)
                    async with aiofiles.open(part, "wb") as f:
                        async for chunk in r.content.iter_chunked(_READ):
                            if not chunk:
                                continue
                            await f.write(chunk)
                            sha.update(chunk)
                            written += len(chunk)
                            if on_progress:
                                on_progress(written, total or written)
            if expected_size and written != expected_size:
                raise RuntimeError(f"大小不符: 期望 {expected_size} 实得 {written}")
            return written, sha.hexdigest()
        except _HttpError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as e:
            last_err = e
            if attempt < max_attempts:
                await asyncio.sleep(2 * attempt)
    raise last_err  # pragma: no cover - 循环内必 raise 或 return


async def download_by_pick_code(cloud, pick_code: str, dest_dir,
                                *, on_progress=None) -> dict:
    """pick_code 直下（搜索结果的 pc 不经路径解析）：downurl -> 流式下载 -> sha1 校验落地。

    与 manual.cmd_download 同一链路（sanitize -> unique_dest -> download_file），
    入口从「115 路径」换成「pick_code」——搜索命中即可下载，省掉 find_entry
    的逐层列目录（每次 1~N 个 API 调用）。AI download_115 / TUI 搜索下载共用。

    返回 {"dest": Path, "size": int, "sha1": str}；
    sha1 不符抛 RuntimeError（.part 现场保留供排查）。
    """
    info = parse_downurl(await cloud.raw.get_download_url(pick_code))
    dest_dir = Path(dest_dir).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = unique_dest(dest_dir, sanitize_name(info["file_name"] or "115_file"))
    size, sha1 = await download_file(info["url"], dest, expected_size=info["file_size"],
                                     on_progress=on_progress)
    part = dest.with_name(dest.name + ".part")
    if info["sha1"] and sha1 != info["sha1"]:
        raise RuntimeError(
            f"SHA1 不符（本地 {sha1} ≠ 115 {info['sha1']}），现场保留: {part.name}")
    part.rename(dest)
    return {"dest": dest, "size": size, "sha1": sha1}
