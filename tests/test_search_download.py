"""搜索 → pick_code 直下 链路测试（手写 fake，零网络）。

覆盖：
  1. Open115Client.search_entry_abbr 归一（全名字段 -> 缩写字段，additive 保留原字段）
  2. download_by_pick_code：正常落地（.part 改名）/ sha1 不符抛错留现场 / 文件名净化
  3. AI 工具链：search_115 输出含序号+pc+sha1（供 download_115 衔接），download_115 成功回执
"""
from __future__ import annotations

import asyncio
import hashlib
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# aiohttp 桩（沙箱未装时）——仿 test_oss_protocol
try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    _aio = types.ModuleType("aiohttp")
    _aio.ClientSession = type("ClientSession", (), {"__init__": lambda s, *a, **k: None})
    _aio.ClientTimeout = lambda **k: None
    _aio.ClientError = Exception
    sys.modules["aiohttp"] = _aio

import cloud115.download as dl_mod  # noqa: E402
from cloud115.download import download_by_pick_code  # noqa: E402
from cloud115.openapi import Open115Client  # noqa: E402

# ── 归一化 ──────────────────────────────────────────────────────────────────
SEARCH_ENTRY = {
    "file_id": "3510654358649833458", "user_id": "103236532",
    "sha1": "EF230D914206EE663D2E9B384543827BA6946105",
    "file_name": "movie.mp4", "file_size": "504487964",
    "pick_code": "csgpl4johgbi6dds2", "parent_id": "3499586176640091949",
    "file_category": "1", "ico": "mp4",
}


def test_search_entry_abbr_from_fullname():
    """search 全名字段 -> 缩写字段（fs 字符串转 int，sha1 转小写）。"""
    out = Open115Client.search_entry_abbr(SEARCH_ENTRY)
    assert out["fn"] == "movie.mp4"
    assert out["fs"] == 504487964            # 字符串 "504487964" -> int
    assert out["pc"] == "csgpl4johgbi6dds2"
    assert out["fid"] == "3510654358649833458"
    assert out["fc"] == "1"
    assert out["pid"] == "3499586176640091949"
    assert out["sha1"] == "ef230d914206ee663d2e9b384543827ba6946105"  # 小写
    # 原字段保留（additive）
    assert out["file_name"] == "movie.mp4" and out["pick_code"] == "csgpl4johgbi6dds2"


def test_search_entry_abbr_passthrough_and_garbage():
    """ls 缩写形态原样通过；脏数据（fs 非数字/缺字段）不抛错。"""
    ls_form = {"fn": "a.mkv", "fs": 123, "pc": "PC1", "fid": "9", "fc": "0", "sha1": "AB"}
    out = Open115Client.search_entry_abbr(ls_form)
    assert out["fn"] == "a.mkv" and out["fs"] == 123 and out["fc"] == "0"
    assert out["sha1"] == "ab"
    bad = Open115Client.search_entry_abbr({"file_size": "not-a-number", "file_name": "x"})
    assert bad["fs"] == 0 and bad["fn"] == "x" and bad["pc"] == ""


# ── download_by_pick_code ───────────────────────────────────────────────────
class _FakeRaw:
    def __init__(self, info: dict):
        self.info = info
        self.calls: list[str] = []

    async def get_download_url(self, pc: str) -> dict:
        self.calls.append(pc)
        return self.info


class _FakeCloud:
    def __init__(self, info: dict):
        self.raw = _FakeRaw(info)


def _fake_downloader(content: bytes):
    """替换 download_file：写 <dest>.part 并返回 (len, sha1)——与真实行为一致。"""
    async def fake(url, dest, *, expected_size=0, max_attempts=2, on_progress=None):
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(content)
        if on_progress:
            on_progress(len(content), len(content))
        return len(content), hashlib.sha1(content).hexdigest()
    return fake


def _info(name="a.mp4", size=4, sha1="", pc="PC1"):
    return {"file_name": name, "file_size": size, "pick_code": pc,
            "sha1": sha1, "url": "http://fake/{pc}"}


