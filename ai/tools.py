"""AI 工具集：把现有 bot 功能封装为 LLM function-calling 工具。

每个工具 = (name, description, JSON-schema parameters, async fn(args)->str)。
与命令模式等价服务；安全取舍：
  - delete_115 默认不注册（防 AI 误删）；需要时 config.ai.system_prompt 提示也不行——硬排除
  - 工具执行异常一律捕获并作为错误文本回给 LLM（让其自行调整），不打断循环
  - 所有调用写日志（audit）
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core.app import state

log = logging.getLogger("tg115bot.ai.tools")

ToolFn = Callable[[Dict[str, Any]], Awaitable[str]]


def tool(name: str, description: str, parameters: Dict[str, Any]):
    """注册装饰器：把 async fn(dict) -> str 变成工具描述。"""
    def deco(fn: ToolFn):
        TOOLS.append({"name": name, "description": description,
                      "parameters": parameters, "fn": fn})
        return fn
    return deco


TOOLS: List[Dict[str, Any]] = []


def schema(props: Dict[str, Any], required: List[str] = ()) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


def _s(desc: str, **kw) -> Dict[str, Any]:
    d = {"type": "string", "description": desc}
    d.update(kw)
    return d


async def dispatch(name: str, args: Dict[str, Any]) -> str:
    """执行一个工具；异常转错误文本。供 agent 循环调用。"""
    for t in TOOLS:
        if t["name"] == name:
            try:
                result = await t["fn"](args or {})
                out = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                log.info("AI 工具 %s(%s) -> %s", name,
                         str(args)[:120], out[:120])
                return out[:4000]
            except Exception as e:  # noqa: BLE001 -- 错误回填给 LLM
                log.warning("AI 工具 %s 失败: %r", name, e)
                return f"[工具执行出错] {e}"
    return f"[未知工具: {name}]"


def tool_specs() -> List[Dict[str, Any]]:
    """给 LLM 的工具清单（不含 fn）。"""
    return [{"name": t["name"], "description": t["description"],
             "parameters": t["parameters"]} for t in TOOLS]


# ══════════════════════════════ 下载/上传 ══════════════════════════════
@tool("offline_download", "115 离线下载磁力/ed2k/直链（115 服务器下载，不占本地带宽）。适用于 torrent/magnet/ed2k 链接。",
      schema({"url": _s("下载链接（magnet:/ed2k:/http(s)://）"),
              "save_dir": _s("115 保存目录，如 /tg115bot/downloads；默认用户当前目标目录")}, ["url"]))
async def _offline_download(a):
    from core.offline import submit
    from config import get_config
    cfg = get_config()
    save = a.get("save_dir") or cfg.upload.target_dir
    ok, msg = await submit(a["url"], save, source="ai")
    return f"{'✅' if ok else '❌'} {msg}\n目录: {save}"


@tool("direct_download", "HTTP 直链本地中转下载后上传 115（与离线互补：115 离线不支持或慢的直链源用它）。文件名自动从 URL 提取。",
      schema({"url": _s("http(s) 直链"),
              "save_dir": _s("115 保存目录，默认用户当前目标目录")}, ["url"]))
async def _direct_download(a):
    from config import get_config
    from core.direct_dl import url_filename
    from core.queue import Task
    cfg = get_config()
    save = a.get("save_dir") or cfg.upload.target_dir
    if state.queue is None or state.workspace is None or state.pyro_bot is None:
        return "❌ 服务未就绪"
    name = url_filename(a["url"])
    tracking = await state.pyro_bot.send_message(
        state.ai_current_chat or 0,
        f"📥 直链任务（AI 触发）\n📄 {name}\n📁 {save}")
    task = Task(user_id=0, message=a["url"], filename=name, size=0,
                target_dir=save, tracking_chat_id=tracking.chat.id,
                tracking_message_id=tracking.id, source="direct")
    state.register_task(task)
    await state.queue.put(task)
    return f"✅ 已加入直链中转队列: {name} -> {save}"


@tool("share_receive", "转存 115 分享链接（需含访问码，如 https://115.com/s/xxx?password=码）到指定目录。",
      schema({"link": _s("115 分享链接（115.com/115cdn/anxia.com，带 password 参数）"),
              "save_dir": _s("保存目录，默认 config.share.target_dir}")}, ["link"]))
async def _share_receive(a):
    from config import get_config
    from cloud115.share import parse_share_link, share_list, share_receive
    cfg = get_config()
    if not cfg.share.cookies:
        return "❌ 未配置 share.cookies（转存需 webapi Cookie），请在 config.yaml 配置"
    parsed = parse_share_link(a["link"])
    if not parsed:
        return "❌ 无法解析分享链接"
    code, pwd = parsed
    if not pwd:
        return "❌ 缺访问码，请让用户提供带 ?password= 的完整链接"
    info, files = await share_list(cfg.share.cookies, code, pwd)
    if not files:
        return "分享为空"
    cloud = await state.accounts.get()
    cid = await cloud.raw.create_dir_recursive(a.get("save_dir") or cfg.share.target_dir)
    fids = [str(f.get("fid") or "") for f in files if f.get("fid")]
    await share_receive(cfg.share.cookies, code, pwd, fids, cid)
    return f"✅ 转存完成 {len(fids)} 项: {info.get('share_title') or code}"


@tool("set_target_dir", "设置该用户的默认上传/保存目录（之后的下载/转存默认存这里）。",
      schema({"path": _s("115 目录，如 /tg115bot/movies")}, ["path"]))
async def _set_target_dir(a):
    if state.ai_current_user:
        state.user_target_dirs[state.ai_current_user] = a["path"]
        return f"✅ 默认目录已设为 {a['path']}"
    return "❌ 无法确定当前用户"


# ══════════════════════════════ 文件管理 ══════════════════════════════
@tool("list_115", "列出 115 网盘指定目录的内容（名称+大小+是否目录）。",
      schema({"path": _s("115 路径，如 /tg115bot；/ 为根")}))
async def _list_115(a):
    from core.progress import human_bytes
    if state.accounts is None:
        return "❌ 115 未初始化"
    cloud = await state.accounts.get()
    path = a.get("path", "/")
    cid = 0
    if path.strip() != "/":
        info = await cloud.raw.get_file_info(path)
        if not info or info.get("file_id") is None:
            return f"❌ 路径不存在: {path}"
        cid = int(info["file_id"])
    data = await cloud.raw.list_files(cid, limit=50)
    items = data.get("list") or []
    if not items:
        return f"{path}（空）"
    lines = []
    for it in items[:50]:
        name = it.get("fn") or "?"
        is_dir = str(it.get("fc") or "1") == "0"
        lines.append(f"{'[目录] ' if is_dir else ''}{name}"
                     + ("" if is_dir else f" ({human_bytes(it.get('fs') or 0)})"))
    return "\n".join(lines)


@tool("search_115", "全盘搜索 115 网盘文件。",
      schema({"keyword": _s("搜索关键词")}, ["keyword"]))
async def _search_115(a):
    from core.progress import human_bytes
    if state.accounts is None:
        return "❌ 115 未初始化"
    cloud = await state.accounts.get()
    data = await cloud.raw.search_files(a["keyword"], limit=20)
    items = data.get("list") or []
    if not items:
        return f"未找到: {a['keyword']}"
    return "\n".join(
        f"{it.get('fn') or '?'} ({human_bytes(it.get('fs') or 0)})" for it in items[:20])


@tool("move_115", "移动 115 文件/目录到另一目录（目的目录不存在会自动创建）。",
      schema({"source": _s("源 115 路径"), "dest_dir": _s("目的目录路径")}, ["source", "dest_dir"]))
async def _move_115(a):
    if state.accounts is None:
        return "❌ 115 未初始化"
    cloud = await state.accounts.get()
    si = await cloud.raw.get_file_info(a["source"])
    if not si or si.get("file_id") is None:
        return f"❌ 源不存在: {a['source']}"
    di = await cloud.raw.get_file_info(a["dest_dir"])
    to_cid = int(di["file_id"]) if di and di.get("file_id") is not None \
        else await cloud.raw.create_dir_recursive(a["dest_dir"])
    await cloud.raw.move_files(str(si["file_id"]), to_cid)
    return f"✅ 已移动 {a['source']} -> {a['dest_dir']}"


# ══════════════════════════════ 状态查询 ══════════════════════════════
@tool("full_status", "查看系统全景：115 空间用量、离线配额、今日 API 请求（风控余量）、账号状态、本地磁盘、任务队列。无参数。",
      schema({}))
async def _full_status(a):
    if state.accounts is None:
        return "❌ 115 未初始化"
    from core.progress import human_bytes
    lines = []
    for acc in state.accounts.status_list():
        lines.append(f"账号 {acc['name']}: {acc['status']}")
    try:
        cloud = await state.accounts.get()
        sp = await cloud.raw.user_space()
        if sp.get("total"):
            lines.append(f"115 空间: {human_bytes(sp['used'])}/{human_bytes(sp['total'])}")
        q = await cloud.raw.offline_quota()
        if q:
            lines.append(f"离线配额: {q.get('used')}/{q.get('count')}")
        lines.append(f"今日 115 API 请求: {cloud.raw.request_count}/{cloud.raw.daily_limit}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"115 信息获取失败: {e}")
    if state.workspace is not None:
        lines.append(f"本地磁盘剩余: {human_bytes(state.workspace.free_bytes())}")
    if state.queue is not None:
        lines.append(f"队列: 进行中 {len(state.task_progress)} / 待处理 {state.queue.qsize()}")
    if state.db is not None:
        from persistence.models import OFFLINE_DONE, OFFLINE_FAILED
        pend = await state.db.offline_by_status("pending", "running", "retrying")
        lines.append(f"离线任务进行中: {len(pend)}")
    return "\n".join(lines)


@tool("list_tasks", "查看最近的上传/下载任务（TG 侧）历史与状态。无参数。",
      schema({}))
async def _list_tasks(a):
    if state.db is None:
        return "持久化未启用"
    rows = await state.db.recent_tasks(10)
    if not rows:
        return "暂无任务"
    from core.progress import human_bytes
    return "\n".join(
        f"{r.status} | {r.filename} ({human_bytes(r.size)}) -> {r.target_dir}"
        + (f" [{r.method}]" if r.method else "") for r in rows)


@tool("offline_status", "查看 115 离线任务队列（进行中/最近完成/失败）。无参数。",
      schema({}))
async def _offline_status(a):
    if state.db is None:
        return "持久化未启用"
    from persistence.models import OFFLINE_DONE, OFFLINE_FAILED
    rows = await state.db.offline_by_status(
        "pending", "running", "retrying", OFFLINE_DONE, OFFLINE_FAILED)
    if not rows:
        return "暂无离线任务"
    icon = {"pending": "⏳", "running": "⬇️", "retrying": "🔁", "done": "✅", "failed": "❌"}
    return "\n".join(
        f"{icon.get(r.status, '•')} {r.name or r.url[:50]} [{r.status}]" for r in rows[-15:])


# ══════════════════════════════ 订阅管理 ══════════════════════════════
@tool("rss_add", "订阅 RSS 源，新条目自动提取下载链接离线到 115。",
      schema({"url": _s("RSS/Atom 地址"),
              "save_dir": _s("离线保存目录（可选）"),
              "keywords": {"type": "array", "items": {"type": "string"},
                           "description": "标题白名单关键词（可选，空=全部条目）"}}, ["url"]))
async def _rss_add(a):
    if state.db is None:
        return "持久化未启用"
    feed = await state.db.add_feed(a["url"], "", a.get("keywords") or [],
                                   a.get("save_dir") or "", state.ai_current_chat or 0)
    if feed is None:
        return "该 RSS 已订阅过"
    return f"✅ 已订阅 #{feed.id}（每 10 分钟检查）"


@tool("rss_list", "查看 RSS 订阅列表。无参数。", schema({}))
async def _rss_list(a):
    if state.db is None:
        return "持久化未启用"
    feeds = await state.db.list_feeds()
    if not feeds:
        return "暂无 RSS 订阅"
    return "\n".join(
        f"#{f.id} {f.url[:60]} 关键词:{'/'.join(f.whitelist) or '(全部)'}"
        + (f" ⚠️{f.last_error[:40]}" if f.last_error else "") for f in feeds)


@tool("rss_del", "退订 RSS。", schema({"feed_id": {"type": "integer", "description": "订阅 ID"}}, ["feed_id"]))
async def _rss_del(a):
    if state.db is None:
        return "持久化未启用"
    await state.db.delete_feed(int(a["feed_id"]))
    return f"✅ 已退订 {a['feed_id']}"


# ══════════════════════════════ 频道 ══════════════════════════════
@tool("channel_rule_add", "添加频道监控规则：该频道新消息命中关键词自动上传 115。",
      schema({"channel_id": _s("频道 ID（如 -1001234567890）"),
              "target_dir": _s("命中后保存的 115 目录"),
              "keywords": {"type": "array", "items": {"type": "string"},
                           "description": "标题/文件名白名单（空=该频道全部媒体）"}},
             ["channel_id", "target_dir"]))
async def _channel_rule_add(a):
    if state.db is None:
        return "持久化未启用"
    try:
        cid = int(a["channel_id"])
    except (TypeError, ValueError):
        return "❌ 频道 ID 需为数字"
    rule = await state.db.upsert_rule(cid, "", a.get("keywords") or [], [],
                                      a["target_dir"], True)
    if state.monitor is not None:
        await state.monitor.reload()
    return f"✅ 频道规则 #{rule.id} 已生效（bot 需已加入该频道）"


@tool("channel_rule_list", "查看频道监控规则。无参数。", schema({}))
async def _channel_rule_list(a):
    if state.db is None:
        return "持久化未启用"
    rules = await state.db.list_rules()
    if not rules:
        return "暂无频道规则"
    return "\n".join(
        f"#{r.id} 频道 {r.channel_id} -> {r.target_dir or '(默认)'} "
        f"关键词:{'/'.join(r.whitelist) or '(全部)'}" for r in rules)


@tool("channel_rule_del", "删除频道监控规则。", schema({"rule_id": {"type": "integer", "description": "规则 ID"}}, ["rule_id"]))
async def _channel_rule_del(a):
    if state.db is None:
        return "持久化未启用"
    await state.db.delete_rule(int(a["rule_id"]))
    if state.monitor is not None:
        await state.monitor.reload()
    return f"✅ 已删除规则 {a['rule_id']}"


@tool("backup_channel", "启动频道回溯备份：把整个频道的历史媒体批量搬进 115（断点续传，可中断续跑）。",
      schema({"channel": _s("频道 ID 或 @频道用户名"),
              "save_dir": _s("保存目录（可选）")}, ["channel"]))
async def _backup_channel(a):
    from config import get_config
    from core.backup import start_backup
    if state.db is None or state.pyro_bot is None:
        return "服务未就绪"
    cfg = get_config()
    save = a.get("save_dir") or cfg.upload.target_dir
    try:
        chat = await state.pyro_bot.get_chat(a["channel"])
    except Exception as e:  # noqa: BLE001
        return f"❌ 找不到频道: {e}"
    ok, msg = await start_backup(chat.id, chat.title or str(a["channel"]), save,
                                 state.ai_current_chat or 0)
    return f"{'🚀' if ok else '⚠️'} {msg}: {chat.title or a['channel']} -> {save}"


@tool("backup_status", "查看频道备份进度。无参数。", schema({}))
async def _backup_status(a):
    if state.db is None:
        return "持久化未启用"
    rows = await state.db.list_backups()
    if not rows:
        return "暂无备份"
    icon = {"running": "▶️", "paused": "⏸", "done": "✅"}
    return "\n".join(
        f"{icon.get(r.status, '•')} #{r.id} {r.title or r.channel_id} "
        f"入队 {r.total_done}/跳过 {r.skipped}" for r in rows)
