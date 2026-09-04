"""阿里云 OSS 上传（115 直传通道）：V1 HMAC-SHA1 签名 + multipart 分片并发。

协议逐行对照 p115oss 0.0.9.1（/tmp/ossdl）与 telegram-115bot/app/utils/alioss.py：
  - StringToSign = METHOD\\nContent-MD5\\nContent-Type\\nDate\\n<sorted x-oss-* headers>\\n/bucket<path>?<query>
  - Authorization: "OSS {AccessKeyId}:{base64(hmac_sha1(AccessKeySecret, sts))}"
  - 请求头须带 x-oss-security-token（STS）
  - init    POST {obj}?sequential=1&uploads=1          -> XML <UploadId>（顺序分片模式）
            ⚠️ sequential 模式要求分片严格按序提交，故单文件分片为串行（并发会 PartNotSequential）
  - 分片    PUT  {obj}?partNumber=N&uploadId=ID        -> 响应头 ETag
  - complete POST {obj}?uploadId=ID + XML body(PartNumber/ETag)
            + x-oss-callback / x-oss-callback-var（base64，来自 upload/init 的 callback）
  - 小文件(<=10MB) 单次 PUT（带 callback），等价 telegram-115bot 的 put_object_from_file；
    大文件分片因 sequential 约束按序串行（单流吞吐 = OSS 分片速度）
  - 分片大小 >=10MB，翻倍直到片数 <=10000（determine_partsize）

取消语义：cancel_event 置位时抛 core.queue.TaskCancelled（与 pipeline/queue 对齐），
不使用 asyncio.CancelledError（那是 BaseException，会击穿 worker 的 except Exception）。

签名/分片/URL 构造均为纯函数，tests/ 有单测。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import re
from email.utils import formatdate
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp
from aiofiles import open as aiofiles_open

from core.queue import TaskCancelled
from utils.rate import with_backoff

log = logging.getLogger(__name__)

ProgressCb = Callable[[int, int], Awaitable[None]]
DEFAULT_REGION_HOST = "oss-cn-shenzhen.aliyuncs.com"
MIN_PART_SIZE = 1024 * 1024 * 10          # OSS/115 要求 >=10MB
MAX_PART_COUNT = 10 ** 4
_READ = 1024 * 1024

_UPLOAD_ID_RE = re.compile(r"<UploadId>([^<]+)</UploadId>")

# STS 凭证相关 OSS 错误码(均对应 HTTP 403)—— 触发 token 刷新 + list_parts 续传
_STS_ERROR_CODES = frozenset({
    "InvalidAccessKeyId", "SecurityTokenExpired", "InvalidSecurityToken",
})

# OSS ListPartsResult XML 解析(>1000 part 才翻页,本期不翻页)
_PART_RE = re.compile(
    r"<Part>\s*<PartNumber>(\d+)</PartNumber>\s*<ETag>([^<]*)</ETag>",
    re.DOTALL,
)
_IS_TRUNCATED_RE = re.compile(r"<IsTruncated>(true|1)</IsTruncated>", re.IGNORECASE)


class _StsExpiredError(Exception):
    """OSS 返回 403 + STS Code,触发 token 刷新 + 续传。

    加入 with_backoff.no_retry,避免被重试同一过期 token;由 _put_parts 捕获后走续传路径。
    """

    def __init__(self, status: int, text: str):
        super().__init__(oss_error_summary(status, text))
        self.status = status
        self.text = text


class AuthRequiredError(RuntimeError):
    """连续多次 STS refresh 失败,需外层走账号冷却/重新登录流程。"""


class _SessionDead(Exception):
    """OSS multipart session 已死(NoSuchUpload),需重新 init + 全传。
    由 _multipart_list_parts 抛,_put_parts 主循环 catch 后从头开始。
    """


# 单文件内连续 refresh 失败上限;达到后抛 AuthRequiredError
MAX_REFRESH_FAILS = 3


# ── 纯函数 ─────────────────────────────────────────────────────────────────
def determine_partsize(size: int) -> int:
    """分片大小：>=10MB，翻倍直到片数 <= 10000（照抄 p115oss.determine_partsize）。"""
    if size <= MIN_PART_SIZE:
        return MIN_PART_SIZE
    n = -(-size // MAX_PART_COUNT)
    partsize = MIN_PART_SIZE
    while partsize < n:
        partsize <<= 1
    return partsize


def object_url(endpoint: str, bucket: str, obj: str) -> str:
    """对象访问 URL：{scheme}://{bucket}.{endpoint_host}/{object}。"""
    endpoint = (endpoint or "").strip() or f"http://{DEFAULT_REGION_HOST}"
    if "://" not in endpoint:
        endpoint = "http://" + endpoint
    scheme, _, host = endpoint.partition("://")
    host = host.rstrip("/")
    return f"{scheme}://{bucket}.{host}/{quote(obj, safe='/')}"


