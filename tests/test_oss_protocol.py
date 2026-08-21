"""115 手写实现纯逻辑测试：PKCE / OSS V1 签名 / 分片 / complete XML / callback。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# cloud115.oss_upload 顶层 import aiohttp（沙箱未装），先注入桩（若真实 aiohttp 可用则跳过）
try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    import types as _types
    _aio = _types.ModuleType("aiohttp")
    _aio.ClientSession = type("ClientSession", (), {"__init__": lambda self, *a, **k: None})
    _aio.ClientTimeout = lambda **k: None
    sys.modules["aiohttp"] = _aio

from cloud115.oss_upload import (  # noqa: E402
    MIN_PART_SIZE, callback_headers, complete_body, determine_partsize,
    object_url, oss_v1_sign,
)
from cloud115.openapi import make_pkce_pair  # noqa: E402

TOKEN = {"AccessKeyId": "AKIDtest", "AccessKeySecret": "SECRETtest",
         "SecurityToken": "STS_TOKEN"}


def test_pkce_pair_s256():
    verifier, challenge = make_pkce_pair()
    assert 40 <= len(verifier) <= 64
    assert all(c.isalnum() or c in "-._~" for c in verifier)
    expect = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expect


def test_determine_partsize():
    assert determine_partsize(0) == MIN_PART_SIZE
    assert determine_partsize(MIN_PART_SIZE) == MIN_PART_SIZE
    assert determine_partsize(MIN_PART_SIZE + 1) == MIN_PART_SIZE
    # 100MB -> 需 >= 100MB/10000=10.24KB? 不对：片数=ceil(size/10MB)=10 片 <=10000，故仍 10MB
    assert determine_partsize(100 * 1024 * 1024) == MIN_PART_SIZE
    # 超大文件强制倍增到片数<=10000：10TB -> 10MB*2^k >= 1GB(=10TB/10000)
    huge = 10 * 1024 ** 4
    ps = determine_partsize(huge)
    assert ps >= huge / 10000
    assert ps // 2 < huge / 10000          # 最小的满足者（翻倍刚够）
    assert ps % (10 * 1024 * 1024) == 0    # 保持 10MB 整数倍


def test_object_url():
    u = object_url("http://oss-cn-shenzhen.aliyuncs.com", "fhnfile", "a/b c.mp4")
    assert u == "http://fhnfile.oss-cn-shenzhen.aliyuncs.com/a/b%20c.mp4"
    u2 = object_url("oss-cn-shenzhen.aliyuncs.com", "bk", "x")   # 无 scheme 自动补 http
    assert u2.startswith("http://bk.")
    u3 = object_url("", "bk", "x")                              # 空 endpoint 用默认
    assert "oss-cn-shenzhen" in u3


def test_oss_v1_sign_structure():
    """签名结构：手工复算 StringToSign 逐段对照（注入固定 date）。"""
    date = "Mon, 01 Jan 2026 00:00:00 GMT"
    url = "http://fhnfile.oss-cn-shenzhen.aliyuncs.com/obj?partNumber=2&uploadId=ABC"
    extra = {"x-oss-callback": "Y2I=", "x-oss-callback-var": "dmFy"}
    h = oss_v1_sign("PUT", url, TOKEN, extra, date=date)
    assert h["x-oss-security-token"] == "STS_TOKEN"
    assert h["date"] == date
    assert h["authorization"].startswith("OSS AKIDtest:")
    # 手工复算（注意 x-oss-security-token 也按字典序参与签名，排最后）
    sts = "\n".join([
        "PUT", "", "", date,
        "x-oss-callback:Y2I=\nx-oss-callback-var:dmFy\nx-oss-security-token:STS_TOKEN",
        "/fhnfile/obj?partNumber=2&uploadId=ABC",
    ])
    sig = base64.b64encode(hmac.new(b"SECRETtest", sts.encode(), hashlib.sha1).digest()).decode()
    assert h["authorization"] == f"OSS AKIDtest:{sig}"


def test_oss_v1_sign_xoss_sorted():
    """x-oss-* 头须按字典序参与签名（换序不影响签名）。"""
    date = "Mon, 01 Jan 2026 00:00:00 GMT"
    url = "http://bk.h/o"
    h1 = oss_v1_sign("PUT", url, TOKEN, {"x-oss-b": "1", "x-oss-a": "2"}, date=date)
    h2 = oss_v1_sign("PUT", url, TOKEN, {"x-oss-a": "2", "x-oss-b": "1"}, date=date)
    assert h1["authorization"] == h2["authorization"]


def test_complete_body_xml():
    body = complete_body([(2, '"etag2"'), (1, '"etag1"')])
    assert body.startswith(b"<CompleteMultipartUpload>")
    assert body.endswith(b"</CompleteMultipartUpload>")
    # 按分片号排序
    assert body.index(b"<PartNumber>1") < body.index(b"<PartNumber>2")
    assert b'<ETag>"etag1"</ETag>' in body


def test_callback_headers_base64():
    cb = {"callback": '{"callbackUrl":"x"}', "callback_var": '{"k":"v"}'}
    h = callback_headers(cb)
    assert base64.b64decode(h["x-oss-callback"]) == b'{"callbackUrl":"x"}'
    assert base64.b64decode(h["x-oss-callback-var"]) == b'{"k":"v"}'
    # 空回调 -> "{}"
    h2 = callback_headers(None)
    assert base64.b64decode(h2["x-oss-callback"]) == b"{}"


def test_poll_qr_status_semantics():
    """data.status 直通（0/1/2/-1/-2）；顶层 state 与失效误判回归；异常返回 None。"""
    import asyncio
    import types as _types
    from pathlib import Path as _Path

    aio = _types.ModuleType("aiohttp")

    class _Resp:
        def __init__(self, payload):
            self.payload = payload
        async def json(self, content_type=None):
            return self.payload

    class _Ctx:
        def __init__(self, resp):
            self.resp = resp
        async def __aenter__(self):
            return self.resp
        async def __aexit__(self, *a):
            return False

    class _Session:
        closed = False
        def __init__(self, payload):
            self.payload = payload
        def get(self, url, **kw):
            return _Ctx(_Resp(self.payload))

    aio.ClientSession = _Session
    aio.ClientTimeout = lambda **k: None
    saved = sys.modules.get("aiohttp")
    sys.modules["aiohttp"] = aio
    try:
        from cloud115.openapi import Open115Client
        async def run(payload):
            c = Open115Client(_Path("/tmp/_t_qr.json"))
            c._session = _Session(payload)
            return await c.poll_qr_status("u", 1, "s")
        assert asyncio.run(run({"state": True, "data": {"status": 0}})) == 0
        assert asyncio.run(run({"state": True, "data": {"status": 1}})) == 1
        assert asyncio.run(run({"state": True, "data": {"status": 2}})) == 2
        assert asyncio.run(run({"state": True, "data": {"status": -1}})) == -1
        assert asyncio.run(run({"state": True, "data": {"status": -2}})) == -2
        # 回归：顶层 state=0/false 不再被误判为二维码过期
        assert asyncio.run(run({"state": 0, "data": {"status": 0}})) == 0
        # 响应异常 -> None（调用方继续轮询）
        assert asyncio.run(run({"unexpected": 1})) is None
        assert asyncio.run(run({"data": "notadict"})) is None
    finally:
        if saved is not None:
            sys.modules["aiohttp"] = saved
        else:
            sys.modules.pop("aiohttp", None)




def test_classify_link():
    """离线链接识别：magnet/ed2k/直链/误报防护。"""
    import types as _types
    if "aiohttp" not in sys.modules:
        _aio = _types.ModuleType("aiohttp")
        _aio.ClientSession = type("ClientSession", (), {"__init__": lambda s, *a, **k: None})
        _aio.ClientTimeout = lambda **k: None
        sys.modules["aiohttp"] = _aio
    from core.offline import classify_link

    assert classify_link("magnet:?xt=urn:btih:ABCDEF1234567890&dn=t") == "magnet"
    assert classify_link("ed2k://|file|name.mkv|12345|hash|/") == "ed2k"
    assert classify_link("https://example.com/movie.torrent") == "url"
    assert classify_link("http://a.com/video.mp4") == "url"
    assert classify_link("https://pan.baidu.com/s/xyz") == "url"
    assert classify_link("普通聊天文本") is None
    assert classify_link("看看这个 https://a.com/x.mp4") is None
    assert classify_link("magnet:?xt=urn:btih:短") is None
    assert classify_link("") is None
    assert classify_link("a" * 3000) is None


def test_rss_extract_and_match():
    """RSS 条目链接提取与关键词匹配。"""
    import types as _types
    if "aiohttp" not in sys.modules:
        _aio = _types.ModuleType("aiohttp")
        _aio.ClientSession = type("ClientSession", (), {"__init__": lambda s, *a, **k: None})
        _aio.ClientTimeout = lambda **k: None
        sys.modules["aiohttp"] = _aio
    from core.rss import entry_matches, extract_links

    m = "magnet:?xt=urn:btih:ABCDEF123456789012345"
    assert extract_links("t", m) == [m]
    r = extract_links("新片 magnet:?xt=urn:btih:AAAABBBBCCCCDDDD1111&dn=x", "https://b.com/p1")
    assert r == ["magnet:?xt=urn:btih:AAAABBBBCCCCDDDD1111&dn=x"]   # 网页链接不混入
    assert extract_links("种子", "https://t.org/x.torrent") == ["https://t.org/x.torrent"]
    assert extract_links("标题", "https://b.com/p1", "ed2k://|file|x.mkv|123|h|/") == \
        ["ed2k://|file|x.mkv|123|h|/"]
    assert extract_links("标题", "https://b.com/p1") == []   # 纯网页条目无链接
    # 去重：同一条 magnet 同时出现在标题与 link，只返回一条
    same = "magnet:?xt=urn:btih:AAAABBBBCCCCDDDD1111"
    assert extract_links(f"m {same}", same) == [same]

    assert entry_matches("anything", []) is True
    assert entry_matches("4K HEVC 电影", ["4k"]) is True
    assert entry_matches("720p", ["4K", "HEVC"]) is False


def test_movie_pick_best():
    """电影资源评分挑选：分辨率优先 + 中字加分。"""
    import types as _types
    if "aiohttp" not in sys.modules:
        _aio = _types.ModuleType("aiohttp")
        _aio.ClientSession = type("ClientSession", (), {"__init__": lambda s, *a, **k: None})
        _aio.ClientTimeout = lambda **k: None
        sys.modules["aiohttp"] = _aio
    from core.movie_sub import pick_best, score_resource

    items = [
        {"name": "M.720p.WEB", "ed2k": "ed2k://a", "resolution": "720p", "zh_sub": 0},
        {"name": "M.2160p.WEB中字", "ed2k": "ed2k://b", "resolution": "2160p", "zh_sub": 1},
        {"name": "M.1080p", "ed2k": "ed2k://c", "resolution": "1080p", "zh_sub": 0},
    ]
    best = pick_best(items, "ed2k")
    assert best.url == "ed2k://b" and best.zh_sub and best.score == 13
    assert pick_best(items, "ed2k", require_zh_sub=True).url == "ed2k://b"
    assert pick_best([], "ed2k") is None
    assert pick_best([{"name": "x"}], "ed2k") is None
    assert score_resource({"name": "F.2160p.mkv", "magnet": "m"}, "magnet").score >= 3


def test_parse_share_link():
    """115 分享链接解析与 Cookie UID 提取。"""
    import types as _types
    if "aiohttp" not in sys.modules:
        _aio = _types.ModuleType("aiohttp")
        _aio.ClientSession = type("ClientSession", (), {"__init__": lambda s, *a, **k: None})
        _aio.ClientTimeout = lambda **k: None
        sys.modules["aiohttp"] = _aio
    from cloud115.share import _uid_from_cookies, parse_share_link

    assert parse_share_link("https://115.com/s/abc123?password=x9y8") == ("abc123", "x9y8")
    assert parse_share_link("https://115cdn.com/s/XYZ789?password=1234") == ("XYZ789", "1234")
    assert parse_share_link("https://115.com/s/abc123") == ("abc123", "")
    assert parse_share_link("看 https://115.com/s/abc123?password=pw 哈") == ("abc123", "pw")
    assert parse_share_link("https://baidu.com/s/xxx") is None
    assert parse_share_link("") is None
    assert _uid_from_cookies("UID=123456789; CID=1") == "123456789"
    assert _uid_from_cookies("") == ""


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print(f"  ok: {fn.__name__}")
    print("test_oss_protocol: ALL PASS")
