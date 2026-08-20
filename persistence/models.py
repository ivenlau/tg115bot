"""持久化层数据模型（DB 行的轻量表示）。

仅作传递/展示用，不与 ORM 耦合。DB 读写见 ``db.py``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


# 任务状态枚举（字符串常量）
STATUS_QUEUED = "queued"
STATUS_DOWNLOADING = "downloading"
STATUS_UPLOADING = "uploading"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# 任务来源
SOURCE_MANUAL = "manual"
SOURCE_CHANNEL = "channel"


@dataclass
class TaskRow:
    task_id: str
    user_id: int
    source: str = SOURCE_MANUAL
    filename: str = ""
    size: int = 0
    target_dir: str = ""
    status: str = STATUS_QUEUED
    method: str = ""                # 秒传 | oss | fs.upload
    error: str = ""
    chat_id: int = 0
    message_id: int = 0
    channel_id: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class ChannelRuleRow:
    id: int = 0
    channel_id: int = 0
    title: str = ""
    whitelist: List[str] = field(default_factory=list)
    blacklist: List[str] = field(default_factory=list)
    target_dir: str = ""
    enabled: bool = True
    created_at: float = 0.0


@dataclass
class AccountRow:
    name: str
    mode: str = "open"
    weight: int = 1
    enabled: bool = True
    status: str = "unknown"         # ok | cooldown | failed | unknown
    last_used_at: float = 0.0
    last_error: str = ""
    updated_at: float = 0.0


@dataclass
class LogRow:
    id: int = 0
    ts: float = 0.0
    level: str = "INFO"
    logger: str = ""
    message: str = ""
