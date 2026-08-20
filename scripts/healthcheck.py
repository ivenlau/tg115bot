"""容器健康检查：Web 启用时探测 /health，未启用则视进程存活即为健康。

退出码 0 = 健康，1 = 不健康。供 Docker HEALTHCHECK 调用。
"""
from __future__ import annotations

import sys
import urllib.request


def main() -> int:
    try:
        from config import get_config
        cfg = get_config()
    except Exception:  # noqa: BLE001 -- 配置读不到直接判不健康
        return 1
    if not cfg.web.enable:
        return 0
    url = f"http://127.0.0.1:{cfg.web.port}/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return 0 if r.status == 200 else 1
    except Exception:  # noqa: BLE001
        return 1


if __name__ == "__main__":
    sys.exit(main())
