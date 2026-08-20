"""持久化层测试（aiosqlite）。sandbox 未装 aiosqlite 时自动跳过。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import aiosqlite  # noqa: F401
    HAVE_AIOSQLITE = True
except Exception:
    HAVE_AIOSQLITE = False

if HAVE_AIOSQLITE:
    from persistence.db import Database  # noqa: E402
    from persistence.models import (  # noqa: E402
        STATUS_DONE, STATUS_FAILED, TaskRow,
    )


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


async def _scoped(tmpdir):
    db = Database(Path(tmpdir) / "t.db")
    await db.init()

    # 任务增改查
    await db.insert_task(TaskRow(
        task_id="t1", user_id=1, filename="a.mp4", size=100,
        target_dir="/x", status="downloading",
    ))
    await db.update_task("t1", status=STATUS_DONE, method="秒传")
    tasks = await db.recent_tasks(10)
    assert len(tasks) == 1
    assert tasks[0].status == STATUS_DONE
    assert tasks[0].method == "秒传"

    stats = await db.task_stats()
    assert stats["total"] == 1 and stats["done"] == 1 and stats["秒传"] == 1

    # 频道规则 upsert/list/delete
    r = await db.upsert_rule(-100123, "test", ["电影"], ["cam"], "/tg/movies", True)
    assert r.channel_id == -100123
    rules = await db.list_rules()
    assert len(rules) == 1 and rules[0].whitelist == ["电影"]
    # 同 channel_id 再次 upsert -> 更新
    await db.upsert_rule(-100123, "test2", ["纪录片"], [], "/tg/doc", True)
    assert len(await db.list_rules()) == 1
    await db.delete_rule(r.id)
    assert len(await db.list_rules()) == 0

    # 账号同步/更新
    class _C:
        def __init__(s, name, mode, weight=1):
            s.name, s.mode, s.weight = name, mode, weight
    await db.sync_accounts([_C("main", "open"), _C("alt", "cookie", 2)])
    await db.update_account("main", status="ok", touch=True)
    accs = await db.list_accounts()
    assert {a.name for a in accs} == {"main", "alt"}
    main = next(a for a in accs if a.name == "main")
    assert main.status == "ok" and main.weight == 1

    # 日志：插入 + 级别过滤 + FIFO
    from persistence.models import LogRow
    await db.insert_logs([LogRow(ts=1.0, level="INFO", logger="x", message="hi")])
    await db.insert_logs([LogRow(ts=2.0, level="ERROR", logger="x", message="boom")])
    all_logs = await db.recent_logs(10)
    assert len(all_logs) == 2
    err_only = await db.recent_logs(10, level="ERROR")
    assert len(err_only) == 1 and err_only[0].level == "ERROR"

    await db.close()


def test_db_crud():
    if not HAVE_AIOSQLITE:
        print("  skip: aiosqlite 未安装（sandbox 预期）")
        return
    with tempfile.TemporaryDirectory() as d:
        run(_scoped(d))


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print(f"  ok: {fn.__name__}")
    print("test_db: ALL PASS")