def test_download_by_pick_code_ok():
    tmp = Path(tempfile.mkdtemp())
    orig = dl_mod.download_file
    content = b"data"
    dl_mod.download_file = _fake_downloader(content)
    try:
        sha1 = hashlib.sha1(content).hexdigest()
        cloud = _FakeCloud(_info(sha1=sha1))
        r = asyncio.run(download_by_pick_code(cloud, "PC1", tmp))
        assert cloud.raw.calls == ["PC1"]
        assert r["dest"] == tmp / "a.mp4" and r["dest"].exists()
        assert not (tmp / "a.mp4.part").exists()   # 已改名落地
        assert r["size"] == len(content) and r["sha1"] == sha1
    finally:
        dl_mod.download_file = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_download_by_pick_code_sha1_mismatch_keeps_part():
    tmp = Path(tempfile.mkdtemp())
    orig = dl_mod.download_file
    dl_mod.download_file = _fake_downloader(b"data")
    try:
        cloud = _FakeCloud(_info(sha1="0" * 40))   # 与内容 sha1 不符
        try:
            asyncio.run(download_by_pick_code(cloud, "PC1", tmp))
        except RuntimeError as e:
            assert "SHA1 不符" in str(e)
        else:
            raise AssertionError("期望 sha1 不符抛 RuntimeError")
        assert (tmp / "a.mp4.part").exists()       # 现场保留
        assert not (tmp / "a.mp4").exists()        # 未落地
    finally:
        dl_mod.download_file = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_download_by_pick_code_sanitize_and_mkdir():
    """远端名含非法字符被净化；目标目录不存在自动创建。"""
    tmp = Path(tempfile.mkdtemp()) / "deep" / "nested"
    orig = dl_mod.download_file
    content = b"x"
    dl_mod.download_file = _fake_downloader(content)
    try:
        sha1 = hashlib.sha1(content).hexdigest()
        cloud = _FakeCloud(_info(name='a/b:"c".mp4', sha1=sha1))
        r = asyncio.run(download_by_pick_code(cloud, "PC1", tmp))
        assert tmp.is_dir()                         # 自动创建
        assert r["dest"].parent == tmp
        assert "/" not in r["dest"].name and ":" not in r["dest"].name
        assert r["dest"].exists()
    finally:
        dl_mod.download_file = orig
        shutil.rmtree(tmp.parent.parent, ignore_errors=True)


# ── AI 工具链（search_115 -> download_115）──────────────────────────────────
def test_ai_search_then_download_tools():
    from ai import tools as ai_tools
    from core.app import state

    class _Raw:
        """模拟生产 cloud.raw（= Open115Client）：search 返回已归一的条目。"""

        async def search_files(self, kw, limit=20):
            raw = [
                dict(SEARCH_ENTRY),
                {"file_id": "2", "file_name": "dir", "file_size": "0",
                 "pick_code": "PC2", "parent_id": "0", "file_category": "0",
                 "sha1": ""},
            ]
            return {"list": [Open115Client.search_entry_abbr(it) for it in raw]}

        async def get_download_url(self, pc: str) -> dict:
            # 与 _fake_downloader(b"data") 的内容 sha1 一致 -> 校验通过落地
            return _info(name="a.mp4", sha1=hashlib.sha1(b"data").hexdigest(), pc=pc)

    class _Accts:
        def __init__(self, cloud):
            self._c = cloud

        async def get(self):
            return self._c

    class _Cloud:
        raw = _Raw()

    tmp = Path(tempfile.mkdtemp())
    old_accts, old_df = state.accounts, dl_mod.download_file
    content = b"data"
    dl_mod.download_file = _fake_downloader(content)
    state.accounts = _Accts(_Cloud())
    try:
        # search_115：序号 + 目录标记 + sha1 前 12 + pc（download_115 的衔接键）
        out = asyncio.run(ai_tools.dispatch("search_115", {"keyword": "x"}))
        assert "1. movie.mp4" in out
        assert "481.1MB" in out                      # human_bytes(504487964)
        assert "sha1=ef230d914206" in out and "pc=csgpl4johgbi6dds2" in out
        assert "2. [目录] dir" in out
        # download_115：成功回执
        out2 = asyncio.run(ai_tools.dispatch(
            "download_115", {"pick_code": "PCX", "dest_dir": str(tmp)}))
        assert out2.startswith("✅ 已下载") and str(tmp) in out2
        # 工具已注册且参数 schema 齐全
        names = [t["name"] for t in ai_tools.tool_specs()]
        assert "download_115" in names and "search_115" in names
        dl = next(t for t in ai_tools.tool_specs() if t["name"] == "download_115")
        assert set(dl["parameters"]["required"]) == {"pick_code", "dest_dir"}
    finally:
        state.accounts = old_accts
        dl_mod.download_file = old_df
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok: {fn.__name__}")
    print("test_search_download: ALL PASS")
