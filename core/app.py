"""全局服务容器（模块级单例）。

main.py 在启动时把所有单例（配置、Pyrogram 客户端、115 客户端、工作区、队列）
写入 ``state``，handlers 与 pipeline 直接读取，避免层层传参。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class _AppState:
    config: Any = None
    pyro_bot: Any = None
    pyro_user: Any = None          # 可选；存在时用于 >20MB 下载与更高额度
    cloud: Any = None              # 兼容：主账号 Cloud115Client（/auth 用）
    accounts: Any = None           # AccountManager（多账号轮转）
    workspace: Any = None          # Workspace
    queue: Any = None              # TaskQueue
    db: Any = None                 # persistence.Database
    monitor: Any = None            # bot/channel_monitor.ChannelMonitor
    web: Any = None                # uvicorn server（可选）
    user_target_dirs: Dict[int, str] = {}   # 内存态：user_id -> 115 目标目录
    low_disk_alerted: bool = False          # 磁盘水位告警去重（避免刷屏）
    # 取消注册表（内存态）
    active_tasks: Dict[str, Any] = {}            # task_id -> Task
    user_task_order: Dict[int, list] = {}        # user_id -> [task_id,...]（入队顺序）
    task_progress: Dict[str, dict] = {}          # task_id -> 实时快照（供 Web 台展示）
    pending_confirm: Dict[str, float] = {}       # 危险操作二次确认（如 /rm），key -> 时间戳
    # AI 模式运行时（ai/agent.py 维护）
    ai_current_chat: int = 0                     # 当前 AI 对话 chat_id（工具里发消息用）
    ai_current_user: int = 0                     # 当前 AI 对话 user_id（set_target_dir 用）
    ai_runtime_enabled: bool = True              # /ai on|off 临时开关（配置启用后的运行时门）

    @classmethod
    def download_client(cls) -> Any:
        """优先用 user session（额度高、可下大文件），否则退回 bot。"""
        return cls.pyro_user or cls.pyro_bot

    @classmethod
    def register_task(cls, task) -> None:
        cls.active_tasks[task.task_id] = task
        cls.user_task_order.setdefault(task.user_id, []).append(task.task_id)

    @classmethod
    def unregister_task(cls, task) -> None:
        cls.active_tasks.pop(task.task_id, None)
        order = cls.user_task_order.get(task.user_id)
        if order and task.task_id in order:
            order.remove(task.task_id)

    @classmethod
    def cancel_latest(cls, user_id: int) -> bool:
        """取消该用户最近一个进行中任务，返回是否找到。"""
        order = cls.user_task_order.get(user_id, [])
        while order:
            t = cls.active_tasks.get(order[-1])
            if t is not None:
                t.cancel_event.set()
                return True
            order.pop()
        return False


state = _AppState
