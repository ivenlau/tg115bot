"""manual.py / cloud115.download / filesystem.find_entry 纯逻辑测试（手写 fake，零网络）。

覆盖：downurl 响应归一、文件名净化/去重、list_files_all 翻页终止条件、
find_entry 父目录解析、格式化函数、菜单表与 parser 一致性、--account 三种位置。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 依赖桩（沙箱未装时注入；真实环境直接用真包）——仿 test_oss_protocol
try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    _aio = types.ModuleType("aiohttp")
    _aio.ClientSession = type("ClientSession", (), {"__init__": lambda self, *a, **k: None})
    _aio.ClientTimeout = lambda **k: None
    _aio.ClientError = Exception
    sys.modules["aiohttp"] = _aio
try:
    import aiofiles  # noqa: F401
except ModuleNotFoundError:
    _af = types.ModuleType("aiofiles")
    _af.open = lambda *a, **k: None
    sys.modules["aiofiles"] = _af

from cloud115.download import parse_downurl, sanitize_name, unique_dest  # noqa: E402
from cloud115.filesystem import find_entry  # noqa: E402
from cloud115.openapi import Open115Client  # noqa: E402
import scripts.manual as manual  # noqa: E402


# ── fake：真 Open115Client + 替换 _request（翻页逻辑测的是真实现） ──────


def _fake_api(folder_infos: dict, listing: list, *, ignore_offset: bool = False):
    """真 Open115Client，_request 换成按 URL 分发的假实现。

    folder_infos: path -> get_info 响应；listing: ufile/files 全量条目（按 offset 切片）。
    ignore_offset=True 模拟 API 忽略 offset 永远返回首页（测 max_items 兜底）。
    """
    c = Open115Client(Path("/nonexistent-token.json"))

    async def fake(method, url, *, params=None, data=None, auth=True, _retry=True):
        params = params or {}
        if "folder/get_info" in url:
            return {"code": 0, "data": folder_infos.get(params.get("path"))}
        if "ufile/files" in url:
            off, lim = params.get("offset", 0), params.get("limit", 32)
            items = listing[:lim] if ignore_offset else listing[off:off + lim]
            return {"code": 0, "data": {"list": items, "count": len(listing)}}
        raise AssertionError(f"意外的 URL: {url}")

    c._request = fake
    return c


class _Cloud:
    """filesystem 期望的最小 cloud 形状：.raw 是 Open115Client。"""

    def __init__(self, api):
        self.raw = api


# ── cloud115.download ──────────────────────────────────────────────────


def test_parse_downurl():
    # url 为 dict（web 形态）
    a = parse_downurl({"file_name": "a.mkv", "file_size": "123", "pick_code": "pc1",
                       "sha1": "ABCDEF00", "url": {"url": "https://x/a.mkv"}})
    assert a == {"file_name": "a.mkv", "file_size": 123, "pick_code": "pc1",
                 "sha1": "abcdef00", "url": "https://x/a.mkv"}
    # url 为纯字符串
    b = parse_downurl({"file_name": "b", "file_size": 0, "pick_code": "", "sha1": "",
                       "url": "https://x/b"})
    assert b["url"] == "https://x/b" and b["sha1"] == "" and b["file_size"] == 0
    # 缺 url 抛错
    for bad in ({"file_name": "x"}, {"url": {"url": "  "}}, {}):
        try:
            parse_downurl(bad)
            raise AssertionError("应当抛 RuntimeError")
        except RuntimeError:
            pass


def test_sanitize_and_unique_dest():
    assert sanitize_name('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    assert sanitize_name("  sp  ace\nname  ") == "sp ace name"
    assert sanitize_name("...") == "115_file"          # 去点后为空 -> 兜底名
    assert sanitize_name("x" * 300) == "x" * 200       # 截断

    tmp = Path(tempfile.mkdtemp(prefix="tg115_test_"))
    try:
        first = unique_dest(tmp, "f.mkv")
        first.write_bytes(b"1")
        second = unique_dest(tmp, "f.mkv")
        assert second.name == "f (1).mkv"
        second.write_bytes(b"2")
        assert unique_dest(tmp, "f.mkv").name == "f (2).mkv"
        assert first.read_bytes() == b"1"              # 绝不覆盖
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── list_files_all 翻页（真实现 + fake _request） ──────────────────────


def test_list_files_all_pagination():
    mk = lambda i: {"fn": f"f{i}", "fid": str(i), "fc": "1"}

    async def run(items, limit, max_items=5000, **kw):
        api = _fake_api({}, items, **kw)
        return await api.list_files_all(0, limit=limit, max_items=max_items)

    # 翻页取全：7 项 limit=3 -> 3+3+1，短批终止
    got = asyncio.run(run([mk(i) for i in range(7)], 3))
    assert [g["fn"] for g in got] == [f"f{i}" for i in range(7)]

    # count 提前终止：count=4 时第二页累计 6>=4 即停（数据异常场景不续拉）
    async def run2():
        # count 报 4：第一页 3 条后 offset=3，第二页 3 条累计 6 >= 4 -> 停
        c = Open115Client(Path("/nonexistent-token.json"))

        async def fake(method, url, *, params=None, **kw):
            params = params or {}
            off, lim = params["offset"], params["limit"]
            items = [mk(i) for i in range(9)][off:off + lim]
            return {"code": 0, "data": {"list": items, "count": 4}}
        c._request = fake
        return await c.list_files_all(0, limit=3, max_items=100)
    assert len(asyncio.run(run2())) == 6

    # API 忽略 offset（永远首页 3 条）：max_items=7 兜底截断且不死循环
    stuck = asyncio.run(run([mk(i) for i in range(9)], 3, max_items=7, ignore_offset=True))
    assert len(stuck) == 7
    assert [s["fn"] for s in stuck[:3]] == ["f0", "f1", "f2"]   # 第一页
    assert stuck[3]["fn"] == "f0"                                # 原地重复（offset 被忽略）


# ── find_entry 父目录解析 ───────────────────────────────────────────────


def test_find_entry():
    listing = [
        {"fn": "dirA", "fid": "10", "fc": "0", "pc": "pcd"},
        {"fn": "a.mkv", "fid": "11", "fc": "1", "fs": 1024, "pc": "pcf"},
        {"fn": "b.txt", "fid": "12", "fc": "1", "fs": 5, "pc": "pcb"},
    ]
    api = _fake_api({"/tg115bot": {"file_id": 5, "file_name": "tg115bot"}}, listing)
    cloud = _Cloud(api)

    async def go(path):
        return await find_entry(cloud, path)

    # 文件：拿到 pc（下载直链必需）
    e = asyncio.run(go("/tg115bot/a.mkv"))
    assert e["pc"] == "pcf" and e["fid"] == "11" and e["fs"] == 1024
    # 目录：fc=0
    d = asyncio.run(go("/tg115bot/dirA"))
    assert d["fc"] == "0" and d["pc"] == "pcd"
    # 父目录为 / 的边界
    api2 = _fake_api({"/": {"file_id": 0}}, listing)
    e2 = asyncio.run(find_entry(_Cloud(api2), "/a.mkv"))
    assert e2["pc"] == "pcf"
    # 不存在
    try:
        asyncio.run(go("/tg115bot/none"))
        raise AssertionError("应当抛 FileNotFoundError")
    except FileNotFoundError:
        pass
    # 父目录不存在（get_info 返回空 data -> None -> resolve_cid 抛）
    try:
        asyncio.run(go("/nope/a.mkv"))
        raise AssertionError("应当抛 FileNotFoundError")
    except FileNotFoundError:
        pass


# ── 格式化 ─────────────────────────────────────────────────────────────


def test_fmt_ls_and_offline():
    items = [
        {"fn": "z.txt", "fc": "1", "fs": 3},
        {"fn": "adir", "fc": "0"},
        {"fn": "adir2", "fc": 0, "file_name": "adir2"},   # fc 为 int 0 也应判目录
        {"fn": "a.mkv", "fc": "1", "fs": 4096, "size": 4096},
    ]
    got = manual.sort_entries(items)
    assert [g["fn"] for g in got][:2] == ["adir", "adir2"]      # 目录排前
    assert manual.entry_is_dir(items[2]) is True
    line = manual.fmt_ls_line(items[3])
    assert line.startswith("📄 a.mkv  ") and "4" in line
    assert manual.fmt_ls_line(items[1]) == "📂 adir/"

    t = {"name": "movie", "status": 1, "percentDone": 42, "info_hash": "abcdef1234"}
    assert manual.fmt_offline_line(t) == "⬇️ movie 42%  [abcdef12]"
    assert manual.fmt_offline_line({"name": "x", "status": 2, "info_hash": "ih"}).startswith("✅")
    assert manual.fmt_offline_line({"name": "y", "status": -1, "info_hash": "ih"}).startswith("❌")


# ── 菜单表 / build_argv / parser 一致性 ────────────────────────────────


def _fill_answers(item: "manual.MenuItem") -> dict:
    """必填字段给样例值（默认空串的必填位置参数用 x 顶上），开关全开。"""
    ans = {}
    for f in item.fields:
        if f.boolean:
            ans[f.dest] = "y"
        elif f.positional and f.multi:
            ans[f.dest] = "a b"
        else:
            ans[f.dest] = f.default or "x"
    return ans


def test_menu_and_parser_consistency():
    parser = manual.build_parser()
    nums = [m.num for m in manual.MENU]
    assert len(nums) == len(set(nums)) and "0" not in manual.MENU_BY_NUM
    for item in manual.MENU:
        if item.action == manual.SWITCH:
            continue
        top = item.action.split()[0]
        assert top in manual.COMMANDS, f"菜单动作 {item.action} 不在 COMMANDS"
        argv = manual.build_argv(item, _fill_answers(item))
        args = parser.parse_args(argv)               # 任一字段名/选项名错位都会炸
        assert getattr(args, "run", None) is not None, f"{item.action} 缺 run"


def test_build_argv_rules():
    item = manual.MENU_BY_NUM["6"]                   # offline add
    # 开关 n -> 不追加；flag 空值 -> 省略；multi -> 空格拆分
    argv = manual.build_argv(item, {"urls": "magnet:?xt=1 http://a", "dir": ""})
    assert argv == ["offline", "add", "magnet:?xt=1", "http://a"]
    argv = manual.build_argv(item, {"urls": "u1", "dir": "/t"})
    assert argv == ["offline", "add", "u1", "-d", "/t"]
    # 未作答字段回退 default
    argv = manual.build_argv(manual.MENU_BY_NUM["1"], {})
    assert argv == ["ls", "/"]


def test_parser_account_positions():
    p = manual.build_parser()
    # ⚠️ 子解析器 default=SUPPRESS：顶层值不被覆盖回空串
    assert p.parse_args(["--account", "b", "ls", "/x"]).account == "b"
    assert p.parse_args(["ls", "/x", "--account", "b"]).account == "b"
    assert p.parse_args(["ls", "/x"]).account == ""
    assert p.parse_args([]).account == ""            # 无子命令 -> 菜单模式
    assert p.parse_args(["auth"]).run is manual.cmd_auth


# ── auth 扫码授权（fake cloud，零网络；sleep 与二维码渲染打桩） ──────────


class _AuthCloud:
    """cmd_auth 期望的最小形状：.raw 三件套 + ensure_login 探活。"""

    def __init__(self, statuses):
        self.raw = self
        self.statuses = list(statuses)   # poll_qr_status 逐次弹出的状态
        self.exchanged = False

    async def start_qr_auth(self):
        return {"uid": 1, "time": 2, "sign": "s",
                "qrcode": "https://115.com/qr", "verifier": "v"}

    async def poll_qr_status(self, uid, t, sign):
        return self.statuses.pop(0) if self.statuses else 2

    async def exchange_qr_token(self, uid, verifier):
        self.exchanged = True
        return True

    async def ensure_login(self):
        return self.exchanged


def test_cmd_auth():
    shown = []

    async def go(statuses):
        cloud = _AuthCloud(statuses)
        ctx = manual.Ctx(cfg=None, account=types.SimpleNamespace(name="t"), rate=None,
                         cloud=cloud)
        return await manual.cmd_auth(ctx, None), cloud

    real_sleep, real_print_qr = asyncio.sleep, manual._print_qr

    async def fast_sleep(_sec):
        return None

    asyncio.sleep = fast_sleep                 # 跳过出码 5s + 轮询 2s 等待
    manual._print_qr = shown.append            # 免 qrcode 库、不打终端
    try:
        # 成功：待扫 -> 已扫待确认 -> 确认 -> 换 token -> 探活通过
        rc, cloud = asyncio.run(go([None, 1, 2]))
        assert rc == 0 and cloud.exchanged
        assert shown == ["https://115.com/qr"]        # 出码内容是 QR 数据本身
        # 二维码过期 / APP 里取消
        assert asyncio.run(go([-1]))[0] == 1
        assert asyncio.run(go([-2]))[0] == 1
    finally:
        asyncio.sleep, manual._print_qr = real_sleep, real_print_qr


def test_menu_has_auth_item():
    item = manual.MENU_BY_NUM["16"]
    assert item.action == "auth" and not item.fields
    assert manual.build_argv(item, {}) == ["auth"]


def test_expand_sources():
    root = Path(tempfile.mkdtemp())
    try:
        (root / "sub").mkdir()
        for n in ("p1.jpg", "p2.jpg", "q.txt", "sub/p3.jpg"):
            (root / n).write_text("x")
        r = str(root)
        # 通配符：只匹配 p*.jpg；文件来源 base=父目录（远端用原名）
        files, bases, miss = manual.expand_sources([r + "/p*.jpg"])
        assert sorted(f.name for f in files) == ["p1.jpg", "p2.jpg"] and not miss
        assert all(bases[f] == root for f in files)
        # 目录 + 通配符混合来源：递归 4 个，跨来源去重；目录来源 base=目录自身（保留结构）
        files, bases, miss = manual.expand_sources([r, r + "/p*.jpg"])
        assert len(files) == 4 and not miss
        assert bases[root / "sub" / "p3.jpg"] == root
        # 无匹配 / 路径不存在 -> 记入 missing，不影响其余来源
        files, _, miss = manual.expand_sources([r + "/zz*.nope", r + "/q.txt"])
        assert [f.name for f in files] == ["q.txt"] and "无匹配" in miss[0]
        files, _, miss = manual.expand_sources([r + "/ghost"])
        assert not files and "路径不存在" in miss[0]
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # parser 接受多路径（nargs="+"）
    args = manual.build_parser().parse_args(["upload", "/a.jpg", "/b.jpg", "-d", "/t"])
    assert args.sources == ["/a.jpg", "/b.jpg"] and args.dir == "/t"


if __name__ == "__main__":
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  ok: {name}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  FAIL: {name}: {e!r}")
    sys.exit(1 if fails else 0)
