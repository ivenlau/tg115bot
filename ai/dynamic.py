"""AI 动态工具：AI 编写新工具（受限沙箱执行），TG 确认后注册。

流程：
  1. LLM 调 create_dynamic_tool(name, description, parameters, code, allow_net)
  2. static_check 通过 -> 存 DB（enabled=0 待确认）-> 向用户发内联确认按钮
  3. 用户点确认 -> enabled=1 -> 注册进 ai.tools.TOOLS，LLM 立即可调用
  4. /aitools 查看与删除

call_tool 桥：动态工具内可组合调用**同步可算**的其它工具；异步工具经
预取缓存不现实，故桥只对同步函数工具开放——当前所有内置工具都是 async，
桥内用 asyncio.run 不行（线程里没有 loop）——改为：call_tool 桥在线程里
创建新事件循环执行 async 工具（to_thread 里 run() 可行，但循环嵌套警告）。
简化与安全取舍：call_tool 桥仅支持动态工具之间的互调（同步），不调内置
async 工具；组合需求由 LLM 在对话层完成（它本来就能多轮调用）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict

from ai.sandbox import SandboxViolation, static_check
from core.app import state

log = logging.getLogger("tg115bot.ai.dynamic")

# 内存注册表：name -> {"id":.., "code":.., "allow_net":..}
_dynamic: Dict[str, Dict[str, Any]] = {}


async def load_dynamic_tools() -> None:
    """启动时把 DB 中已启用的动态工具注册进工具表。"""
    if state.db is None:
        return
    from ai import tools as ai_tools
    rows = await state.db.ai_tool_list(only_enabled=True)
    for row in rows:
        try:
            _register(row, ai_tools)
        except Exception as e:  # noqa: BLE001 -- 单个坏工具不阻断
            log.warning("动态工具 %s 注册失败: %r", row["name"], e)
    if rows:
        log.info("动态工具已加载: %d 个", len(_dynamic))


def _register(row: Dict[str, Any], ai_tools) -> None:
    name = row["name"]
    params = json.loads(row["parameters"] or '{"type":"object","properties":{}}')
    code = row["code"]

    # 判定 allow_net：存库时拼在 parameters 的 _allow_net 标记（不用单独列）
    allow_net = bool(params.pop("_allow_net", False))

    async def _fn(a: Dict[str, Any], _code=code, _net=allow_net) -> str:
        from ai.sandbox import run_sandbox_async
        return await run_sandbox_async(_code, a, allow_net=_net)

    ai_tools.TOOLS.append({"name": name, "description": row["description"] or "",
                           "parameters": params, "fn": _fn})
    _dynamic[name] = {"id": row["id"], "code": code, "allow_net": allow_net}
    # 去重保护（重启后重复 load）
    seen = set()
    deduped = []
    for t in ai_tools.TOOLS:
        if t["name"] == name and t["name"] in seen:
            continue
        seen.add(t["name"])
        deduped.append(t)
    ai_tools.TOOLS[:] = deduped


async def request_create(name: str, description: str,
                         parameters: Dict[str, Any], code: str,
                         allow_net: bool = False) -> str:
    """静态检查 + 落库（待确认）+ 发确认按钮。返回给 LLM 的说明文本。"""
    if state.db is None:
        return "持久化未启用，动态工具不可用"
    if name in _dynamic:
        return f"工具 {name} 已存在（/aitools 查看后可删除再建）"
    try:
        static_check(code)
    except SandboxViolation as e:
        return f"❌ 代码安全检查未通过: {e}。请改写（禁止 import/open/eval/dunder/裸 while True 等）"

    # allow_net 标记拼进 parameters 存库
    params = dict(parameters or {})
    params["_allow_net"] = allow_net
    row_id = await state.db.ai_tool_add(name, description,
                                        json.dumps(params, ensure_ascii=False), code)
    if row_id is None:
        return f"工具名 {name} 冲突"

    # TG 确认按钮
    if state.pyro_bot is not None and state.ai_current_chat:
        from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        net_note = "\n🌐 允许访问网络（仅 http_get）" if allow_net else ""
        try:
            await state.pyro_bot.send_message(
                state.ai_current_chat,
                f"🛠 AI 请求创建新工具\n\n**{name}** — {description}\n"
                f"```python\n{code[:600]}\n```{net_note}\n\n确认启用？",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ 启用", callback_data=f"aitool|{row_id}|1"),
                    InlineKeyboardButton("拒绝", callback_data=f"aitool|{row_id}|0"),
                ]]),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("动态工具确认消息发送失败: %r", e)
    return (f"已创建工具 {name}（ID {row_id}），等待用户在 TG 点确认后生效。"
            f"请告知用户查看确认按钮。")


async def confirm(row_id: int, accept: bool) -> str:
    """确认回调：启用并注册，或删除。"""
    if state.db is None:
        return "持久化未启用"
    rows = await state.db.ai_tool_list()
    row = next((r for r in rows if r["id"] == row_id), None)
    if row is None:
        return "工具不存在"
    if not accept:
        await state.db.ai_tool_delete(row_id)
        return f"已拒绝并删除 {row['name']}"
    from ai import tools as ai_tools
    _register(row, ai_tools)
    # enabled 已是 1（创建时默认）；确认只是注册进运行时
    return f"✅ 动态工具 {row['name']} 已启用"


async def delete_by_name(name: str) -> str:
    from ai import tools as ai_tools
    removed_db = False
    if state.db is not None:
        rows = await state.db.ai_tool_list()
        row = next((r for r in rows if r["name"] == name), None)
        if row is not None:
            await state.db.ai_tool_delete(row["id"])
            removed_db = True
    _dynamic.pop(name, None)
    before = len(ai_tools.TOOLS)
    ai_tools.TOOLS[:] = [t for t in ai_tools.TOOLS if t["name"] != name]
    if not removed_db and before == len(ai_tools.TOOLS):
        return f"无此工具: {name}"
    return f"✅ 已删除动态工具 {name}"


# 注册 create/delete 两个元工具（内置，非动态）
from ai.tools import schema, tool, _s  # noqa: E402


@tool("create_dynamic_tool",
      "创建一个新的自定义工具（Python 函数体，受限沙箱执行）。当现有工具无法满足需求时用。"
      "代码规则：是一个函数体（不要 def 行），入参 args(dict)，return 字符串结果；"
      "可用：json_loads/json_dumps/re_search/re_findall/re_sub/普通表达式与控制流；"
      "禁止：import/open/eval/exec/dunder/裸 while True。创建后需用户在 TG 确认才生效。",
      schema({"name": _s("工具名，snake_case 英文"),
              "description": _s("工具用途描述（给 LLM 看）"),
              "parameters": {"type": "object", "description": "JSON schema：type=object + properties"},
              "code": _s("Python 函数体（无 def 行），args 为参数 dict，return 结果"),
              "allow_net": {"type": "boolean", "description": "是否需要 http_get 网络访问（默认 false）"}},
             ["name", "description", "code"]))
async def _create_dynamic(a):
    return await request_create(a["name"], a.get("description", ""),
                                a.get("parameters") or {}, a["code"],
                                bool(a.get("allow_net")))


@tool("list_dynamic_tools", "查看已创建的动态工具。无参数。", schema({}))
async def _list_dynamic(a):
    if state.db is None:
        return "持久化未启用"
    rows = await state.db.ai_tool_list()
    if not rows:
        return "暂无动态工具"
    return "\n".join(f"{'✅' if r['enabled'] else '⏳待确认'} {r['name']}: {(r['description'] or '')[:60]}"
                     for r in rows)


@tool("delete_dynamic_tool", "删除动态工具。", schema({"name": _s("工具名")}, ["name"]))
async def _del_dynamic(a):
    return await delete_by_name(a["name"])
