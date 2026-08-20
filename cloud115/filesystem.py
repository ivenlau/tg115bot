"""115 文件系统操作（开放平台）：路径->cid、递归建目录、列目录。

基于 cloud115/openapi.py 的 Open115Client（自带 path 缓存 + 建目录延迟重试）。
字段说明：get_info 返回 file_id（即 cid）；列表条目为缩写字段（fn=名 fid=id fs=size pc=pickcode）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

ROOT_CID = "0"


def _api(cloud):
    return cloud.raw


async def resolve_cid(cloud, path: str) -> str:
    """路径 -> cid（字符串）。不存在抛 FileNotFoundError。"""
    path = (path or "").strip() or "/"
    if path == "/":
        return ROOT_CID
    info = await _api(cloud).get_file_info(path)
    if not info or info.get("file_id") is None:
        raise FileNotFoundError(f"115 路径不存在: {path}")
    return str(info["file_id"])


async def mkdir_p(cloud, path: str) -> str:
    """递归创建目录（已存在直接返回），返回 cid。"""
    path = (path or "").strip() or "/"
    if path == "/":
        return ROOT_CID
    cid = await _api(cloud).create_dir_recursive(path)
    return str(cid)


async def list_dir(cloud, cid: str = ROOT_CID) -> List[Dict[str, Any]]:
    """列目录。返回条目 dict 列表（fn/fid/pid/fc/fs/pc 缩写字段）。"""
    data = await _api(cloud).list_files(int(cid) if str(cid).isdigit() else 0)
    items = data.get("list") or []
    return list(items)


async def upload_file(cloud, target_cid: str, local_path, filename: str) -> Dict[str, Any]:
    """高层上传（开放平台全链路：秒传 + 二次校验 + OSS 直传）。见 oss.fast_upload。"""
    from cloud115.oss import fast_upload
    result = await fast_upload(cloud, local_path, 0, "", target_cid, filename)
    if result is None:
        raise RuntimeError("上传失败（fast_upload 返回 None）")
    return {"method": result.method, "detail": result.detail}
