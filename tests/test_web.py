"""Web 台测试：路径工具 / 离线行映射 / 任务注册表 / 配置写盘 / 路由冒烟（整栈渲染）。

不触网：路由测试用假 state/cloud/db（鸭子类型 + starlette TestClient），只覆盖
渲染与分支；115 真实调用（浏览/下载/授权握手）在用户机上验证。缺 fastapi/httpx
时整组跳过（打印 SKIP，不算失败）——与 test_tb 的缺依赖打桩同哲学。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
import types

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 重依赖桩（沙箱未装时注入；真实环境直接用真包）——仿 test_tb
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

try:
    import httpx  # noqa: F401
    from starlette.testclient import TestClient
    _CLIENT_OK = True
except ModuleNotFoundError:
    _CLIENT_OK = False

from web.helpers import path_crumbs, path_join, path_parent  # noqa: E402
from web.offline import offline_row                          # noqa: E402
from web.files import _MAX_JOBS, _new_job, job_snapshot      # noqa: E402


# ── 纯函数 ───────────────────────────────────────────────────────────────

def test_path_helpers():
    """路径拼接/父目录/面包屑：115 全程 /，Windows 上不可走 Path。"""
    assert path_join("/tg115bot", "a.mkv") == "/tg115bot/a.mkv"
    assert path_join("/", "x") == "/x"
    assert path_parent("/tg115bot/sub") == "/tg115bot"
    assert path_parent("/tg115bot") == "/"
    assert path_parent("/") == "/"
    crumbs = path_crumbs("/tg115bot/movies")
    assert [c["name"] for c in crumbs] == ["/", "tg115bot", "movies"]
    assert [c["path"] for c in crumbs] == ["/", "/tg115bot", "/tg115bot/movies"]


def test_offline_row_mapping():
    """离线任务状态映射：-1 失败 / 1 进行中带百分比 / 2 完成 / 未知归中。"""
    done = offline_row({"status": 2, "name": "task-a", "info_hash": "ih2"})
    assert done["icon"] == "✅" and done["pct"] == "" and done["name"] == "task-a"
    run = offline_row({"status": 1, "name": "", "url": "magnet:?xt=1", "percentDone": 55})
    assert run["icon"] == "📥" and run["pct"] == "55%"
    assert run["name"] == "magnet:?xt=1"
    bad = offline_row({"status": -1, "name": "x", "info_hash": "ih"})
    assert bad["icon"] == "❌" and bad["cls"] == "err"
    unk = offline_row({"status": "junk", "name": "y"})
    assert unk["icon"] == "•"


def test_job_registry_snapshot_order():
    """任务注册表：快照按时间倒序（新任务在前）。"""
    a = _new_job("download", "one")
    b = _new_job("upload", "two")
    snap = job_snapshot()
    assert snap[0]["id"] == b["id"] and snap[1]["id"] == a["id"]
    assert a["status"] == "running"


def test_job_prune_keeps_running():
    """裁剪只清已结束的最老任务，running 不动；上限内不裁。"""
    from web import files as wf
    old_ids = []
    for i in range(_MAX_JOBS):
        j = _new_job("download", f"j{i}")
        j["status"] = "done"
        old_ids.append(j["id"])
    keep = _new_job("download", "live")           # running，不应被裁
    assert keep["id"] in wf._jobs
    extra = _new_job("download", "overflow")      # 触发溢出 -> 裁最老的 done
    assert extra["id"] in wf._jobs
    assert old_ids[0] not in wf._jobs
    assert keep["id"] in wf._jobs
    wf._jobs.clear()


# ── 整栈路由冒烟（TestClient + 假 state） ────────────────────────────────

def _build_client():
    """假 state 组装 + create_app。返回 (TestClient, fakes)。"""
    from config import AccountCfg, AppConfig, TelegramCfg
    from web.app import create_app

    class FakeRaw:
        def __init__(self):
            self.request_count, self.daily_limit = 123, 9500
            self.exchanged = []

        async def get_file_info(self, path):
            return {"file_id": 7, "file_name": path}

        async def list_files(self, cid, limit=32, offset=0):
            return {"list": [
                {"fn": "movies", "fc": "0", "fs": 0, "fid": "11", "pc": ""},
                {"fn": "a.mkv", "fc": "1", "fs": 2048, "fid": "12", "pc": "PC1"},
            ], "count": 2}

        async def list_files_all(self, cid=0, limit=100, max_items=5000):
            data = await self.list_files(cid)
            return data["list"]

        async def search_files(self, kw, limit=20):
            return {"list": [{"fn": f"{kw}.mkv", "fc": "1", "fs": 4096,
                              "pc": "PC9", "sha1": "abc12345"}], "count": 1}

        async def user_space(self):
            return {"used": 300 * 1024**3, "total": 1000 * 1024**3}

        async def offline_quota(self):
            return {"used": 2, "count": 10}

        async def offline_list_all(self):
            return [
                {"status": 2, "name": "done-task", "info_hash": "IH2", "percentDone": 100},
                {"status": 1, "name": "run-task", "info_hash": "IH1", "percentDone": 40},
            ]

        async def offline_add(self, url, save):
            self.added = (url, save)
            return True

        async def offline_del(self, ih, del_source_file=0):
            self.deleted = ih
            return True

        async def delete_files(self, file_ids):
            self.deleted_files = file_ids
            return True

        async def move_files(self, file_ids, to_cid):
            self.moved = (file_ids, to_cid)
            return True

        async def rename_file(self, file_id, new_name):
            self.renamed = (file_id, new_name)
            return True

        async def create_dir_recursive(self, path):
            self.created_dir = path
            return 7

        async def start_qr_auth(self):
            return {"uid": "U1", "time": "1", "sign": "S",
                    "qrcode": "https://example.com/qr", "verifier": "V"}

        async def poll_qr_status(self, uid, t, sign):
            return 2

        async def exchange_qr_token(self, uid, verifier):
            self.exchanged.append(uid)
            return True

        def invalidate_path_cache(self, path=""):
            pass

    class FakeCloud:
        def __init__(self):
            self.account = AccountCfg(name="main")
            self.raw = FakeRaw()

    class FakeAccounts:
        def __init__(self):
            self.cloud = FakeCloud()
            self.authorized = []

        @property
        def primary(self):
            return self.cloud

        def names(self):
            return ["main"]

        def status_list(self):
            return [{"name": "main", "mode": "open", "weight": 1,
                     "ready": True, "status": "ok", "cooldown_sec": 0}]

        def mark_authorized(self, name):
            self.authorized.append(name)

    class FakeQueue:
        def qsize(self):
            return 0

    class FakeWorkspace:
        def free_bytes(self):
            return 50 * 1024**3

    class FakeDB:
        async def task_stats(self):
            return {"total": 3, "done": 2, "failed": 1, "cancelled": 0, "秒传": 1}

        async def recent_tasks(self, limit=50):
            from persistence.db import TaskRow
            return [TaskRow(
                task_id="t1", user_id=1, status="done", filename="v.mp4",
                size=10, target_dir="/tg115bot", method="秒传", source="manual",
                error="", created_at=1700000000, progress=100,
            )]

        async def delete_task(self, task_id):
            self.deleted = task_id

        async def list_accounts(self):
            return []

        async def recent_logs(self, limit=200, level=None):
            from persistence.db import LogRow
            return [LogRow(ts=1700000000, level="INFO", logger="x", message="hi")]

        async def list_rules(self):
            return []

    tg = types.SimpleNamespace(
        config=AppConfig(
            accounts=[AccountCfg(name="main")],
            telegram=TelegramCfg(api_id=1, api_hash="h", bot_token="t"),
        ),
        queue=FakeQueue(), workspace=FakeWorkspace(),
        task_progress={}, monitor=None,
    )
    accounts = FakeAccounts()
    db = FakeDB()
    app = create_app(tg, db, accounts)
    app.state.cloud = accounts.cloud
    return TestClient(app), tg, accounts, db


def _with_client(fn):
    """缺 httpx 时优雅跳过（run_all 只把抛异常当失败）。"""
    if not _CLIENT_OK:
        print("  (skip: 缺 httpx/starlette TestClient)")
        return
    fn()


def test_routes_smoke():
    """主要页面 200 且关键内容在位：仪表盘/文件/离线/任务/日志/配置。"""
    def run():
        client, tg, accounts, db = _build_client()
        auth = ("admin", "changeme")
        r = client.get("/", auth=auth)
        assert r.status_code == 200 and "115 API 余量" in r.text, r.text[:300]
        r = client.get("/files", auth=auth)
        assert r.status_code == 200 and "a.mkv" in r.text and "movies" in r.text
        r = client.get("/files?kw=1080p", auth=auth)     # 搜索模式
        assert r.status_code == 200 and "1080p.mkv" in r.text and "abc12345" in r.text
        r = client.get("/offline", auth=auth)
        assert r.status_code == 200 and "done-task" in r.text and "run-task" in r.text
        r = client.get("/tasks", auth=auth)
        assert r.status_code == 200 and "v.mp4" in r.text and "删记录" in r.text
        r = client.get("/logs", auth=auth)
        assert r.status_code == 200 and "hi" in r.text
        r = client.get("/logs?view=stdout", auth=auth)
        assert r.status_code == 200
        r = client.get("/config", auth=auth)
        assert r.status_code == 200 and "config.yaml" in r.text
        r = client.get("/accounts", auth=auth)
        assert r.status_code == 200
        # partial 路由（HTMX 局部刷新端点）也要能渲染
        for path in ("/partials/dashboard", "/partials/files", "/partials/jobs",
                     "/partials/offline", "/partials/tasks", "/partials/stdout",
                     "/partials/config-editor", "/partials/auth-poll"):
            r = client.get(path, auth=auth)
            assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:200]}"
        # 未带凭据 -> 401
        assert client.get("/files").status_code == 401
    _with_client(run)


def test_file_actions_and_jobs():
    """文件操作：搜索结果直下（起传输任务）、删除、新建目录、离线增删。"""
    def run():
        client, tg, accounts, db = _build_client()
        auth = ("admin", "changeme")
        # pick_code 直下：路由立即返回任务片段，后台任务跑桩链路
        r = client.post("/files/download", data={"pc": "PC9", "name": "x.mkv",
                                                 "value": "~/Downloads"}, auth=auth)
        assert r.status_code == 200 and "传输任务" in r.text
        assert any(j["name"] == "x.mkv" for j in job_snapshot())
        # 删除 / 新建目录（走假 raw，动作后返回列表片段）
        r = client.post("/files/delete", data={"path": "/tg115bot", "name": "a.mkv"}, auth=auth)
        assert r.status_code == 200 and "已删除" in r.text
        r = client.post("/files/mkdir", data={"path": "/tg115bot", "value": "newdir"}, auth=auth)
        assert r.status_code == 200 and "已创建" in r.text
        # 离线添加 / 删除
        r = client.post("/offline/add", data={"url": "magnet:?xt=abc"}, auth=auth)
        assert r.status_code == 200 and "已提交" in r.text
        assert accounts.cloud.raw.added[1] == tg.config.upload.target_dir
        r = client.post("/offline/delete", data={"info_hash": "IH1"}, auth=auth)
        assert r.status_code == 200 and "已删除" in r.text
        assert accounts.cloud.raw.deleted == "IH1"
    _with_client(run)


def test_task_record_delete_guard():
    """任务记录删除：仅终态可删（downloading 拒绝）。"""
    def run():
        client, tg, accounts, db = _build_client()
        auth = ("admin", "changeme")
        r = client.post("/tasks/t1/delete", auth=auth)
        assert r.status_code == 200 and "已删除" in r.text
        assert getattr(db, "deleted", None) == "t1"

        async def fake_recent(limit=50):
            from persistence.db import TaskRow
            return [TaskRow(
                task_id="t2", user_id=1, status="downloading", filename="w.mp4",
                size=1, target_dir="/x", error="", created_at=1700000000, progress=1,
            )]
        db.recent_tasks = fake_recent
        r = client.post("/tasks/t2/delete", auth=auth)
        assert r.status_code == 200 and "仅完成" in r.text
    _with_client(run)


def test_config_save_and_qr_flow():
    """配置保存写盘（坏 YAML 拒绝）+ 扫码授权轮询到确认态。"""
    def run():
        from tb import ops
        client, tg, accounts, db = _build_client()
        auth = ("admin", "changeme")
        # 坏 YAML -> 不落盘
        r = client.post("/config/save", data={"text": "a: [bad"}, auth=auth)
        assert "未保存" in r.text
        # 好 YAML -> 写盘（重定向 CONFIG_FILE 到临时文件）
        tmp = Path(tempfile.mkdtemp()) / "config.yaml"
        old = ops.CONFIG_FILE
        ops.CONFIG_FILE = tmp
        try:
            good = ("telegram:\n  api_id: 1\n  api_hash: \"h\"\n"
                    "  bot_token: \"t\"\naccounts:\n  - name: main\n"
                    "web:\n  enable: true\n")
            r = client.post("/config/save", data={"text": good}, auth=auth)
            assert "已保存" in r.text and tmp.read_text("utf-8").startswith("telegram:")
            # 开关翻转（现值取反写盘）
            r = client.post("/config/toggle", data={"key": "web.enable"}, auth=auth)
            assert "已写盘" in r.text
        finally:
            ops.CONFIG_FILE = old
        # 扫码：start -> 轮询 2（确认）-> 换 token + mark_authorized
        r = client.post("/auth/qr/start", auth=auth)
        assert r.status_code == 200 and ("data:image/svg+xml" in r.text or "example.com/qr" in r.text)
        r = client.get("/partials/auth-poll", auth=auth)
        assert "授权成功" in r.text
        assert accounts.authorized == ["main"]
        assert accounts.cloud.raw.exchanged == ["U1"]
    _with_client(run)


def test_stdout_tail():
    """stdout tail：读尾行；文件缺失返回空串不抛。"""
    from tb import service
    tmp = Path(tempfile.mkdtemp()) / "stdout.log"
    tmp.write_text("l1\nl2\nl3\n", encoding="utf-8")
    old = service.STDOUT_LOG
    service.STDOUT_LOG = tmp
    try:
        from web.views import _stdout_tail
        assert "l2\nl3" in _stdout_tail(2)
        service.STDOUT_LOG = tmp.parent / "nope.log"
        assert _stdout_tail() == ""
    finally:
        service.STDOUT_LOG = old
