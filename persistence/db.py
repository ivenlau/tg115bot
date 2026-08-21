"""SQLite 持久化（aiosqlite）：任务历史 / 频道规则 / 账号状态 / 日志。

单连接、复用同一 aiosqlite 连接；所有方法均为 async。Web 台与 channel_monitor
通过本模块读写持久数据；运行态（进行中任务、活跃 client）仍在 core.app.state 内存。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, List, Optional

import aiosqlite

from persistence.models import (
    AccountRow, BackupRow, ChannelRuleRow, LogRow, MovieSubRow,
    OfflineTaskRow, RssFeedRow, TaskRow, STATUS_QUEUED,
)

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    user_id     INTEGER,
    source      TEXT,
    filename    TEXT,
    size        INTEGER,
    target_dir  TEXT,
    status      TEXT,
    method      TEXT,
    error       TEXT,
    chat_id     INTEGER,
    message_id  INTEGER,
    channel_id  INTEGER,
    created_at  REAL,
    updated_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);

CREATE TABLE IF NOT EXISTS channel_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  INTEGER NOT NULL,
    title       TEXT,
    whitelist   TEXT,            -- JSON 数组
    blacklist   TEXT,            -- JSON 数组
    target_dir  TEXT,
    enabled     INTEGER DEFAULT 1,
    created_at  REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rules_channel ON channel_rules(channel_id);

CREATE TABLE IF NOT EXISTS accounts (
    name         TEXT PRIMARY KEY,
    mode         TEXT,
    weight       INTEGER DEFAULT 1,
    enabled      INTEGER DEFAULT 1,
    status       TEXT,
    last_used_at REAL,
    last_error   TEXT,
    updated_at   REAL
);

CREATE TABLE IF NOT EXISTS offline_tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL,
    name        TEXT,
    save_path   TEXT,
    status      TEXT,
    source      TEXT,
    info_hash   TEXT,
    percent     INTEGER DEFAULT 0,
    retries     INTEGER DEFAULT 0,
    error       TEXT,
    chat_id     INTEGER,
    created_at  REAL,
    updated_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_offline_status ON offline_tasks(status);
CREATE INDEX IF NOT EXISTS idx_offline_url ON offline_tasks(url);

CREATE TABLE IF NOT EXISTS rss_feeds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL,
    name        TEXT,
    whitelist   TEXT,
    save_path   TEXT,
    enabled     INTEGER DEFAULT 1,
    chat_id     INTEGER,
    last_fetch  REAL DEFAULT 0,
    last_error  TEXT,
    created_at  REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_feeds_url ON rss_feeds(url);

CREATE TABLE IF NOT EXISTS rss_seen (
    url         TEXT PRIMARY KEY,
    title       TEXT,
    created_at  REAL
);

CREATE TABLE IF NOT EXISTS movie_subs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id     TEXT NOT NULL,
    movie_name  TEXT,
    save_path   TEXT,
    downloaded  INTEGER DEFAULT 0,
    download_url TEXT,
    chat_id     INTEGER,
    created_at  REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_movie_tmdb ON movie_subs(tmdb_id);

CREATE TABLE IF NOT EXISTS channel_backups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  INTEGER NOT NULL,
    title       TEXT,
    save_path   TEXT,
    status      TEXT,
    last_message_id INTEGER DEFAULT 0,
    total_done  INTEGER DEFAULT 0,
    skipped     INTEGER DEFAULT 0,
    chat_id     INTEGER,
    created_at  REAL,
    updated_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_backup_channel ON channel_backups(channel_id);

CREATE TABLE IF NOT EXISTS logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL,
    level   TEXT,
    logger  TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts DESC);
"""

