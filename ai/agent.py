"""AI Agent 对话循环：消息历史 + 工具调用回填，直到最终文本回复。

流程（对照社区 200 行 agent 模式，OpenAI 兼容 wire format）：
  1. 拼 messages（system + 历史 + 本轮 user）
  2. LLM -> assistant 消息；有 tool_calls 则逐个执行、结果以 role=tool 回填，回到 2
  3. 无 tool_calls（或达到轮数上限）-> 最终文本回复
会话记忆：内存 dict[chat_id] + SQLite 持久化（P2），max_history 裁剪。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ai import tools as ai_tools
from ai.llm import LLMClient, LLMError
from core.app import state

log = logging.getLogger("tg115bot.ai")

DEFAULT_SYSTEM = """你是 tg115bot —— 一个 Telegram 网盘助手，管理用户的 115 网盘。
你可以调用工具完成：离线下载、直链中转上传、分享转存、文件管理（列目录/搜索/移动）、
状态查询、RSS 订阅、电影订阅追更、频道监控规则、频道回溯备份。

行为准则：
- 用户意图能映射到工具就直接调用，不要只"介绍怎么做"。
- 路径默认用 /tg115bot 开头；不确定目录时先 list_115 查看。
- 中文回复，简洁（Telegram 场景），少用列表，关键结果加粗不必要。
- 工具返回错误时如实转述，不要编造成功。
- 涉及删除等危险操作时先向用户确认。"""

# 内存态会话（镜像 DB；DB 持久化重启不丢）
_sessions: Dict[int, List[Dict[str, Any]]] = {}
_loaded = False


async def load_sessions() -> None:
    """启动时从 DB 恢复各 chat 的会话记忆（裁剪到 max_history）。"""
    global _loaded
    if _loaded or state.db is None:
        return
    from config import get_config
    max_h = get_config().ai.max_history
    async with state.db.conn.execute(
        "SELECT DISTINCT chat_id FROM ai_sessions"
    ) as cur:
        chats = [r[0] for r in await cur.fetchall()]
    for chat_id in chats:
        rows = await state.db.ai_history(chat_id, limit=max_h)
        if rows:
            _sessions[chat_id] = rows
    _loaded = True
    if chats:
        log.info("AI 会话记忆已恢复: %d 个会话", len(chats))


def _system_prompt() -> str:
    from config import get_config
    extra = get_config().ai.system_prompt.strip()
    return f"{DEFAULT_SYSTEM}\n\n{extra}" if extra else DEFAULT_SYSTEM


def session(chat_id: int) -> List[Dict[str, Any]]:
    return _sessions.setdefault(chat_id, [])


async def reset_session(chat_id: int) -> None:
    _sessions.pop(chat_id, None)
    if state.db is not None:
        await state.db.ai_clear(chat_id)


def _trim(chat_id: int) -> None:
    from config import get_config
    max_h = get_config().ai.max_history
    msgs = _sessions.get(chat_id)
    if msgs and len(msgs) > max_h:
        _sessions[chat_id] = msgs[-max_h:]


def enabled() -> bool:
    """AI 模式总开关：配置启用 且 运行时开关开。"""
    from config import get_config
    return get_config().ai.enabled and state.ai_runtime_enabled


async def chat(chat_id: int, user_id: int, text: str,
               on_status=None) -> Optional[str]:
    """处理一条用户文本。返回最终回复文本；AI 未启用/出错返回 None 或错误提示。

    on_status: async callback(str)，用于在 TG 里提示"思考中/调用工具 xxx"。
    """
    if not enabled():
        return None
    state.ai_current_chat = chat_id
    state.ai_current_user = user_id

    hist = session(chat_id)
    hist.append({"role": "user", "content": text})
    if state.db is not None:
        await state.db.ai_append(chat_id, "user", text)
    specs = ai_tools.tool_specs()

    llm = LLMClient()
    messages: List[Dict[str, Any]] = (
        [{"role": "system", "content": _system_prompt()}] + list(hist)
    )

    from config import get_config
    max_rounds = get_config().ai.max_tool_rounds
    final_text = ""
    try:
        for round_i in range(max_rounds + 1):
            if on_status and round_i == 0:
                await on_status("🤔 思考中 …")
            msg = await llm.chat(messages, tools=specs)
            tool_calls = msg.get("tool_calls") or []

            if not tool_calls:
                final_text = (msg.get("content") or "").strip() or "（空回复）"
                break

            # assistant 的工具调用意图入历史
            hist.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": tool_calls})
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             "tool_calls": tool_calls})

            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                args = fn.get("arguments") or {}
                if on_status:
                    await on_status(f"🔧 调用 {name} …")
                log.info("AI[%s] 工具调用 #%d: %s %s", chat_id, round_i, name, str(args)[:150])
                result = await ai_tools.dispatch(name, args)
                entry = {"role": "tool", "tool_call_id": tc.get("id", ""),
                         "name": name, "content": result}
                hist.append(entry)
                messages.append(entry)
        else:
            final_text = "⚠️ 工具调用轮数达到上限，已停止。以上操作可能部分完成，可用 /status 查看。"
    except LLMError as e:
        log.warning("AI 对话失败: %r", e)
        # 回滚本轮 user 消息，避免污染历史
        if hist and hist[-1].get("role") == "user":
            hist.pop()
        return f"❌ AI 服务出错: {e}"
    except Exception as e:  # noqa: BLE001
        log.exception("AI agent 异常")
        return f"❌ AI 内部错误: {e}"

    hist.append({"role": "assistant", "content": final_text})
    if state.db is not None:
        await state.db.ai_append(chat_id, "assistant", final_text)
    _trim(chat_id)
    return final_text
