"""运行全部测试（不依赖 pytest）。

用法：python3 tests/run_all.py
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODULES = [
    "tests.test_organize",
    "tests.test_channel_match",
    "tests.test_account_rotation",
    "tests.test_crypto",
    "tests.test_config",
    "tests.test_floodwait",
    "tests.test_db",
    "tests.test_oss_protocol",
    "tests.test_manual_tools",
    "tests.test_tb",
    "tests.test_workspace_finalize",
    "tests.test_log_rotation",
    "tests.test_tui",
]


def main() -> int:
    failures = 0
    for mod_name in MODULES:
        print(f"\n== {mod_name} ==")
        try:
            mod = importlib.import_module(mod_name)
            mod.__name__  # 触发加载
            # 每个测试模块自带 __main__ 运行逻辑：收集 test_ 函数并执行
            fns = [(k, v) for k, v in sorted(vars(mod).items()) if k.startswith("test_") and callable(v)]
            for name, fn in fns:
                setup = getattr(mod, "setup_function", None)
                try:
                    if setup:
                        setup(fn)
                    fn()
                    print(f"  ok: {name}")
                except Exception as e:  # noqa: BLE001
                    failures += 1
                    print(f"  FAIL: {name}: {e!r}")
        except Exception as e:  # noqa: BLE001 -- 导入级失败
            failures += 1
            print(f"  IMPORT FAIL: {e!r}")
    print(f"\n{'='*40}\n结果: {'全部通过 ✅' if not failures else f'{failures} 个失败 ❌'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