# 日志表保留上限（FIFO 裁剪）
MAX_LOG_ROWS = 5000


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        log.info("数据库就绪: %s", self.path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database 未初始化，请先 await init()")
        return self._db

    # ── 任务 ──────────────────────────────────────────────────────────────
    async def insert_task(self, t: TaskRow) -> None:
        now = time.time()
        await self.conn.execute(
            """INSERT INTO tasks(task_id,user_id,source,filename,size,target_dir,
               status,method,error,chat_id,message_id,channel_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t.task_id, t.user_id, t.source, t.filename, t.size, t.target_dir,
             t.status or STATUS_QUEUED, t.method, t.error, t.chat_id, t.message_id,
             t.channel_id, t.created_at or now, now),
        )
        await self.conn.commit()

    async def update_task(self, task_id: str, *, status: Optional[str] = None,
                          method: Optional[str] = None, error: Optional[str] = None) -> None:
        sets, params = [], []
        if status is not None:
            sets.append("status=?"); params.append(status)
        if method is not None:
            sets.append("method=?"); params.append(method)
        if error is not None:
            sets.append("error=?"); params.append(error)
        if not sets:
            return
        sets.append("updated_at=?"); params.append(time.time())
        params.append(task_id)
        await self.conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE task_id=?", params)
        await self.conn.commit()

    async def recent_tasks(self, limit: int = 50) -> List[TaskRow]:
        async with self.conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [_task_row(r) for r in rows]

    async def task_stats(self) -> dict:
        out = {"total": 0, "done": 0, "failed": 0, "cancelled": 0, "running": 0,
               "秒传": 0, "oss": 0, "fs.upload": 0}
        async with self.conn.execute(
            "SELECT status, method, COUNT(*) c FROM tasks GROUP BY status, method"
        ) as cur:
            for r in await cur.fetchall():
                out["total"] += r["c"]
                if r["status"] in out:
                    out[r["status"]] += r["c"]
                if r["status"] == "done" and r["method"] in ("秒传", "oss", "fs.upload"):
                    out[r["method"]] += r["c"]
                if r["status"] in ("downloading", "uploading", "queued"):
                    out["running"] += r["c"]
        return out

    # ── 频道规则 ─────────────────────────────────────────────────────────
    async def list_rules(self) -> List[ChannelRuleRow]:
        async with self.conn.execute("SELECT * FROM channel_rules ORDER BY id") as cur:
            rows = await cur.fetchall()
        return [_rule_row(r) for r in rows]

    async def get_rule(self, channel_id: int) -> Optional[ChannelRuleRow]:
        async with self.conn.execute(
            "SELECT * FROM channel_rules WHERE channel_id=?", (channel_id,)
        ) as cur:
            r = await cur.fetchone()
        return _rule_row(r) if r else None

    async def upsert_rule(self, channel_id: int, title: str,
                          whitelist: List[str], blacklist: List[str],
                          target_dir: str, enabled: bool = True) -> ChannelRuleRow:
        wl = json.dumps(whitelist or [], ensure_ascii=False)
        bl = json.dumps(blacklist or [], ensure_ascii=False)
        now = time.time()
        await self.conn.execute(
            """INSERT INTO channel_rules(channel_id,title,whitelist,blacklist,target_dir,enabled,created_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(channel_id) DO UPDATE SET
                 title=excluded.title, whitelist=excluded.whitelist,
                 blacklist=excluded.blacklist, target_dir=excluded.target_dir,
                 enabled=excluded.enabled""",
            (channel_id, title, wl, bl, target_dir, 1 if enabled else 0, now),
        )
        await self.conn.commit()
        rule = await self.get_rule(channel_id)
        assert rule is not None
        return rule

    async def delete_rule(self, rule_id: int) -> None:
        await self.conn.execute("DELETE FROM channel_rules WHERE id=?", (rule_id,))
        await self.conn.commit()

    # ── 账号状态 ─────────────────────────────────────────────────────────
    async def sync_accounts(self, configs: list) -> None:
        """以配置为准同步账号元数据（凭据不入库，仅状态/权重）。"""
        now = time.time()
        for c in configs:
            await self.conn.execute(
                """INSERT INTO accounts(name,mode,weight,enabled,status,updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET mode=excluded.mode, weight=excluded.weight""",
                (c.name, c.mode, c.weight, 1, "unknown", now),
            )
        await self.conn.commit()

    async def update_account(self, name: str, *, status: Optional[str] = None,
                             last_error: Optional[str] = None, touch: bool = False) -> None:
        sets, params = [], []
        if status is not None:
            sets.append("status=?"); params.append(status)
        if last_error is not None:
            sets.append("last_error=?"); params.append(last_error[:500])
        if touch:
            sets.append("last_used_at=?"); params.append(time.time())
        if not sets:
            return
        sets.append("updated_at=?"); params.append(time.time()); params.append(name)
        await self.conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE name=?", params)
        await self.conn.commit()

    async def list_accounts(self) -> List[AccountRow]:
        async with self.conn.execute("SELECT * FROM accounts ORDER BY name") as cur:
            rows = await cur.fetchall()
        return [_account_row(r) for r in rows]

    # ── 离线任务 ─────────────────────────────────────────────────────────
    async def insert_offline(self, t: OfflineTaskRow) -> int:
        cur = await self.conn.execute(
            """INSERT INTO offline_tasks(url,name,save_path,status,source,info_hash,
               percent,retries,error,chat_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t.url, t.name, t.save_path, t.status, t.source, t.info_hash,
             t.percent, t.retries, t.error, t.chat_id, time.time(), time.time()),
        )
        await self.conn.commit()
        return cur.lastrowid or 0

    async def get_offline_by_url(self, url: str) -> Optional[OfflineTaskRow]:
        async with self.conn.execute(
            "SELECT * FROM offline_tasks WHERE url=? ORDER BY id DESC LIMIT 1", (url,)
        ) as cur:
            r = await cur.fetchone()
        return _offline_row(r) if r else None

    async def update_offline(self, offline_id: int, **kw) -> None:
        allowed = ("name", "status", "info_hash", "percent", "retries", "error")
        sets, params = [], []
        for k, v in kw.items():
            if k in allowed:
                sets.append(f"{k}=?"); params.append(v)
        if not sets:
            return
        sets.append("updated_at=?"); params.append(time.time()); params.append(offline_id)
        await self.conn.execute(f"UPDATE offline_tasks SET {', '.join(sets)} WHERE id=?", params)
        await self.conn.commit()

    async def offline_by_status(self, *statuses: str) -> List[OfflineTaskRow]:
        if not statuses:
            return []
        ph = ",".join("?" * len(statuses))
        async with self.conn.execute(
            f"SELECT * FROM offline_tasks WHERE status IN ({ph}) ORDER BY id", statuses
        ) as cur:
            rows = await cur.fetchall()
        return [_offline_row(r) for r in rows]

    # ── RSS 订阅 ─────────────────────────────────────────────────────────
    async def list_feeds(self, only_enabled: bool = False) -> List[RssFeedRow]:
        sql = "SELECT * FROM rss_feeds" + (" WHERE enabled=1" if only_enabled else "") + " ORDER BY id"
        async with self.conn.execute(sql) as cur:
            rows = await cur.fetchall()
        return [_feed_row(r) for r in rows]

    async def get_feed(self, feed_id: int) -> Optional[RssFeedRow]:
        async with self.conn.execute("SELECT * FROM rss_feeds WHERE id=?", (feed_id,)) as cur:
            r = await cur.fetchone()
        return _feed_row(r) if r else None

    async def add_feed(self, url: str, name: str, whitelist: List[str],
                       save_path: str, chat_id: int) -> Optional[RssFeedRow]:
        """新增订阅；URL 已存在返回 None。"""
        async with self.conn.execute(
            "SELECT id FROM rss_feeds WHERE url=?", (url,)
        ) as cur:
            if await cur.fetchone():
                return None
        cur = await self.conn.execute(
            """INSERT INTO rss_feeds(url,name,whitelist,save_path,enabled,chat_id,created_at)
               VALUES(?,?,?,?,1,?,?)""",
            (url, name, json.dumps(whitelist or [], ensure_ascii=False),
             save_path, chat_id, time.time()),
        )
        await self.conn.commit()
        return await self.get_feed(cur.lastrowid or 0)

    async def delete_feed(self, feed_id: int) -> None:
        await self.conn.execute("DELETE FROM rss_feeds WHERE id=?", (feed_id,))
        await self.conn.commit()

    async def update_feed(self, feed_id: int, *, last_error: Optional[str] = None,
                          touch: bool = False) -> None:
        sets, params = [], []
        if last_error is not None:
            sets.append("last_error=?"); params.append(last_error[:200])
        if touch:
            sets.append("last_fetch=?"); params.append(time.time())
        if not sets:
            return
        params.append(feed_id)
        await self.conn.execute(f"UPDATE rss_feeds SET {', '.join(sets)} WHERE id=?", params)
        await self.conn.commit()

    async def seen_link(self, url: str) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM rss_seen WHERE url=?", (url,)
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_seen(self, url: str, title: str = "") -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO rss_seen(url,title,created_at) VALUES(?,?,?)",
            (url, title, time.time()),
        )
        await self.conn.commit()

    # ── 电影订阅 ─────────────────────────────────────────────────────────
    async def list_movie_subs(self, only_pending: bool = False) -> List[MovieSubRow]:
        sql = "SELECT * FROM movie_subs" + (" WHERE downloaded=0" if only_pending else "") + " ORDER BY id"
        async with self.conn.execute(sql) as cur:
            rows = await cur.fetchall()
        return [_movie_row(r) for r in rows]

    async def get_movie_sub_by_tmdb(self, tmdb_id: str) -> Optional[MovieSubRow]:
        async with self.conn.execute(
            "SELECT * FROM movie_subs WHERE tmdb_id=?", (tmdb_id,)
        ) as cur:
            r = await cur.fetchone()
        return _movie_row(r) if r else None

    async def add_movie_sub(self, tmdb_id: str, movie_name: str, save_path: str,
                            chat_id: int) -> Optional[MovieSubRow]:
        async with self.conn.execute(
            "SELECT id FROM movie_subs WHERE tmdb_id=?", (tmdb_id,)
        ) as cur:
            if await cur.fetchone():
                return None
        cur = await self.conn.execute(
            """INSERT INTO movie_subs(tmdb_id,movie_name,save_path,downloaded,chat_id,created_at)
               VALUES(?,?,?,0,?,?)""",
            (tmdb_id, movie_name, save_path, chat_id, time.time()),
        )
        await self.conn.commit()
        async with self.conn.execute(
            "SELECT * FROM movie_subs WHERE tmdb_id=?", (tmdb_id,)
        ) as cur:
            r = await cur.fetchone()
        return _movie_row(r) if r else None

    async def update_movie_sub(self, sub_id: int, **kw) -> None:
        allowed = ("downloaded", "download_url")
        sets, params = [], []
        for k, v in kw.items():
            if k in allowed:
                sets.append(f"{k}=?"); params.append(1 if v is True else 0 if v is False else v)
        if not sets:
            return
        params.append(sub_id)
        await self.conn.execute(f"UPDATE movie_subs SET {', '.join(sets)} WHERE id=?", params)
        await self.conn.commit()

    async def delete_movie_sub(self, sub_id: int) -> None:
        await self.conn.execute("DELETE FROM movie_subs WHERE id=?", (sub_id,))
        await self.conn.commit()

    # ── 频道备份 ─────────────────────────────────────────────────────────
    async def get_backup_by_channel(self, channel_id: int) -> Optional[BackupRow]:
        async with self.conn.execute(
            "SELECT * FROM channel_backups WHERE channel_id=? ORDER BY id DESC LIMIT 1",
            (channel_id,),
        ) as cur:
            r = await cur.fetchone()
        return _backup_row(r) if r else None

    async def add_backup(self, channel_id: int, title: str, save_path: str,
                         chat_id: int) -> Optional[BackupRow]:
        existing = await self.get_backup_by_channel(channel_id)
        if existing and existing.status == "running":
            return None
        cur = await self.conn.execute(
            """INSERT INTO channel_backups(channel_id,title,save_path,status,chat_id,created_at,updated_at)
               VALUES(?,?,?,'running',?,?,?)""",
            (channel_id, title, save_path, chat_id, time.time(), time.time()),
        )
        await self.conn.commit()
        return await self.get_backup_by_channel(channel_id)

    async def update_backup(self, backup_id: int, **kw) -> None:
        allowed = ("status", "last_message_id", "total_done", "skipped")
        sets, params = [], []
        for k, v in kw.items():
            if k in allowed:
                sets.append(f"{k}=?"); params.append(v)
        if not sets:
            return
        sets.append("updated_at=?"); params.append(time.time()); params.append(backup_id)
        await self.conn.execute(f"UPDATE channel_backups SET {', '.join(sets)} WHERE id=?", params)
        await self.conn.commit()

    async def list_backups(self) -> List[BackupRow]:
        async with self.conn.execute("SELECT * FROM channel_backups ORDER BY id DESC") as cur:
            rows = await cur.fetchall()
        return [_backup_row(r) for r in rows]

    # ── 日志 ─────────────────────────────────────────────────────────────
    async def insert_logs(self, entries: List[LogRow]) -> None:
        if not entries:
            return
        await self.conn.executemany(
            "INSERT INTO logs(ts,level,logger,message) VALUES(?,?,?,?)",
            [(e.ts, e.level, e.logger, e.message[:2000]) for e in entries],
        )
        # FIFO 裁剪
        await self.conn.execute(
            "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT ?)",
            (MAX_LOG_ROWS,),
        )
        await self.conn.commit()

    async def recent_logs(self, limit: int = 200, level: Optional[str] = None) -> List[LogRow]:
        # SQLite 对 level 字符串无法做语义 >= 比较，故在 Python 侧算出允许集合
        params: list
        if level:
            threshold = LEVEL_ORDER.get((level or "").upper(), 0)
            allowed = [k for k, v in LEVEL_ORDER.items() if v >= threshold]
            placeholders = ",".join("?" * len(allowed))
            sql = f"SELECT * FROM logs WHERE level IN ({placeholders}) ORDER BY id DESC LIMIT ?"
            params = [*allowed, limit]
        else:
            sql = "SELECT * FROM logs ORDER BY id DESC LIMIT ?"
            params = [limit]
        async with self.conn.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [LogRow(id=r["id"], ts=r["ts"], level=r["level"],
                       logger=r["logger"], message=r["message"]) for r in rows]


# ── Row -> dataclass 转换 ──────────────────────────────────────────────────
def _task_row(r: aiosqlite.Row) -> TaskRow:
    return TaskRow(
        task_id=r["task_id"], user_id=r["user_id"], source=r["source"],
        filename=r["filename"], size=r["size"], target_dir=r["target_dir"],
        status=r["status"], method=r["method"], error=r["error"],
        chat_id=r["chat_id"], message_id=r["message_id"], channel_id=r["channel_id"],
        created_at=r["created_at"], updated_at=r["updated_at"],
    )


def _rule_row(r: aiosqlite.Row) -> ChannelRuleRow:
    return ChannelRuleRow(
        id=r["id"], channel_id=r["channel_id"], title=r["title"],
        whitelist=json.loads(r["whitelist"] or "[]"),
        blacklist=json.loads(r["blacklist"] or "[]"),
        target_dir=r["target_dir"] or "", enabled=bool(r["enabled"]),
        created_at=r["created_at"],
    )


def _account_row(r: aiosqlite.Row) -> AccountRow:
    return AccountRow(
        name=r["name"], mode=r["mode"], weight=r["weight"],
        enabled=bool(r["enabled"]), status=r["status"] or "unknown",
        last_used_at=r["last_used_at"] or 0.0, last_error=r["last_error"] or "",
        updated_at=r["updated_at"] or 0.0,
    )


def _backup_row(r: aiosqlite.Row) -> BackupRow:
    return BackupRow(
        id=r["id"], channel_id=r["channel_id"], title=r["title"] or "",
        save_path=r["save_path"] or "", status=r["status"] or "running",
        last_message_id=r["last_message_id"] or 0, total_done=r["total_done"] or 0,
        skipped=r["skipped"] or 0, chat_id=r["chat_id"] or 0,
        created_at=r["created_at"] or 0.0, updated_at=r["updated_at"] or 0.0,
    )


def _movie_row(r: aiosqlite.Row) -> MovieSubRow:
    return MovieSubRow(
        id=r["id"], tmdb_id=r["tmdb_id"] or "", movie_name=r["movie_name"] or "",
        save_path=r["save_path"] or "", downloaded=bool(r["downloaded"]),
        download_url=r["download_url"] or "", chat_id=r["chat_id"] or 0,
        created_at=r["created_at"] or 0.0,
    )


def _feed_row(r: aiosqlite.Row) -> RssFeedRow:
    return RssFeedRow(
        id=r["id"], url=r["url"], name=r["name"] or "",
        whitelist=json.loads(r["whitelist"] or "[]"),
        save_path=r["save_path"] or "", enabled=bool(r["enabled"]),
        chat_id=r["chat_id"] or 0, last_fetch=r["last_fetch"] or 0.0,
        last_error=r["last_error"] or "", created_at=r["created_at"] or 0.0,
    )


def _offline_row(r: aiosqlite.Row) -> OfflineTaskRow:
    return OfflineTaskRow(
        id=r["id"], url=r["url"], name=r["name"] or "", save_path=r["save_path"] or "",
        status=r["status"] or "pending", source=r["source"] or "manual",
        info_hash=r["info_hash"] or "", percent=r["percent"] or 0,
        retries=r["retries"] or 0, error=r["error"] or "", chat_id=r["chat_id"] or 0,
        created_at=r["created_at"] or 0.0, updated_at=r["updated_at"] or 0.0,
    )


# SQLite 不支持 >= 比较字符串 level，提供一个数值映射给 recent_logs
LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
