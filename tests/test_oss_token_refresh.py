"""OSS multipart token 过期时无感续传测试。

覆盖点：
  1. 纯函数：is_sts_error / _parse_list_parts_xml
  2. 端到端 fake aiohttp session：
     - STS 错误触发 refresh + list_parts 续传(单 part 重传,前面 part 不重传)
     - NoSuchUpload 兜底:重新 init + 全传
     - 连续 refresh 失败 → AuthRequiredError
     - refresh 成功后计数清零
     - 用户取消 → abort_multipart
     - 不传 token_refresher → 原 RuntimeError 行为(向后兼容)
     - SignatureDoesNotMatch 不触发 refresh
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# aiohttp 桩注入(沙箱未装时)— 沿用 test_oss_protocol.py 的模式
try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    _aio = types.ModuleType("aiohttp")
    _aio.ClientSession = type("ClientSession", (), {"__init__": lambda s, *a, **k: None})
    _aio.ClientTimeout = lambda **k: None
    sys.modules["aiohttp"] = _aio

import cloud115.oss_upload as ou  # noqa: E402
from cloud115.oss_upload import (  # noqa: E402
    AuthRequiredError, is_sts_error, upload_to_oss,
)
from core.queue import TaskCancelled  # noqa: E402


# ── fake aiohttp session ────────────────────────────────────────────────────
class _Route:
    """单次匹配的路由(默认)/ 可重复匹配。"""

    def __init__(self, method, url_substr, *, status=200, body="", etag=None,
                 repeat=False, label=""):
        self.method = method
        self.url_substr = url_substr
        self.status = status
        self.body = body
        self.etag = etag
        self.repeat = repeat
        self.label = label


class _Resp:
    """aiohttp 响应的最小替身,自带 async context manager 接口。"""

    def __init__(self, status, body, headers):
        self.status = status
        self._body = body
        self.headers = headers

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self._body


class _FakeSession:
    """按 method + url_substr 分发响应的 fake session。"""

    def __init__(self, routes):
        self.routes = list(routes)
        self.calls: list[tuple[str, str]] = []
        self.header_log: list[tuple[str, str, dict]] = []   # (method, url, headers)
        self.closed = False

    def _match(self, method, url):
        for i, r in enumerate(self.routes):
            if r.method == method and r.url_substr in url:
                if not r.repeat:
                    self.routes.pop(i)
                return r
        remaining = [(r.method, r.url_substr, r.label) for r in self.routes]
        raise AssertionError(
            f"无匹配 fake route: {method} {url[:120]}; 剩余={remaining}"
        )

    def _resp(self, r):
        headers = {"ETag": r.etag} if r.etag else {}
        return _Resp(r.status, r.body, headers)

    def post(self, url, **kw):
        self.calls.append(("POST", url))
        self.header_log.append(("POST", url, kw.get("headers") or {}))
        return self._resp(self._match("POST", url))

    def put(self, url, **kw):
        self.calls.append(("PUT", url))
        self.header_log.append(("PUT", url, kw.get("headers") or {}))
        return self._resp(self._match("PUT", url))

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        self.header_log.append(("GET", url, kw.get("headers") or {}))
        return self._resp(self._match("GET", url))

    def delete(self, url, **kw):
        self.calls.append(("DELETE", url))
        self.header_log.append(("DELETE", url, kw.get("headers") or {}))
        return self._resp(self._match("DELETE", url))


# ── 公共 fixture ────────────────────────────────────────────────────────────
STS_BODY = ('<Error><Code>InvalidAccessKeyId</Code>'
            '<Message>The OSS Access Key Id you provided does not exist.</Message>'
            '</Error>')
STS_BODY_ALT = ('<Error><Code>SecurityTokenExpired</Code>'
                '<Message>STS expired.</Message></Error>')
SIG_BODY = ('<Error><Code>SignatureDoesNotMatch</Code>'
            '<Message>sig mismatch</Message></Error>')
NOSUCHUPLOAD = '<Error><Code>NoSuchUpload</Code><Message>gone</Message></Error>'


def _list_parts_xml(parts: list[tuple[int, str]], truncated: bool = False) -> str:
    items = "".join(
        f"<Part><PartNumber>{n}</PartNumber><ETag>{e}</ETag><Size>1024</Size></Part>"
        for n, e in parts
    )
    trunc = "<IsTruncated>true</IsTruncated>" if truncated else "<IsTruncated>false</IsTruncated>"
    return f"<ListPartsResult>{items}{trunc}</ListPartsResult>"


def _patch_sleep():
    """用 0 延迟替换 oss_upload 模块的 asyncio.sleep(加速退避)。"""
    orig = ou.asyncio.sleep
    async def _zero(_s):
        await orig(0)
    ou.asyncio.sleep = _zero
    # 注意:ou.asyncio 是全局 asyncio 模块本体,恢复须写回其 sleep 属性
    # (setattr(ou, "asyncio.sleep", ...) 只是建了个同名垃圾属性,不会恢复)
    return lambda: setattr(ou.asyncio, "sleep", orig)


def _tmpfile(size: int) -> Path:
    """创建一个填充 0 字节的临时文件,size 必须 > MIN_PART_SIZE(10MB)以走 multipart。"""
    p = Path("/tmp/_oss_token_refresh_test.bin")
    if p.exists() and p.stat().st_size >= size:
        return p
    p.write_bytes(b"\0" * size)
    return p


# ── 纯函数测试 ──────────────────────────────────────────────────────────────
def test_is_sts_error_positive():
    """三种 STS Code + HTTP 403 → True。"""
    assert is_sts_error(403, STS_BODY) is True
    assert is_sts_error(403, STS_BODY_ALT) is True
    assert is_sts_error(403, '<Error><Code>InvalidSecurityToken</Code></Error>') is True


def test_is_sts_error_negative():
    """SignatureDoesNotMatch / RequestTimeTooSkewed / 非 403 → False。"""
    assert is_sts_error(403, SIG_BODY) is False
    assert is_sts_error(403, '<Error><Code>RequestTimeTooSkewed</Code></Error>') is False
    assert is_sts_error(200, '<Error><Code>InvalidAccessKeyId</Code></Error>') is False
    assert is_sts_error(500, '<Code>InternalError</Code>') is False
    assert is_sts_error(404, "") is False
    assert is_sts_error(403, "not even XML") is False


def test_parse_list_parts_xml_basic():
    """乱序 part 自动按 PartNumber 升序排序;ETag 保留引号。"""
    xml = _list_parts_xml([(3, '"e3"'), (1, '"e1"'), (2, '"e2"')])
    parts = ou._parse_list_parts_xml(xml)
    assert parts == [(1, '"e1"'), (2, '"e2"'), (3, '"e3"')]


def test_parse_list_parts_xml_empty_and_truncated():
    """空响应 / IsTruncated=true 时正确处理。"""
    assert ou._parse_list_parts_xml("") == []
    assert ou._parse_list_parts_xml("<ListPartsResult></ListPartsResult>") == []
    # IsTruncated=true 不应抛(仅 log warning),但仍返回能解析的 part
    xml = _list_parts_xml([(1, '"e1"')], truncated=True)
    assert ou._parse_list_parts_xml(xml) == [(1, '"e1"')]


# ── 端到端 fake session 测试 ───────────────────────────────────────────────
def _common_token():
    return {"AccessKeyId": "AK", "AccessKeySecret": "SK",
            "SecurityToken": "STS0", "endpoint": ""}


def _new_token(suffix="1"):
    return {"AccessKeyId": "AK", "AccessKeySecret": "SK",
            "SecurityToken": f"STS{suffix}", "endpoint": ""}


def _make_test_file(size=30 * 1024 * 1024):  # 30MB → 3 parts × 10MB
    return _tmpfile(size)


async def _run_upload(session, tmpfile, refresher=None):
    """跑一次 upload_to_oss;refresher=None 时退化为旧行为。"""
    await upload_to_oss(
        session, tmpfile, tmpfile.stat().st_size,
        endpoint="oss-cn-shenzhen.aliyuncs.com",
        bucket="fhnfile", obj="t.mp4", token=_common_token(),
        callback=None,
        token_refresher=refresher,
    )


def test_upload_sts_refresh_resume_partial():
    """part2 第 1 次 403 STS → refresh → list_parts 返 part1 → part2 重试成功。
    断言:part1 只 PUT 1 次(没被重传),part2 PUT 共 2 次(1 失败 + 1 成功)。"""
    restore = _patch_sleep()
    try:
        tmpfile = _make_test_file(30 * 1024 * 1024)
        routes = [
            _Route("POST", "uploads=1", body="<UploadId>UP1</UploadId>", label="init"),
            _Route("PUT", "partNumber=1", etag='"e1"', label="part1"),
            _Route("PUT", "partNumber=2", status=403, body=STS_BODY, label="part2-sts"),
            _Route("GET", "uploadId=UP1", body=_list_parts_xml([(1, '"e1"')]), label="list"),
            _Route("PUT", "partNumber=2", etag='"e2"', label="part2-retry"),
            _Route("PUT", "partNumber=3", etag='"e3"', label="part3"),
            _Route("POST", "uploadId=UP1", body="", label="complete"),
        ]
        session = _FakeSession(routes)

        refresh_calls = {"n": 0}

        async def refresher():
            refresh_calls["n"] += 1
            return _new_token("1")

        asyncio.run(_run_upload(session, tmpfile, refresher=refresher))

        # 路由全部消费完
        assert not session.routes, f"残留路由: {session.routes}"
        # refresh 调用 1 次
        assert refresh_calls["n"] == 1
        # 调用序列:init, put1, put2(fail), get-list-parts, put2(ok), put3, complete
        methods = [m for m, _ in session.calls]
        assert methods.count("POST") == 2   # init + complete
        assert methods.count("GET") == 1    # list_parts
        assert methods.count("PUT") == 4    # part1 + part2(fail) + part2(ok) + part3
        # complete 必须用刷新后的 STS1 签名(用旧 STS0 会再吃一次 403)
        complete_hdrs = [h for m, u, h in session.header_log
                         if m == "POST" and "uploadId=UP1" in u]
        assert len(complete_hdrs) == 1, f"complete 请求数异常: {complete_hdrs}"
        assert complete_hdrs[0]["x-oss-security-token"] == "STS1", \
            f"complete 未用刷新后 token: {complete_hdrs[0].get('x-oss-security-token')}"
    finally:
        restore()


def test_upload_sts_refresh_no_such_upload_reinit():
    """list_parts 返 NoSuchUpload → 重新 init + 全传。"""
    restore = _patch_sleep()
    try:
        tmpfile = _make_test_file(30 * 1024 * 1024)
        routes = [
            _Route("POST", "uploads=1", body="<UploadId>UP1</UploadId>", label="init1"),
            _Route("PUT", "partNumber=1", etag='"e1"', label="part1"),
            _Route("PUT", "partNumber=2", status=403, body=STS_BODY, label="part2-sts"),
            _Route("GET", "uploadId=UP1", status=404, body=NOSUCHUPLOAD, label="list-empty"),
            _Route("POST", "uploads=1", body="<UploadId>UP2</UploadId>", label="init2"),
            _Route("PUT", "partNumber=1", etag='"e1b"', label="p1-retry"),
            _Route("PUT", "partNumber=2", etag='"e2b"', label="p2-ok"),
            _Route("PUT", "partNumber=3", etag='"e3b"', label="p3"),
            # complete 用新 session UP2 的 uploadId
            _Route("POST", "uploadId=UP2", body="", label="complete"),
        ]
        session = _FakeSession(routes)

        refresh_calls = {"n": 0}

        async def refresher():
            refresh_calls["n"] += 1
            return _new_token("1")

        asyncio.run(_run_upload(session, tmpfile, refresher=refresher))

        assert not session.routes, f"残留路由: {session.routes}"
        assert refresh_calls["n"] == 1
        # init 被调 2 次,complete 1 次,list_parts 1 次
        methods = [m for m, _ in session.calls]
        assert methods.count("POST") == 3   # init1 + init2 + complete
        assert methods.count("GET") == 1
    finally:
        restore()


def test_upload_refresh_fails_consecutive_raises():
    """连续 MAX_REFRESH_FAILS(3)次 refresh 抛异常 → AuthRequiredError。"""
    restore = _patch_sleep()
    try:
        tmpfile = _make_test_file(30 * 1024 * 1024)
        # 4 次 STS 错误(每轮 part2 失败 → refresh 失败 → continue → 下一轮继续)
        routes = [
            _Route("POST", "uploads=1", body="<UploadId>UP1</UploadId>", label="init"),
            _Route("PUT", "partNumber=1", etag='"e1"', label="part1"),
            _Route("PUT", "partNumber=2", status=403, body=STS_BODY, label="p2-fail1"),
            _Route("PUT", "partNumber=2", status=403, body=STS_BODY, label="p2-fail2"),
            _Route("PUT", "partNumber=2", status=403, body=STS_BODY, label="p2-fail3"),
            _Route("PUT", "partNumber=2", status=403, body=STS_BODY, label="p2-fail4"),
        ]
        session = _FakeSession(routes)

        async def bad_refresher():
            raise RuntimeError("network down")

        try:
            asyncio.run(_run_upload(session, tmpfile, refresher=bad_refresher))
        except AuthRequiredError as e:
            assert "连续 3 次" in str(e), f"错误信息不符: {e}"
        else:
            raise AssertionError("期望 AuthRequiredError,但未抛出")

        # 4 次 PUT(part2 第 1/2/3/4 轮都失败,都触发 refresh),3 次 refresh 全失败 → 第 4 次抛 AuthRequiredError
        # 但 refresh_fails 在第 3 次后即达到上限,所以 part2 第 4 次 PUT 不会执行
        methods = [m for m, _ in session.calls]
        assert methods.count("PUT") == 4   # part1 + part2 三次重试
    finally:
        restore()


def test_upload_refresh_succeeds_after_one_fail():
    """第 1 次 refresh 失败 → 第 2 次 refresh 成功 → 上传完成。

    list_parts 在 refresh 成功后调一次,返 [(1, '"e1"')] — part1 已成功;
    然后重试当前 part2(用新 token)成功。
    """
    restore = _patch_sleep()
    try:
        tmpfile = _make_test_file(30 * 1024 * 1024)
        routes = [
            _Route("POST", "uploads=1", body="<UploadId>UP1</UploadId>", label="init"),
            _Route("PUT", "partNumber=1", etag='"e1"', label="part1"),
            _Route("PUT", "partNumber=2", status=403, body=STS_BODY, label="p2-fail1"),
            _Route("PUT", "partNumber=2", status=403, body=STS_BODY, label="p2-fail2"),
            _Route("GET", "uploadId=UP1", body=_list_parts_xml([(1, '"e1"')]), label="list"),
            _Route("PUT", "partNumber=2", etag='"e2"', label="p2-ok"),
            _Route("PUT", "partNumber=3", etag='"e3"', label="part3"),
            _Route("POST", "uploadId=UP1", body="", label="complete"),
        ]
        session = _FakeSession(routes)

        refresh_calls = {"n": 0}

        async def flaky_refresher():
            refresh_calls["n"] += 1
            if refresh_calls["n"] == 1:
                raise RuntimeError("transient")
            return _new_token(str(refresh_calls["n"]))

        asyncio.run(_run_upload(session, tmpfile, refresher=flaky_refresher))

        assert not session.routes, f"残留路由: {session.routes}"
        assert refresh_calls["n"] == 2
        methods = [m for m, _ in session.calls]
        assert methods.count("POST") == 2
        assert methods.count("GET") == 1
        assert methods.count("PUT") == 5   # part1 + p2 fail1 + p2 fail2 + p2 ok + part3
    finally:
        restore()


def test_upload_cancel_aborts_oss_session():
    """用户在 part2 前取消 → abort_multipart(DELETE)被命中。"""
    tmpfile = _make_test_file(30 * 1024 * 1024)
    routes = [
        _Route("POST", "uploads=1", body="<UploadId>UP1</UploadId>", label="init"),
        _Route("PUT", "partNumber=1", etag='"e1"', label="part1"),
        # part2 前 cancel → DELETE 应被调
        _Route("DELETE", "uploadId=UP1", body="", label="abort"),
    ]
    session = _FakeSession(routes)
    cancel = asyncio.Event()
    cancel_fired = {"v": False}

    orig_put = session.put

    def hooked_put(url, **kw):
        r = orig_put(url, **kw)
        if not cancel_fired["v"]:
            cancel_fired["v"] = True
            cancel.set()
        return r

    session.put = hooked_put

    async def run():
        await upload_to_oss(
            session, tmpfile, tmpfile.stat().st_size,
            endpoint="oss-cn-shenzhen.aliyuncs.com",
            bucket="fhnfile", obj="t.mp4", token=_common_token(),
            callback=None, cancel_event=cancel,
        )

    try:
        asyncio.run(run())
    except TaskCancelled:
        pass
    else:
        raise AssertionError("期望 TaskCancelled,但未抛出")

    # DELETE 必须被调
    methods = [m for m, _ in session.calls]
    assert methods.count("DELETE") == 1, f"DELETE 未调用: {session.calls}"


def test_upload_complete_normal_no_abort():
    """正常完成 → DELETE 从未被调用。"""
    tmpfile = _make_test_file(30 * 1024 * 1024)
    routes = [
        _Route("POST", "uploads=1", body="<UploadId>UP1</UploadId>", label="init"),
        _Route("PUT", "partNumber=1", etag='"e1"', label="p1"),
        _Route("PUT", "partNumber=2", etag='"e2"', label="p2"),
        _Route("PUT", "partNumber=3", etag='"e3"', label="p3"),
        _Route("POST", "uploadId=UP1", body="", label="complete"),
    ]
    session = _FakeSession(routes)
    asyncio.run(_run_upload(session, tmpfile))
    methods = [m for m, _ in session.calls]
    assert methods.count("DELETE") == 0
    assert methods.count("POST") == 2


def test_upload_no_refresher_sts_error_propagates():
    """不传 token_refresher → STS 错误用同一过期 token 重试 3 次 → RuntimeError(原行为)。"""
    tmpfile = _make_test_file(30 * 1024 * 1024)
    routes = [
        _Route("POST", "uploads=1", body="<UploadId>UP1</UploadId>", label="init"),
        _Route("PUT", "partNumber=1", status=403, body=STS_BODY, repeat=True, label="p1-fail"),
    ]
    session = _FakeSession(routes)
    try:
        asyncio.run(_run_upload(session, tmpfile, refresher=None))
    except RuntimeError as e:
        assert "STS" in str(e) or "分片" in str(e)
    else:
        raise AssertionError("期望 RuntimeError")
    # part1 重试 3 + 1 = 4 次都失败
    puts = sum(1 for m, _ in session.calls if m == "PUT")
    assert puts == 4, f"PUT 次数: {puts}"


def test_upload_signature_error_does_not_trigger_refresh():
    """SignatureDoesNotMatch(403 但非 STS)→ 不调 refresher → 原 RuntimeError。"""
    tmpfile = _make_test_file(30 * 1024 * 1024)
    routes = [
        _Route("POST", "uploads=1", body="<UploadId>UP1</UploadId>", label="init"),
        _Route("PUT", "partNumber=1", status=403, body=SIG_BODY, repeat=True, label="p1-sig"),
    ]
    session = _FakeSession(routes)

    refresh_calls = {"n": 0}

    async def refresher():
        refresh_calls["n"] += 1
        return _new_token("1")

    try:
        asyncio.run(_run_upload(session, tmpfile, refresher=refresher))
    except RuntimeError:
        pass
    else:
        raise AssertionError("期望 RuntimeError")

    assert refresh_calls["n"] == 0, f"不应调用 refresh,但调用了 {refresh_calls['n']} 次"
    puts = sum(1 for m, _ in session.calls if m == "PUT")
    assert puts == 4


# ── main ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok: {fn.__name__}")
    print("test_oss_token_refresh: ALL PASS")
