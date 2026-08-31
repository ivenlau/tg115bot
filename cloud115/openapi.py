"""115 开放平台 API 手写客户端（aiohttp 原生异步，零 p115client/p115oss 依赖）。

实现对照参考项目 telegram-115bot（app/core/open_115.py）逐字段校准，协议自包含：

  鉴权    PKCE(S256) 扫码授权 -> access_token / refresh_token（Bearer 头）
  刷新    POST /open/refreshToken；业务返回 40140125 时自动刷新并重试一次
  目录    GET  /open/folder/get_info (path -> file_id/cid，带缓存)
          POST /open/folder/add     (建目录；code 20004 = 已存在视为成功)
  上传    POST /open/upload/init    (秒传探测：fileid=sha1、target=U_1_{cid}；
          data.status==2 命中；sign_key/sign_check 需二次区间 SHA1 校验)
          GET  /open/upload/get_token (OSS STS 临时凭证)
  列表    GET  /open/ufile/files
  扫码    POST passportapi /open/authDeviceCode (client_id+PKCE challenge)
          GET  qrcodeapi  /get/status/  (data.status: None=待扫 1=已扫 2=已确认; state 0=失效)
          POST passportapi /open/deviceCodeToToken (uid+code_verifier -> tokens)

token 以 JSON 落盘（access_token/refresh_token），可经 utils.crypto 加密。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

import aiohttp

from utils.crypto import decrypt_if_possible, encrypt
from utils.rate import RateLimiter

log = logging.getLogger(__name__)

BASE_API = "https://proapi.115.com"
BASE_PASSPORT = "https://passportapi.115.com"
QR_STATUS_URL = "https://qrcodeapi.115.com/get/status/"
# p115client 同款公共测试 AppID；有自己开放平台应用的在 config.accounts[].app_id 覆盖
DEFAULT_APP_ID = 100195125

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 令牌相关错误码（参考 telegram-115bot handle_token_expiry）
CODE_TOKEN_EXPIRED = 40140125      # 可刷新后重试
CODE_NEED_REAUTH = (40140116, 40140119)   # access_token 彻底失效，需重新扫码


class AuthRequiredError(RuntimeError):
    """需要重新扫码授权（refresh_token 也失效）。"""


def make_pkce_pair() -> tuple[str, str]:
    """生成 (code_verifier, code_challenge)，RFC 7636 S256。"""
    verifier = base64.urlsafe_b64encode(os.urandom(64)).rstrip(b"=").decode()
    verifier = re.sub(r"[^A-Za-z0-9\-._~]", "", verifier)[:64]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class Open115Client:
    """单账号开放平台客户端。token_file 每账号一个。"""

    def __init__(self, token_file: Path, *, app_id: int = 0,
                 secret_key: str = "", rate: Optional[RateLimiter] = None):
        self.token_file = Path(token_file)
        self.app_id = int(app_id or DEFAULT_APP_ID)
        self._secret_key = secret_key
        self._rate = rate or RateLimiter(0.5)
        self._session: Optional[aiohttp.ClientSession] = None
        self.access_token = ""
        self.refresh_token = ""
        self._path_cache: Dict[str, dict] = {}      # path -> file_info（脏缓存主动失效）
        self._last_code_ts = 0.0                    # 最近一次 proapi 调用时间（探活限速用）
        # 日请求计数（风控防御，对照 telegram-115bot：普通用户 10000/日，终身会员 15000）
        self.request_count = 0
        self.daily_limit = 9500                     # 0.95 安全阈值
        self._count_date = time.localtime().tm_yday

    # ── 生命周期 ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": UA},
                # 只限制连接建立，不限总时长：OSS 大分片（>=10MB）串行上传远超 60s
                timeout=aiohttp.ClientTimeout(total=None, connect=15, sock_read=None),
            )
        self._load_token()

    def upload_session(self) -> aiohttp.ClientSession:
        """OSS 上传专用 session（共享连接池；与 API session 分离便于独立调优）。"""
        return self.session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise RuntimeError("Open115Client 未 start() 或已 close()")
        return self._session

    # ── token 持久化（可加密） ────────────────────────────────────────────
    def _load_token(self) -> None:
        if not self.token_file.exists():
            return
        try:
            data = json.loads(self.token_file.read_text("utf-8"))
            self.access_token = decrypt_if_possible(str(data.get("access_token", "")), self._secret_key)
            self.refresh_token = decrypt_if_possible(str(data.get("refresh_token", "")), self._secret_key)
        except Exception as e:  # noqa: BLE001
            log.warning("读取 token 文件失败 %s: %r", self.token_file, e)

    def _save_token(self) -> None:
        sk = self._secret_key
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps({
            "access_token": encrypt(self.access_token, sk) if sk else self.access_token,
            "refresh_token": encrypt(self.refresh_token, sk) if sk else self.refresh_token,
            "saved_at": time.time(),
        }, ensure_ascii=False), "utf-8")

    def has_token(self) -> bool:
        return bool(self.access_token or self.refresh_token)

    # ── HTTP 基础 ─────────────────────────────────────────────────────────
    async def _request(self, method: str, url: str, *, params=None, data=None,
                       auth: bool = True, _retry: bool = True) -> Dict[str, Any]:
        """统一请求：限速 + Bearer + 40140125 自动刷新重试一次。返回 json dict。"""
        headers = {}
        if auth:
            if not self.access_token and self.refresh_token:
                await self.refresh_access_token()   # 启动时只有 refresh_token 也能换
            headers["Authorization"] = f"Bearer {self.access_token}"
        self._count_request()
        if BASE_API in url and self.request_count > self.daily_limit:
            raise RuntimeError(f"115 日请求已达安全阈值({self.daily_limit})，为避风控暂停 API 调用，0 点自动恢复")
        await self._rate.acquire()
        async with self.session.request(method, url, params=params, data=data, headers=headers) as r:
            text = await r.text()
        try:
            resp = json.loads(text)
        except ValueError:
            resp = {"state": False, "code": r.status, "message": text[:200]}
        if not isinstance(resp, dict):
            return {"state": False, "code": r.status, "message": str(resp)[:200]}

        code = resp.get("code")
        if auth and _retry and code == CODE_TOKEN_EXPIRED:
            log.info("access_token 过期(40140125)，刷新后重试")
            await self.refresh_access_token()
            return await self._request(method, url, params=params, data=data, auth=True, _retry=False)
        if auth and code in CODE_NEED_REAUTH:
            raise AuthRequiredError(f"115 令牌失效(code={code})，请 /auth 重新扫码授权")
        return resp

    @staticmethod
    def _ok(resp: Dict[str, Any]) -> bool:
        """proapi 成功判定：code==0（passport 接口 state==True 时 code 也为 0）。"""
        return resp.get("code") == 0 or resp.get("state") is True

    def _count_request(self) -> None:
        """日请求计数（跨天自动重置）。"""
        today = time.localtime().tm_yday
        if today != self._count_date:
            log.info("115 日请求计数重置（昨日 %d 次）", self.request_count)
            self.request_count = 0
            self._count_date = today
        self.request_count += 1

    # ── token 刷新 ───────────────────────────────────────────────────────
    async def refresh_access_token(self) -> bool:
        if not self.refresh_token:
            return False
        await self._rate.acquire()
        async with self.session.post(
            f"{BASE_PASSPORT}/open/refreshToken",
            data={"refresh_token": self.refresh_token},
            headers={"User-Agent": UA},
        ) as r:
            try:
                resp = await r.json()
            except Exception:  # noqa: BLE001
                resp = {}
        data = (resp or {}).get("data") or {}
        if (resp or {}).get("state") and data.get("access_token"):
            self.access_token = data["access_token"]
            self.refresh_token = data.get("refresh_token", self.refresh_token)
            self._save_token()
            log.info("115 access_token 已刷新")
            return True
        log.warning("刷新 access_token 失败: %s", str(resp)[:200])
        return False

    # ── 扫码授权（PKCE） ─────────────────────────────────────────────────
    async def start_qr_auth(self) -> Dict[str, Any]:
        """发起扫码授权，返回 {uid, time, sign, qrcode, verifier}。"""
        verifier, challenge = make_pkce_pair()
        resp = await self._request(
            "POST", f"{BASE_PASSPORT}/open/authDeviceCode",
            data={
                "client_id": self.app_id,
                "code_challenge": challenge,
                "code_challenge_method": "sha256",
            }, auth=False,
        )
        data = resp.get("data") or {}
        if not (self._ok(resp) and data.get("uid") is not None):
            raise RuntimeError(f"获取扫码二维码失败: {str(resp)[:200]}")
        return {"uid": data["uid"], "time": data["time"], "sign": data["sign"],
                "qrcode": data["qrcode"], "verifier": verifier}

    async def poll_qr_status(self, uid, t, sign) -> Optional[int]:
        """轮询扫码状态（data.status 语义，对照 p115client 官方轮询实现）：
        0=待扫码  1=已扫待确认  2=已确认  -1=二维码过期  -2=用户取消
        网络异常/响应异常返回 None（调用方继续轮询，不当失效）。"""
        try:
            await self._rate.acquire()
            async with self.session.get(
                QR_STATUS_URL, params={"uid": uid, "time": t, "sign": sign},
                headers={"User-Agent": UA},
            ) as r:
                resp = await r.json(content_type=None)
        except Exception:  # noqa: BLE001 -- 网络抖动不算失效
            return None
        data = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(data, dict):
            return None
        return data.get("status")

    async def exchange_qr_token(self, uid, verifier: str) -> bool:
        """扫码确认后换取 access_token/refresh_token 并落盘。"""
        resp = await self._request(
            "POST", f"{BASE_PASSPORT}/open/deviceCodeToToken",
            data={"uid": uid, "code_verifier": verifier}, auth=False,
        )
        data = resp.get("data") or {}
        if data.get("access_token"):
            self.access_token = data["access_token"]
            self.refresh_token = data.get("refresh_token", "")
            self._save_token()
            log.info("115 扫码授权成功，token 已保存: %s", self.token_file)
            return True
        raise RuntimeError(f"换取 token 失败: {str(resp)[:200]}")

    # ── 文件系统 ─────────────────────────────────────────────────────────
    async def get_file_info(self, path: str, *, use_cache: bool = True) -> Optional[dict]:
        """路径 -> 信息 dict（file_id 即 cid）。不存在返回 None。"""
        path = "/" + (path or "").strip("/")
        if path == "/":
            return {"file_id": 0, "file_name": "/", "file_category": "0"}
        if use_cache and path in self._path_cache:
            return self._path_cache[path]
        resp = await self._request(
            "GET", f"{BASE_API}/open/folder/get_info", params={"path": path}
        )
        if not self._ok(resp):
            return None
        info = resp.get("data") or {}
        if not isinstance(info, dict) or not info:
            return None
        self._path_cache[path] = info
        return info

    def invalidate_path_cache(self, path: str = "") -> None:
        if path:
            self._path_cache.pop(path, None)
            # 父路径也可能受影响：把以 path 为前缀的都清掉
            for k in [k for k in self._path_cache if k == path or k.startswith(path.rstrip("/") + "/")]:
                del self._path_cache[k]
        else:
            self._path_cache.clear()

    async def create_dir(self, pid: int, name: str) -> bool:
        """在 pid 下建目录 name。已存在(code 20004)视为成功。"""
        resp = await self._request(
            "POST", f"{BASE_API}/open/folder/add",
            data={"pid": pid, "file_name": name},
        )
        if self._ok(resp) or resp.get("code") == 20004:
            return True
        log.warning("create_dir(%s, %s) 失败: %s", pid, name, str(resp)[:200])
        return False

    async def create_dir_recursive(self, path: str) -> int:
        """递归创建目录，返回目标 cid（int）。路径已存在直接返回其 id。"""
        path = "/" + (path or "").strip("/")
        if path == "/":
            return 0
        self.invalidate_path_cache(path)
        info = await self.get_file_info(path, use_cache=False)
        if info and info.get("file_id") is not None:
            return int(info["file_id"])

        parts = [p for p in path.split("/") if p]
        pid = 0
        cur = ""
        for name in parts:
            cur = f"{cur}/{name}"
            info = await self.get_file_info(cur, use_cache=False)
            if info and info.get("file_id") is not None:
                pid = int(info["file_id"])
                continue
            ok = await self.create_dir(pid, name)
            if not ok:
                raise RuntimeError(f"创建目录失败: {cur}")
            # 建目录后服务端有延迟，重试取 cid
            info = await self._get_info_retry(cur)
            pid = int(info["file_id"])
        return pid

    async def _get_info_retry(self, path: str, tries: int = 3, delay: float = 0.5) -> dict:
        for i in range(tries):
            info = await self.get_file_info(path, use_cache=False)
            if info and info.get("file_id") is not None:
                return info
            await asyncio.sleep(delay)
        raise RuntimeError(f"建目录后取不到 cid: {path}")

    async def list_files(self, cid: int = 0, limit: int = 32, offset: int = 0) -> Dict[str, Any]:
        """列目录（直接子项：目录+文件）。返回 {list: [...], count?: N}。

        ⚠️ show_dir=1 必带（与 search_files 一致）：实测（2026-08）缺省时不返回
        目录、且列表呈「递归文件」形态——子目录里的文件会混入父级列表。
        data 可能直接是数组或 {list: [...]}（两种形态都出现过）。
        """
        resp = await self._request(
            "GET", f"{BASE_API}/open/ufile/files",
            params={"cid": cid, "limit": limit, "offset": offset, "show_dir": 1},
        )
        if not self._ok(resp):
            return {"list": [], "error": str(resp)[:200]}
        data = resp.get("data")
        if isinstance(data, list):            # 有的版本 data 直接是条目数组
            return {"list": data}
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            return data
        return {"list": []}

    async def list_files_all(self, cid: int = 0, limit: int = 100,
                             max_items: int = 5000) -> list:
        """翻页取目录全部条目。终止条件（任一）：批 < limit / 累计 >= count / max_items 兜底
        （防 API 忽略 offset 时死循环）。页间不额外 sleep——_request 内已有 RateLimiter。"""
        items: list = []
        offset = 0
        while len(items) < max_items:
            data = await self.list_files(cid, limit, offset)
            batch = data.get("list") or []
            if not batch:
                break
            items.extend(batch)
            count = int(data.get("count") or 0)
            if len(batch) < limit or (count and len(items) >= count):
                break
            offset += len(batch)
        return items[:max_items]

    # ── 上传 ─────────────────────────────────────────────────────────────
    async def upload_init(self, file_name: str, file_size: int, sha1: str, cid: int,
                          sign_key: str = "", sign_val: str = "") -> Dict[str, Any]:
        """秒传探测 / 取 OSS 参数。返回 data dict：
        status==2 秒传命中；否则含 bucket/object/callback/pick_code；
        含 sign_key+sign_check 时需二次区间 SHA1 后带 sign_key/sign_val 重调。"""
        data = {
            "file_name": file_name,
            "file_size": file_size,
            "target": f"U_1_{cid}",
            "fileid": sha1,
        }
        if sign_key and sign_val:
            data["sign_key"] = sign_key
            data["sign_val"] = sign_val
        resp = await self._request("POST", f"{BASE_API}/open/upload/init", data=data)
        if not self._ok(resp):
            raise RuntimeError(f"upload/init 失败: {str(resp)[:200]}")
        return resp.get("data") or {}

    async def get_upload_token(self) -> Dict[str, str]:
        """取 OSS STS 临时凭证：AccessKeyId/AccessKeySecret/SecurityToken/endpoint。"""
        resp = await self._request("GET", f"{BASE_API}/open/upload/get_token")
        if not self._ok(resp):
            raise RuntimeError(f"upload/get_token 失败: {str(resp)[:200]}")
        data = resp.get("data") or {}
        for k in ("AccessKeyId", "AccessKeySecret", "SecurityToken"):
            if not data.get(k):
                raise RuntimeError(f"STS 凭证缺字段 {k}")
        return data

    # ── 离线下载（对照 telegram-115bot open_115.py 逐字段） ─────────────
    async def offline_add(self, url: str, save_path: str) -> bool:
        """添加离线任务到指定目录。⚠️ urls 字段是单个 URL 字符串（尽管名字是复数）。

        返回 True 成功；目录不存在会自动递归创建（含建后延迟重试）。
        """
        wp_path_id = await self.create_dir_recursive(save_path)
        resp = await self._request(
            "POST", f"{BASE_API}/open/offline/add_task_urls",
            data={"urls": url, "wp_path_id": wp_path_id},
        )
        if self._ok(resp):
            log.info("离线任务已添加: %s -> %s", url[:80], save_path)
            return True
        raise RuntimeError(f"添加离线任务失败: {str(resp)[:200]}")

    async def offline_list(self, page: int = 1) -> Dict[str, Any]:
        """取一页离线任务。返回 data（含 tasks/page_count）。字段（缩写）：
        name/url/status(-1失败 1进行 2完成)/percentDone/info_hash/file_id/wp_path_id/delete_file_id"""
        resp = await self._request(
            "GET", f"{BASE_API}/open/offline/get_task_list", params={"page": page}
        )
        if not self._ok(resp):
            return {"tasks": [], "page_count": 0, "error": str(resp)[:200]}
        return resp.get("data") or {"tasks": [], "page_count": 0}

    async def offline_list_all(self) -> list:
        """翻页取全部离线任务（页间 sleep 2s 避风控）。"""
        first = await self.offline_list(1)
        tasks = list(first.get("tasks") or [])
        for page in range(2, int(first.get("page_count", 1)) + 1):
            await asyncio.sleep(2)
            data = await self.offline_list(page)
            tasks.extend(data.get("tasks") or [])
        return tasks

    async def offline_del(self, info_hash: str, del_source_file: int = 0) -> bool:
        """删除离线任务记录。del_source_file: 1=连已下载文件一起删 0=仅清任务记录。"""
        resp = await self._request(
            "POST", f"{BASE_API}/open/offline/del_task",
            data={"info_hash": info_hash, "del_source_file": del_source_file},
        )
        if self._ok(resp):
            return True
        log.warning("删除离线任务失败: %s", str(resp)[:200])
        return False

    async def offline_quota(self) -> Dict[str, Any]:
        """离线下载配额：{used, count}。"""
        resp = await self._request("GET", f"{BASE_API}/open/offline/get_quota_info")
        if not self._ok(resp):
            return {}
        return resp.get("data") or {}

    @staticmethod
    def offline_done(task: Dict[str, Any]) -> bool:
        """完成判定（对照 check_offline_download_success）：status==2 或 percentDone==100。"""
        return task.get("status") == 2 or task.get("percentDone") == 100

    @staticmethod
    def offline_failed(task: Dict[str, Any]) -> bool:
        return task.get("status") == -1

    async def search_files(self, keyword: str, limit: int = 20) -> Dict[str, Any]:
        """全盘搜索文件/目录。GET /open/ufile/search（上限 1 万条，count 仅作参考）。"""
        resp = await self._request(
            "GET", f"{BASE_API}/open/ufile/search",
            params={"aid": 1, "cid": 0, "limit": limit, "offset": 0,
                    "show_dir": 1, "search_value": keyword},
        )
        if not self._ok(resp):
            return {"list": [], "error": str(resp)[:200]}
        data = resp.get("data")
        if isinstance(data, list):
            return {"list": data}
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            return data
        return {"list": []}

    async def delete_files(self, file_ids) -> bool:
        """删除文件/目录（入回收站）。POST /open/ufile/delete，file_ids 逗号分隔。"""
        if isinstance(file_ids, (list, tuple)):
            file_ids = ",".join(str(i) for i in file_ids)
        resp = await self._request(
            "POST", f"{BASE_API}/open/ufile/delete", data={"file_ids": file_ids},
        )
        if self._ok(resp):
            return True
        raise RuntimeError(f"删除失败: {str(resp)[:200]}")

    async def move_files(self, file_ids, to_cid: int) -> bool:
        """移动文件/目录到 to_cid。POST /open/ufile/move。"""
        if isinstance(file_ids, (list, tuple)):
            file_ids = ",".join(str(i) for i in file_ids)
        resp = await self._request(
            "POST", f"{BASE_API}/open/ufile/move",
            data={"file_ids": file_ids, "to_cid": to_cid},
        )
        if self._ok(resp):
            return True
        raise RuntimeError(f"移动失败: {str(resp)[:200]}")

    async def rename_file(self, file_id, new_name: str) -> bool:
        """重命名文件/目录。POST /open/ufile/update（file_id + file_name）。"""
        resp = await self._request(
            "POST", f"{BASE_API}/open/ufile/update",
            data={"file_id": file_id, "file_name": new_name},
        )
        if self._ok(resp):
            # 路径缓存里旧名/新名都可能已脏，全清最稳
            self.invalidate_path_cache()
            return True
        raise RuntimeError(f"重命名失败: {str(resp)[:200]}")

    async def get_download_url(self, pick_code: str) -> Dict[str, Any]:
        """取文件下载直链。POST /open/ufile/downurl（form: pick_code）。

        响应 data 是单条目 map，值含 file_name/file_size/pick_code/sha1/url
        （url 可能是 {"url": ...} 或纯字符串，由 cloud115.download.parse_downurl 归一）。
        ⚠️ 实测（2026-08）map 的 key 并不总是 pick_code（可能是内部 file_id），
        故按「含 url 的 dict 值」取条目，不按 key 匹配。
        """
        resp = await self._request(
            "POST", f"{BASE_API}/open/ufile/downurl", data={"pick_code": pick_code},
        )
        if not self._ok(resp):
            raise RuntimeError(f"获取下载地址失败: {str(resp)[:200]}")
        data = resp.get("data")
        if isinstance(data, dict):
            if data.get("url"):                   # 容错：无外层 key 的形态
                return data
            for v in data.values():               # 实测形态：{<内部id>: {…url…}}
                if isinstance(v, dict) and v.get("url"):
                    return v
        raise RuntimeError(f"downurl 响应缺条目 {pick_code}: {str(resp)[:200]}")

    async def user_space(self) -> Dict[str, Any]:
        """空间用量：{used, total}（字节）。"""
        resp = await self._request("GET", f"{BASE_API}/open/user/info")
        if not self._ok(resp):
            return {}
        data = resp.get("data") or {}
        # 字段名各版本差异，多候选容错：老形态 used_size/size_total；
        # 新形态 rt_space_info.{all_use,all_total}.size（2026-08 实测）
        rt = data.get("rt_space_info") or {}
        return {
            "used": data.get("used_size") or data.get("space_used")
                or (rt.get("all_use") or {}).get("size") or 0,
            "total": data.get("size_total") or data.get("space_total")
                or (rt.get("all_total") or {}).get("size") or 0,
        }

    # ── 探活 ─────────────────────────────────────────────────────────────
    async def check_login(self) -> bool:
        try:
            # 探活必须真发请求：get_file_info("/") 有根路径短路特判（不发网络），
            # 走轻量 user/info 才能暴露 token 失效（如 40140116 授权已解除）
            return bool(await self.user_space())
        except Exception as e:  # noqa: BLE001
            log.warning("115 探活失败: %r", e)
            return False
