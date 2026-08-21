"""配置加载：config.yaml + 环境变量覆盖（TG115BOT__section__key）。

环境变量用双下划线分层，例如：
    TG115BOT__TELEGRAM__BOT_TOKEN
    TG115BOT__UPLOAD__TARGET_DIR
    TG115BOT__ACCOUNTS__0__WEIGHT   （数组用下标）
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"
ENV_PREFIX = "TG115BOT__"


class TelegramCfg(BaseModel):
    api_id: int
    api_hash: str
    bot_token: str
    user_session: str = ""          # 文件路径或 session 字符串；空=只用 bot
    allowed_users: List[int] = Field(default_factory=list)
    # 代理（国内服务器访问 TG 必需；115/OSS 不走代理，国内直连更快）
    # 形如 "socks5://127.0.0.1:7891" 或 "http://127.0.0.1:7890"；空=直连
    proxy: str = ""


class UploadCfg(BaseModel):
    workers: int = 8
    chunk_size: int = 1024 * 1024
    oss_concurrency: int = 8
    target_dir: str = "/tg115bot"
    delete_after_upload: bool = True


class AccountCfg(BaseModel):
    name: str = "main"
    mode: str = "open"              # 仅 "open"（开放平台扫码授权）；cookie 模式已移除
    app_id: str = ""                # 留空用公共测试 AppID；有自己开放平台应用的填这里
    weight: int = 1


class StorageCfg(BaseModel):
    work_dir: str = "downloads"
    min_free_gb: int = 5


class Rate115Cfg(BaseModel):
    min_interval_sec: float = 0.3
    backoff_base_sec: float = 2.0
    max_retries: int = 5


class QueueCfg(BaseModel):
    concurrency: int = 2          # 同时处理的任务数；多任务并发时注意带宽/磁盘


class WebCfg(BaseModel):
    enable: bool = False
    host: str = "0.0.0.0"
    port: int = 8080
    username: str = "admin"
    password: str = "changeme"


class OrganizeCfg(BaseModel):
    rename_template: str = "{filename}"
    classify_by_ext: bool = False


class ChannelMonitorCfg(BaseModel):
    enabled: bool = False
    default_target_dir: str = ""        # 命中规则但规则未指定目录时的兜底；空=用 upload.target_dir
    notify_chat_id: int = 0             # 进度/结果通知发到这里；0=回复到频道（需 bot 为频道管理员）


class LoggingCfg(BaseModel):
    level: str = "INFO"
    file: str = "logs/tg115bot.log"
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5
    db_buffer: int = 200                # 内存缓冲条数；>0 启用 DB 日志落盘


class ShareCfg(BaseModel):
    # 分享链接转存（可选）。⚠️ 转存接口仅存在于 webapi（Cookie 鉴权），
    # 开放平台无对应端点；不配置则"发分享链接转存"功能停用，不影响其他功能。
    cookies: str = ""
    target_dir: str = "/tg115bot/shared"   # 转存默认目录


class MovieSubCfg(BaseModel):
    # nullbr API 授权（https://nullbr.online/api 申请）；留空则电影订阅功能停用
    app_id: str = ""
    api_key: str = ""
    target_dir: str = "/tg115bot/movies"   # 电影订阅默认保存目录
    zh_sub_only: bool = False              # true 时只选带中字的资源


class SecurityCfg(BaseModel):
    # 凭据加密密钥口令；留空则读 TG115BOT_SECRET_KEY 环境变量；仍空则用开发态默认（仅本机）
    secret_key: str = ""


class AppConfig(BaseModel):
    telegram: TelegramCfg
    upload: UploadCfg = Field(default_factory=UploadCfg)
    accounts: List[AccountCfg] = Field(default_factory=list)
    storage: StorageCfg = Field(default_factory=StorageCfg)
    rate115: Rate115Cfg = Field(default_factory=Rate115Cfg)
    queue: QueueCfg = Field(default_factory=QueueCfg)
    web: WebCfg = Field(default_factory=WebCfg)
    organize: OrganizeCfg = Field(default_factory=OrganizeCfg)
    channel_monitor: ChannelMonitorCfg = Field(default_factory=ChannelMonitorCfg)
    logging: LoggingCfg = Field(default_factory=LoggingCfg)
    security: SecurityCfg = Field(default_factory=SecurityCfg)
    movie_sub: MovieSubCfg = Field(default_factory=MovieSubCfg)
    share: ShareCfg = Field(default_factory=ShareCfg)

    @property
    def primary_account(self) -> AccountCfg:
        if not self.accounts:
            raise RuntimeError("config.accounts 为空，请至少配置一个 115 账号")
        return self.accounts[0]

    @property
    def work_dir_abs(self) -> Path:
        p = Path(self.storage.work_dir)
        return p if p.is_absolute() else (ROOT / p)

    @property
    def session_dir(self) -> Path:
        d = ROOT / "config"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def db_path(self) -> Path:
        d = ROOT / "db"
        d.mkdir(parents=True, exist_ok=True)
        return d / "tg115bot.db"

    @property
    def notify_chat_id(self) -> int:
        """频道监控通知目标：显式配置 > 白名单第一个用户 > 0。"""
        cm = self.channel_monitor.notify_chat_id
        if cm:
            return cm
        if self.telegram.allowed_users:
            return self.telegram.allowed_users[0]
        return 0


# ── 环境变量覆盖 ──────────────────────────────────────────────────────────
def _apply_env_overrides(raw: dict) -> dict:
    for key, val in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = key[len(ENV_PREFIX):].lower().split("__")
        node = raw
        for i, part in enumerate(path):
            last = i == len(path) - 1
            if last:
                node[part] = _coerce(val)
            else:
                # 支持数组下标（纯数字）
                if part.isdigit() and isinstance(node, list):
                    idx = int(part)
                    while len(node) <= idx:
                        node.append({})
                    if not isinstance(node[idx], dict):
                        node[idx] = {}
                    node = node[idx]
                else:
                    node = node.setdefault(part, {})
    return raw


def _coerce(val: str):
    if val.lower() in ("true", "false"):
        return val.lower() == "true"
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


_config: Optional[AppConfig] = None


def load_config(path: Optional[Path] = None) -> AppConfig:
    global _config
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {p}\n请复制 config.yaml.example 为 config.yaml 并填写"
        )
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    raw = _apply_env_overrides(raw)
    cfg = AppConfig.model_validate(raw)
    _config = cfg
    return cfg


def get_config() -> AppConfig:
    if _config is None:
        return load_config()
    return _config
