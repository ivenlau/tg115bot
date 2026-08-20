"""多账号加权轮转选择算法测试（纯函数 select_weighted）。

cloud115.account 顶层 import aiohttp（沙箱未装），此处先注入桩。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _install_aiohttp_stub() -> None:
    if "aiohttp" in sys.modules:
        return
    mod = types.ModuleType("aiohttp")

    class _Dummy:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    mod.ClientSession = _Dummy
    mod.ClientTimeout = lambda **k: None
    sys.modules["aiohttp"] = mod


_install_aiohttp_stub()

from cloud115.account import select_weighted  # noqa: E402


def test_single_account():
    assert select_weighted(["a"], {"a": 1}, ["a"], 0) == "a"
    assert select_weighted(["a"], {"a": 1}, ["a"], 7) == "a"


def test_empty_available_returns_none():
    assert select_weighted(["a", "b"], {"a": 1, "b": 1}, [], 0) is None


def test_skip_unavailable():
    # 只有 b 可用 -> 必选 b，无论 rr_index
    for i in range(5):
        assert select_weighted(["a", "b", "c"], {"a": 1, "b": 1, "c": 1}, ["b"], i) == "b"


def test_round_robin_equal_weights():
    order = ["a", "b"]
    weights = {"a": 1, "b": 1}
    avail = ["a", "b"]
    # 轮转序列 = [a, b]；随 rr_index 推进交替
    picks = [select_weighted(order, weights, avail, i) for i in range(6)]
    assert picks == ["a", "b", "a", "b", "a", "b"]


def test_weight_bias():
    order = ["a", "b"]
    weights = {"a": 3, "b": 1}
    avail = ["a", "b"]
    # 轮转序列 = [a, a, a, b]；a 出现 3 次、b 1 次
    picks = [select_weighted(order, weights, avail, i) for i in range(4)]
    assert picks.count("a") == 3
    assert picks.count("b") == 1
    assert picks[3] == "b"


def test_weight_zero_treated_as_one():
    order = ["a", "b"]
    weights = {"a": 0, "b": 1}   # a 权重 0 -> 至少出现 1 次
    avail = ["a", "b"]
    picks = [select_weighted(order, weights, avail, i) for i in range(2)]
    assert "a" in picks


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print(f"  ok: {fn.__name__}")
    print("test_account_rotation: ALL PASS")
