"""FloodWait 识别与重试测试（flood_wait_secs / with_flood_wait）。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import utils.rate as rate  # noqa: E402
from utils.rate import flood_wait_secs, with_flood_wait  # noqa: E402


def _make_floodwait(secs: int) -> Exception:
    """构造一个名为 FloodWait、带 value 属性的异常（duck-type 路径可识别）。"""
    return type("FloodWait", (Exception,), {"value": secs})()


def test_flood_wait_secs_recognizes_value_attr():
    assert flood_wait_secs(_make_floodwait(42)) == 42.0
    assert flood_wait_secs(_make_floodwait(0)) == 1.0   # 0 -> 兜底 1s


def test_flood_wait_secs_non_flood_returns_none():
    assert flood_wait_secs(ValueError("nope")) is None
    assert flood_wait_secs(RuntimeError()) is None


def test_with_flood_wait_retries_then_succeeds():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _make_floodwait(0)
        return "ok"

    # 直接用真实 sleep，value=0 -> 等 0.5s，可接受
    result = asyncio.run(with_flood_wait(factory, max_retries=3))
    assert result == "ok"
    assert calls["n"] == 2


def test_with_flood_wait_non_flood_propagates():
    async def factory():
        raise ValueError("boom")

    try:
        asyncio.run(with_flood_wait(factory, max_retries=3))
    except ValueError:
        return
    raise AssertionError("非 FloodWait 异常应直接抛出")


def test_with_flood_wait_gives_up_after_max_retries():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise _make_floodwait(0)

    try:
        asyncio.run(with_flood_wait(factory, max_retries=2))
    except Exception as e:  # noqa: BLE001
        assert type(e).__name__ == "FloodWait"
        assert calls["n"] == 3   # 初次 + 2 次重试
        return
    raise AssertionError("超过重试上限应抛出")


def test_with_backoff_no_retry_tuple():
    """no_retry 声明的异常类型必须直接抛出，不得重试（TaskCancelled 场景）。"""
    import asyncio
    from core.queue import TaskCancelled
    import utils.rate as rate_mod

    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise TaskCancelled()

    try:
        asyncio.run(rate_mod.with_backoff(factory, max_retries=5, no_retry=(TaskCancelled,)))
    except TaskCancelled:
        assert calls["n"] == 1, f"不应重试，实际调了 {calls['n']} 次"
        print("  ok: test_with_backoff_no_retry_tuple")
        return
    raise AssertionError("TaskCancelled 应被抛出")


def test_with_backoff_retries_normal_error():
    """普通异常仍按退避重试。"""
    import asyncio
    import utils.rate as rate_mod

    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("transient")
        return "ok"

    # 缩短退避：patch sleep
    orig = rate_mod.asyncio.sleep
    async def _fast_sleep(_s):
        await orig(0)
    rate_mod.asyncio.sleep = _fast_sleep
    try:
        r = asyncio.run(rate_mod.with_backoff(factory, base=0.01, max_retries=3))
    finally:
        rate_mod.asyncio.sleep = orig
    assert r == "ok" and calls["n"] == 2
    print("  ok: test_with_backoff_retries_normal_error")


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print(f"  ok: {fn.__name__}")
    print("test_floodwait: ALL PASS")
