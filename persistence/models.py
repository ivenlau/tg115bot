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
    progress: int = -1              # 0-100（downloading/uploading 实时节流落库；-1=未知/不适用）
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


# 离线下载任务状态
OFFLINE_PENDING = "pending"        # 已提交 115
OFFLINE_RUNNING = "running"        # 下载中
OFFLINE_DONE = "done"              # 完成
OFFLINE_FAILED = "failed"          # 失败
OFFLINE_RETRYING = "retrying"      # 失败待重试


@dataclass
class OfflineTaskRow:
    id: int = 0
    url: str = ""                   # magnet/ed2k/http
    name: str = ""                  # 115 返回的资源名
    save_path: str = ""            # 115 目标目录
    status: str = OFFLINE_PENDING
    source: str = "manual"         # manual | rss | ai
    info_hash: str = ""
    percent: int = 0
    retries: int = 0
    error: str = ""
    chat_id: int = 0               # 完成通知发往
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class RssFeedRow:
    id: int = 0
    url: str = ""                    # RSS/Atom 源地址
    name: str = ""                   # 备注名
    whitelist: List[str] = field(default_factory=list)   # 标题关键词（空=全部）
    save_path: str = ""              # 离线保存目录（空=upload.target_dir）
    enabled: bool = True
    chat_id: int = 0                 # 通知发往
    last_fetch: float = 0.0
    last_error: str = ""
    created_at: float = 0.0


@dataclass
class SeenLinkRow:
    url: str = ""
    title: str = ""
    created_at: float = 0.0


# 频道备份状态
BACKUP_RUNNING = "running"
BACKUP_PAUSED = "paused"
BACKUP_DONE = "done"


@dataclass
class BackupRow:
    id: int = 0
    channel_id: int = 0
    title: str = ""
    save_path: str = ""
    status: str = BACKUP_RUNNING
    last_message_id: int = 0      # 断点：已处理到的消息 id（从新到旧递减）
    total_done: int = 0           # 已入队媒体数
    skipped: int = 0              # 跳过（无媒体/规则不命中）数
    chat_id: int = 0              # 进度通知发往
    created_at: float = 0.0
    updated_at: float = 0.0
