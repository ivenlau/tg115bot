"""115 多账号管理 + 加权轮转 + 故障冷却。

设计：
  - 每个账号一个 Cloud115Client（开放平台 + 独立 RateLimiter，账号间风控互不影响）。
  - 账号状态三态：ok（token 有效）/ unauthorized（无 token，等 /auth 扫码）/ failed（失效或冷却中）。
  - 取号只在 ok 账号中加权轮转；全部不可用时抛 RuntimeError（pipeline 会向用户报错）。
  - 无任何已授权账号时也能启动 bot（等待 /auth），只是任务会失败提示先授权。

选择算法 ``select_weighted`` 为纯函数，便于单测（见 tests/）。
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List

from cloud115.client import Cloud115Client, Cloud115Error
from config import AccountCfg
from utils.rate import RateLimiter

log = logging.getLogger(__name__)

# 故障冷却：逐次翻倍，封顶 5 分钟
_COOLDOWN_STEPS = [15, 30, 60, 120, 300]


def select_weighted(
    order: List[str],
    weights: Dict[str, int],
    available: List[str],
    rr_index: int,
):
    """在 available 中按权重加权轮转选一个账号名。空返回 None。"""
    if not available:
        return None
    av = [n for n in available if n in weights]
    if not av:
        av = list(available)
    wheel: List[str] = []
    for n in order:
        if n in av:
            wheel.extend([n] * max(1, weights.get(n, 1)))
    if not wheel:
        return av[0]
    return wheel[rr_index % len(wheel)]


class AccountManager:
    """多账号容器。``get()`` 返回一个 Cloud115Client 供本次任务使用。"""

    def __init__(self, configs: List[AccountCfg], session_dir, rate_min_interval: float, db=None):
        if not configs:
            raise RuntimeError("accounts 配置为空")
        self._configs: List[AccountCfg] = list(configs)
        self._session_dir = session_dir
        self._rate_min = rate_min_interval
        self._db = db
        self._order = [c.name for c in self._configs]
        self._clients: Dict[str, Cloud115Client] = {}
        self._authorized: Dict[str, bool] = {}          # name -> 是否已授权且探活通过
        self._weights: Dict[str, int] = {c.name: max(1, c.weight) for c in self._configs}
        self._cooldown_until: Dict[str, float] = {}
        self._cooldown_step: Dict[str, int] = {}
        self._rr = 0

    # ── 生命周期 ──────────────────────────────────────────────────────────
    async def init(self) -> None:
        if self._db is not None:
            await self._db.sync_accounts(self._configs)
        for c in self._configs:
            rate = RateLimiter(self._rate_min)
            client = Cloud115Client(c, self._session_dir, rate)
            try:
                await client.init()
                self._clients[c.name] = client
                if not client.api.has_token():
                    log.warning("账号 %s 未授权（无 token），启动后请 /auth 扫码", c.name)
                    self._authorized[c.name] = False
                    if self._db is not None:
                        await self._db.update_account(c.name, status="unauthorized", last_error="未授权")
                elif await client.ensure_login():
                    log.info("账号 %s 就绪 ✅", c.name)
                    self._authorized[c.name] = True
                    if self._db is not None:
                        await self._db.update_account(c.name, status="ok")
                else:
                    log.warning("账号 %s token 失效 ❌（/auth 重新扫码可恢复）", c.name)
                    self._authorized[c.name] = False
                    if self._db is not None:
                        await self._db.update_account(c.name, status="failed", last_error="token 失效")
            except Exception as e:  # noqa: BLE001
                log.warning("账号 %s 初始化异常: %r", c.name, e)
                if self._db is not None:
                    await self._db.update_account(c.name, status="failed", last_error=repr(e))
        # 允许零授权启动（等 /auth）；零 client（构造都失败）才报错
        if not self._clients:
            raise RuntimeError("没有任何 115 账号客户端成功创建")

    async def close(self) -> None:
        for client in self._clients.values():
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def primary(self) -> Cloud115Client:
        for name in self._order:
            if name in self._clients:
                return self._clients[name]
        return next(iter(self._clients.values()))

    def get_client(self, name: str):
        return self._clients.get(name)

    def names(self) -> List[str]:
        return list(self._clients.keys())

    # ── 取号 ─────────────────────────────────────────────────────────────
    def _available(self) -> List[str]:
        now = time.monotonic()
        return [n for n in self._order
                if self._authorized.get(n) and self._cooldown_until.get(n, 0) <= now]

    async def get(self) -> Cloud115Client:
        """取一个可用 client；无可用账号抛 RuntimeError。"""
        avail = self._available()
        name = select_weighted(self._order, self._weights, avail, self._rr)
        if name is None:
            hint = "无已授权账号，请先 /auth 扫码" if not any(self._authorized.values()) \
                else "所有账号冷却中/失效，请稍后重试或 /auth"
            raise RuntimeError(hint)
        self._rr = (self._rr + 1) % 997
        if self._db is not None:
            await self._db.update_account(name, touch=True)
        return self._clients[name]

    # ── 故障/恢复反馈 ────────────────────────────────────────────────────
    def report_failure(self, name: str, error: str = "") -> None:
        if name not in self._clients:
            return
        step = min(self._cooldown_step.get(name, 0), len(_COOLDOWN_STEPS) - 1)
        secs = _COOLDOWN_STEPS[step]
        self._cooldown_until[name] = time.monotonic() + secs
        self._cooldown_step[name] = step + 1
        log.warning("账号 %s 失败，冷却 %ds（%r）", name, secs, error)
        if self._db is not None:
            import asyncio
            asyncio.ensure_future(
                self._db.update_account(name, status="cooldown", last_error=error)
            )

    def report_success(self, name: str) -> None:
        self._cooldown_step.pop(name, None)
        self._cooldown_until.pop(name, None)
        if self._db is not None:
            import asyncio
            asyncio.ensure_future(self._db.update_account(name, status="ok", last_error=""))

    def mark_authorized(self, name: str) -> None:
        """扫码授权成功后调用。"""
        self._authorized[name] = True
        self._cooldown_until.pop(name, None)
        self._cooldown_step.pop(name, None)
        self._rr = 0
        if self._db is not None:
            import asyncio
            asyncio.ensure_future(self._db.update_account(name, status="ok", last_error=""))

    # ── 展示 ─────────────────────────────────────────────────────────────
    def status_list(self) -> List[dict]:
        now = time.monotonic()
        out = []
        for c in self._configs:
            name = c.name
            cooling = self._cooldown_until.get(name, 0) > now
            if cooling:
                status = "cooldown"
            elif name not in self._clients:
                status = "failed"
            elif self._authorized.get(name):
                status = "ok"
            else:
                status = "unauthorized"
            out.append({
                "name": name, "mode": "open", "weight": c.weight,
                "ready": name in self._clients,
                "status": status,
                "cooldown_sec": max(0, int(self._cooldown_until.get(name, 0) - now)) if cooling else 0,
            })
        return out

    async def check_all(self) -> Dict[str, bool]:
        """逐个探活（/auth 与定时健康检查用）。"""
        result: Dict[str, bool] = {}
        for name, client in self._clients.items():
            ok = await client.ensure_login()
            result[name] = ok
            self._authorized[name] = ok
            if self._db is not None:
                await self._db.update_account(
                    name, status="ok" if ok else "failed",
                    last_error="" if ok else "探活失败/token 失效",
                )
        return result