def oss_v1_string_to_sign(method: str, url: str, headers: Dict[str, str]) -> str:
    """由（已含 date/x-oss-* 的）头构造 StringToSign；oss_v1_sign 内部同源，供诊断。"""
    urlp = urlsplit(url)
    bucket = (urlp.hostname or "").partition(".")[0]
    path_qs = urlunsplit(("", "", urlp.path, urlp.query, ""))
    xoss = sorted((k, v) for k, v in headers.items() if k.startswith("x-oss-"))
    return "\n".join([
        method.upper(),
        headers.get("content-md5", ""),
        headers.get("content-type", ""),
        headers["date"],
        "\n".join(f"{k}:{v}" for k, v in xoss),
        f"/{bucket}{path_qs}",
    ])


def oss_v1_sign(method: str, url: str, token: Dict[str, str],
                extra_headers: Optional[Dict[str, str]] = None,
                *, date: str = "") -> Dict[str, str]:
    """OSS V1 签名，返回带 Authorization 的完整请求头（纯函数，date 可注入便于测试）。"""
    headers = {k.lower(): v for k, v in (extra_headers or {}).items()}
    headers["x-oss-security-token"] = token["SecurityToken"]
    headers["date"] = date or headers.get("x-oss-date") or headers.get("date") or formatdate(usegmt=True)

    string_to_sign = oss_v1_string_to_sign(method, url, headers)
    digest = hmac.new(token["AccessKeySecret"].encode("utf-8"),
                      string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    signature = base64.b64encode(digest).decode("ascii")
    headers["authorization"] = f"OSS {token['AccessKeyId']}:{signature}"
    return headers


def complete_body(parts: List[Tuple[int, str]]) -> bytes:
    """构造 complete 的 XML body（PartNumber + ETag 原样，含引号）。"""
    chunks = [b"<CompleteMultipartUpload>"]
    for number, etag in sorted(parts):
        chunks.append(f"<Part><PartNumber>{number}</PartNumber>"
                      f"<ETag>{etag}</ETag></Part>".encode("ascii"))
    chunks.append(b"</CompleteMultipartUpload>")
    return b"".join(chunks)


def callback_headers(callback: Optional[dict]) -> Dict[str, str]:
    """upload/init 返回的 callback -> 两个 base64 头（照抄 telegram-115bot）。"""
    cb = callback or {}
    body = cb.get("callback") or "{}"
    var = cb.get("callback_var") or "{}"
    return {
        "x-oss-callback": base64.b64encode(str(body).encode()).decode(),
        "x-oss-callback-var": base64.b64encode(str(var).encode()).decode(),
    }


_OSS_ERR_FIELDS = ("Code", "Message", "StringToSign", "RequestId",
                   "RequestTime", "ServerTime", "AccessKeyId")


def oss_error_summary(status: int, text: str) -> str:
    """解析 OSS 错误 XML，给出可定位原因的一行摘要（403 三大常见：签名错/时钟偏移/凭证无效）。"""
    info = {}
    for f in _OSS_ERR_FIELDS:
        m = re.search(rf"<{f}>([^<]*)</{f}>", text)
        if m:
            info[f] = m.group(1)
    code = info.get("Code", "?")
    hint = ""
    if code == "RequestTimeTooSkewed":
        hint = " —— 服务器时钟不准！请执行: timedatectl set-ntp true（或 ntpdate）后重试"
    elif code == "SignatureDoesNotMatch":
        hint = " —— V1 签名构造有误，把完整报错发我"
    elif code in ("InvalidAccessKeyId", "SecurityTokenExpired", "InvalidSecurityToken"):
        hint = " —— STS 凭证问题（token 过期/字段不符）"
    parts = [f"HTTP {status}", code]
    if "Message" in info:
        parts.append(info["Message"])
    if "ServerTime" in info and "RequestTime" in info:
        parts.append(f"server={info['ServerTime']} local={info['RequestTime']}")
    out = " | ".join(parts) + hint
    if info.get("StringToSign"):
        out += f"\n      └─ 服务器端 StringToSign:\n{info['StringToSign']}"
    return out


def is_sts_error(status: int, text: str) -> bool:
    """判断 OSS 响应是否为 STS 凭证问题(可触发 token 刷新 + 续传)。

    只识别 HTTP 403 + 三种 Code;SignatureDoesNotMatch/RequestTimeTooSkewed
    是签名/时钟问题,重试无效,必须排除。
    """
    if status != 403:
        return False
    m = re.search(r"<Code>([^<]*)</Code>", text)
    return bool(m and m.group(1) in _STS_ERROR_CODES)


def _parse_list_parts_xml(text: str) -> List[Tuple[int, str]]:
    """解析 OSS ListPartsResult XML。

    返回按 PartNumber 升序的 (part_number, etag) 列表;ETag 保留引号原样。
    IsTruncated=true 时记 WARNING 日志(单文件 < 10GB 不会触发;本期不翻页,
    fallback 到"放弃漏报 part + complete 失败 + 外层重试"策略)。
    """
    if _IS_TRUNCATED_RE.search(text):
        log.warning("OSS ListParts IsTruncated=true,分片数 >1000,本实现不翻页")
    parts = [(int(n), e) for n, e in _PART_RE.findall(text)]
    parts.sort(key=lambda p: p[0])
    return parts


# ── 上传执行 ───────────────────────────────────────────────────────────────
def _part_reader(path: Path, offset: int, length: int):
    """按 [offset, offset+length) 流式读文件的异步生成器。"""
    async def gen():
        f = await aiofiles_open(path, "rb")
        try:
            await f.seek(offset)
            remaining = length
            while remaining > 0:
                data = await f.read(min(_READ, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data
        finally:
            await f.close()
    return gen()


async def upload_to_oss(
    session: aiohttp.ClientSession,
    local_path: Path,
    size: int,
    *,
    endpoint: str,
    bucket: str,
    obj: str,
    token: Dict[str, str],
    callback: Optional[dict],
    concurrency: int = 8,
    on_progress: Optional[ProgressCb] = None,
    cancel_event: Optional[asyncio.Event] = None,
    token_refresher: Optional[Callable[[], Awaitable[Dict[str, str]]]] = None,
) -> None:
    """把文件直传 115 的 OSS。小文件单 PUT；大文件 multipart 串行。成功返回，失败抛异常。

    token_refresher: 可选异步闭包,返回新 STS dict(同 get_upload_token 格式)。
                    在 _put_parts 检测到 STS 错误时被调用以续传,缺省时保持旧行为(token 锁死)。
    """
    base = object_url(endpoint, bucket, obj)
    cb_headers = callback_headers(callback)

    if size <= MIN_PART_SIZE:
        await _put_single(session, base, local_path, size, token, cb_headers,
                          on_progress, cancel_event)
        return

    partsize = determine_partsize(size)
    total_parts = (size + partsize - 1) // partsize
    log.info("OSS multipart: %d parts x %dMB", total_parts, partsize // 1024 // 1024)

    upload_id = await _multipart_init(session, base, token)
    try:
        parts, final_upload_id, final_token = await _put_parts(
            session, base, local_path, size, partsize, total_parts,
            token, upload_id, concurrency, on_progress, cancel_event,
            token_refresher=token_refresher,
        )
        await _multipart_complete(session, base, final_upload_id, parts, final_token, cb_headers)
    except TaskCancelled:
        # 用户取消:best-effort 释放 OSS 残留(abort 失败吞掉,不阻塞 cancel 语义)
        await _multipart_abort(session, base, upload_id, token)
        raise
    # AuthRequiredError 不 abort — 让外层 with_backoff 重试整个 fast_upload,
    # 旧的 uploadId 7 天内会被 OSS 自动回收


async def _put_single(session, base, local_path, size, token, cb_headers,
                      on_progress, cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise TaskCancelled()
    headers = oss_v1_sign("PUT", base, token, {**cb_headers,
                                                "content-type": "application/octet-stream"})
    if on_progress:
        await on_progress(0, size)
    async with session.put(base, data=_part_reader(local_path, 0, size), headers=headers) as r:
        if r.status >= 400:
            body = await r.text()
            raise RuntimeError(f"OSS 单片 PUT 失败: {oss_error_summary(r.status, body)}")
    if on_progress:
        await on_progress(size, size)


async def _multipart_init(session, base, token) -> str:
    url = f"{base}?sequential=1&uploads=1"
    headers = oss_v1_sign("POST", url, token,
                          {"content-type": "application/octet-stream"})
    async with session.post(url, headers=headers) as r:
        text = await r.text()
        if r.status >= 400:
            raise RuntimeError(
                f"OSS init 失败: {oss_error_summary(r.status, text)}"
                f"\n      └─ 我方 StringToSign:\n{oss_v1_string_to_sign('POST', url, headers)}")
    m = _UPLOAD_ID_RE.search(text)
    if not m:
        raise RuntimeError(f"OSS init 未返回 UploadId: {text[:200]}")
    return m.group(1)


async def _put_parts(session, base, local_path, size, partsize, total_parts,
                     token, upload_id: str, concurrency, on_progress,
                     cancel_event, *, token_refresher=None):
    """串行按序上传分片。

    init 带 sequential=1(照抄 p115oss 协议),OSS 强制分片号严格按序提交,
    并发 PUT 会触发 PartNotSequential(p115oss 本身也是串行迭代器)。
    单文件内串行;速度由单流吞吐决定,TG 下载侧仍并行。concurrency 参数保留备用。

    续传逻辑(需 token_refresher):
      - 单 part 内循环:STS 错误 → refresh → list_parts → 重试当前 part(若必要)
      - refresh 失败按"单 part 内连续失败"计数,达到 MAX_REFRESH_FAILS → AuthRequiredError
      - refresh 失败后继续重试当前 part(期待再次 STS → 再次 refresh,直到成功或抛错)
      - list_parts 返 NoSuchUpload → 重新 init + 全传(放弃已成功 part)

    返回 (parts, final_upload_id, final_token) — id 可能是重新 init 后的,
    token 可能是刷新后的;complete 必须用这二者,否则续传后仍 403。
    """
    # mutable 容器便于闭包共享/重写
    state = {"token": token, "upload_id": upload_id, "refresh_fails": 0}
    parts: Dict[int, str] = {}
    done_bytes = 0
    start = 1
    while True:
        try:
            for number in range(start, total_parts + 1):
                if cancel_event is not None and cancel_event.is_set():
                    raise TaskCancelled()
                # list_parts 已记录的 part 直接跳过(只在续传分支被注入)
                if number in parts:
                    done_bytes += min(partsize, size - (number - 1) * partsize)
                    if on_progress:
                        await on_progress(done_bytes, size)
                    continue

                offset = (number - 1) * partsize
                length = min(partsize, size - offset)
                url = f"{base}?partNumber={number}&uploadId={quote(state['upload_id'])}"
                etag = await _put_one_part(session, base, local_path, offset, length,
                                           number, url, parts, state, cancel_event,
                                           token_refresher)
                parts[number] = etag
                done_bytes += length
                if on_progress:
                    await on_progress(done_bytes, size)
            return sorted(parts.items()), state["upload_id"], state["token"]
        except _SessionDead:
            # OSS session 已死(NoSuchUpload):重新 init + 全传
            log.warning("OSS session 已死,重新 init + 从头传")
            state["upload_id"] = await _multipart_init(session, base, state["token"])
            parts.clear()
            start = 1
            done_bytes = 0
            continue


async def _put_one_part(session, base, local_path, offset, length, number, url,
                        parts: Dict[int, str], state: dict,
                        cancel_event, token_refresher) -> str:
    """单 part 内循环:普通 PUT → STS 错误 → refresh + list_parts + 重试。

    state["token"] / state["upload_id"] / state["refresh_fails"] 由本函数读写。
    返回 ETag 或抛 AuthRequiredError / TaskCancelled。
    """
    while True:
        async def _attempt() -> str:
            # 每次尝试重建 reader(流式 body 不可重用)与签名(date 会变)
            # 通过闭包读 state["token"](Python 闭包按引用捕获 dict 元素)
            headers = oss_v1_sign("PUT", url, state["token"],
                                  {"content-type": "application/octet-stream"})
            async with session.put(url, data=_part_reader(local_path, offset, length),
                                   headers=headers) as r:
                if r.status >= 400:
                    body = await r.text()
                    if token_refresher is not None and is_sts_error(r.status, body):
                        raise _StsExpiredError(r.status, body)
                    raise RuntimeError(
                        f"OSS 分片#{number} PUT 失败: {oss_error_summary(r.status, body)}")
                return r.headers.get("ETag", "")

        try:
            return await with_backoff(_attempt, base=2.0, max_retries=3,
                                      no_retry=(TaskCancelled, _StsExpiredError))
        except _StsExpiredError:
            # ── 单 part 内的续传触发点 ──
            if token_refresher is None:
                raise RuntimeError(
                    "STS 过期但未提供 token_refresher(传 token_refresher 给 upload_to_oss 启用续传)"
                ) from None
            try:
                state["token"] = await token_refresher()
                state["refresh_fails"] = 0
                log.warning("STS 已刷新,触发续传 part#%d", number)
            except Exception as ref_e:  # noqa: BLE001
                state["refresh_fails"] += 1
                log.error("STS refresh 失败 (%d/%d): %r",
                          state["refresh_fails"], MAX_REFRESH_FAILS, ref_e)
                if state["refresh_fails"] >= MAX_REFRESH_FAILS:
                    raise AuthRequiredError(
                        f"连续 {MAX_REFRESH_FAILS} 次 STS refresh 失败"
                    ) from ref_e
                # refresh 失败:重试当前 part(期待再次 STS → 再次 refresh)
                continue

            existing = await _multipart_list_parts(session, base,
                                                   state["upload_id"],
                                                   state["token"])
            if not existing:
                # _multipart_list_parts 已经在内部抛 _SessionDead,正常走不到这里
                raise _SessionDead()   # 防御性
            for n, e in existing:
                parts[n] = e
            if number in parts:
                # 当前 part 在 list_parts 中已存在(网络抖动期间服务端已记录)
                return parts[number]
            # 重新拼 url 用新 upload_id,然后 continue 重试 _attempt
            url = f"{base}?partNumber={number}&uploadId={quote(state['upload_id'])}"
            continue


async def _multipart_complete(session, base, upload_id, parts, token, cb_headers) -> None:
    url = f"{base}?uploadId={quote(upload_id)}"
    headers = oss_v1_sign("POST", url, token, {**cb_headers, "content-type": "text/xml"})
    async with session.post(url, data=complete_body(parts), headers=headers) as r:
        text = await r.text()
        if r.status >= 400:
            raise RuntimeError(f"OSS complete 失败: {oss_error_summary(r.status, text)}")
    log.info("OSS complete 成功: %d parts", len(parts))


async def _multipart_list_parts(session, base, upload_id, token):
    """GET ?uploadId=ID。返回已成功的 (PartNumber, ETag) 列表。

    抛 _SessionDead:NoSuchUpload(session 已死,需 _put_parts 主循环重新 init + 全传)。
    抛 RuntimeError:其他 4xx(签名/网络等)。
    """
    url = f"{base}?uploadId={quote(upload_id)}"
    headers = oss_v1_sign("GET", url, token)
    async with session.get(url, headers=headers) as r:
        text = await r.text()
        if r.status == 404 or "NoSuchUpload" in text:
            log.warning("OSS session 已失效 (NoSuchUpload),需重新 init")
            raise _SessionDead()
        if r.status >= 400:
            raise RuntimeError(f"OSS ListParts 失败: {oss_error_summary(r.status, text)}")
    return _parse_list_parts_xml(text)


async def _multipart_abort(session, base, upload_id, token) -> None:
    """DELETE ?uploadId=ID。**仅用于用户取消路径**,续传路径不调。

    任意 status / 异常都吞掉只 log warning — cancel 语义绝不能被 abort 失败阻塞;
    OSS 端 multipart session 7 天自动 GC,abort 失败时残留影响有限。
    """
    url = f"{base}?uploadId={quote(upload_id)}"
    try:
        headers = oss_v1_sign("DELETE", url, token)
        async with session.delete(url, headers=headers) as r:
            if r.status >= 400:
                log.warning("OSS abort 失败 status=%d (忽略): %s",
                            r.status, (await r.text())[:200])
            else:
                log.info("OSS abort 完成: uploadId=%s", upload_id[:12])
    except Exception as e:  # noqa: BLE001
        log.warning("OSS abort 异常 (忽略): %r", e)
