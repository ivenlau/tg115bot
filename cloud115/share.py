"""115 分享链接转存（webapi + Cookie；可选功能）。

⚠️ 为什么用 Cookie：转存接口（share/snap + share/receive）只存在于 webapi
（Cookie 鉴权），115 开放平台（proapi/Bearer）**没有**对应端点（已对照
p115client 源码确认 P115OpenClient 无 share 方法）。故本功能独立于 open 主链路，
仅在 config.share.cookies 配置了浏览器 Cookie 时启用——不影响上传/离线等主功能。

协议对照 TgtoDrive（tgto115.py）：
  列文件  GET  webapi.115.com/share/snap?share_code=&receive_code=&cid=&offset=&limit=
  转存    POST webapi.115.com/share/receive
          {user_id, share_code, receive_code, file_id(逗号分隔), cid}
          "文件已接收，无需重复接收" 视为成功
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

import aiohttp

log = logging.getLogger(__name__)

WEB_API = "https://webapi.115.com"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 分享链接：115.com/115cdn/anxia.com /s/<code>?password=<pwd>
_SHARE_RE = re.compile(
    r"https?://(?:\w+\.)?(?:115\.com|115cdn\.com|anxia\.com)/s/([0-9a-zA-Z]+)"
    r"(?:\?password=([0-9a-zA-Z]+))?",
    re.IGNORECASE,
)


def parse_share_link(text: str) -> Optional[Tuple[str, str]]:
    """从文本提取 (share_code, receive_code)。密码缺失时 receive_code 为空串。"""
    t = (text or "").strip()
    if "\n" in t or len(t) > 512:
        return None
    m = _SHARE_RE.search(t)
    if not m:
        return None
    return m.group(1), m.group(2) or ""


def _uid_from_cookies(cookies: str) -> str:
    """从 Cookie 串取 UID。"""
    m = re.search(r"UID=(\d+)", cookies or "")
    return m.group(1) if m else ""


def _headers(cookies: str) -> Dict[str, str]:
    return {"User-Agent": UA, "Cookie": cookies}


async def share_list(cookies: str, share_code: str, receive_code: str,
                     limit_each: int = 100) -> Tuple[dict, List[dict]]:
    """列分享内全部文件。返回 (share_info, files)。"""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        files: List[dict] = []
        share_info: dict = {}
        offset = 0
        while True:
            url = (f"{WEB_API}/share/snap?share_code={share_code}"
                   f"&receive_code={receive_code}&cid=&offset={offset}&limit={limit_each}")
            async with s.get(url, headers=_headers(cookies)) as r:
                data = await r.json(content_type=None)
            if not data.get("state"):
                raise RuntimeError(f"读取分享失败: {data.get('error', str(data)[:120])}")
            d = data.get("data") or {}
            share_info = d.get("shareinfo") or share_info
            batch = d.get("list") or []
            files.extend(batch)
            count = int(d.get("count") or len(files))
            offset += len(batch)
            if not batch or len(files) >= count:
                break
    return share_info, files


async def share_receive(cookies: str, share_code: str, receive_code: str,
                        file_ids: List[str], cid: int) -> bool:
    """转存文件到 cid。"无需重复接收" 视为成功。"""
    uid = _uid_from_cookies(cookies)
    payload = {
        "user_id": uid,
        "share_code": share_code,
        "receive_code": receive_code,
        "file_id": ",".join(file_ids),
        "cid": cid,
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        async with s.post(f"{WEB_API}/share/receive", data=payload,
                          headers=_headers(cookies)) as r:
            data = await r.json(content_type=None)
    if data.get("state"):
        return True
    err = str(data.get("error", ""))
    if "无需重复接收" in err:
        return True
    raise RuntimeError(f"转存失败: {err[:150]}")
