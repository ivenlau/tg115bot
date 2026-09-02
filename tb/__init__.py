"""tg115bot 统一命令行入口（python -m tb）。

安装目录即仓库根（config.yaml/downloads/logs 与代码同址），与既有部署习惯一致；
本包负责把散落的 scripts/* 收敛为单一 `tb` 命令（CLI 子命令 + 交互 TUI）。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 仓库根（= 安装目录）入 sys.path：tb 可从任意 cwd 启动，平铺的
# config/cloud115/core/scripts 模块才能导入（scripts/manual.py 同款做法）
INSTALL_DIR = Path(__file__).resolve().parent.parent
if str(INSTALL_DIR) not in sys.path:
    sys.path.insert(0, str(INSTALL_DIR))

VERSION = "1.0.0"
