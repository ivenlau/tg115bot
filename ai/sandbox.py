"""受限沙箱：执行 AI 生成的动态工具代码。

安全模型（纵深防御）：
  1. 静态检查（创建前）：拒绝 import/open/exec/eval/下划线属性访问等危险 token
  2. 受控命名空间：只注入白名单 builtins + 安全原语（http_get/字符串/JSON）+ call_tool
  3. 线程池 + 5s 超时：超时后放弃等待（线程可能泄漏但进程无害）；
     裸 while True 已被静态检查拒绝
  4. 网络仅经 http_get 白名单原语（默认禁网，allow_net=True 才开且仅 http(s)）
  5. 无文件系统访问（无 open/__import__/os/sys）
  6. 输出限长 4000 字符

代码形态：函数体字符串（无 def 头），注入为 ``def _dynamic_tool(args: dict) -> str``
在沙箱内 exec 编译执行（exec 仅用于我们拼接的受控模板，AI 代码本身经静态检查）。
同步执行（沙箱内不得 await）；http_get 用 aiohttp 在外层预取的模型不适用——
动态工具里需要同步 HTTP，故 http_get 用 urllib（受超时与白名单约束）。

⚠️ 这是最佳努力沙箱，非 VM 级隔离。危险 token 清单覆盖常见逃逸路径；
如需更强隔离可后续换 Docker 执行器。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.request
from typing import Any, Dict

log = logging.getLogger("tg115bot.ai.sandbox")

EXEC_TIMEOUT = 5          # 秒
MAX_OUTPUT = 4000
MAX_CODE_LEN = 4000

# 危险 token（静态检查拒绝）——注意匹配词边界，避免误杀变量名里的子串
FORBIDDEN = [
    r"\bwhile\s+True\b(?!.*\bbreak\b)",   # 裸死循环（同表达式内无 break）
    r"\bimport\b", r"\bfrom\b\s+\w+\s+\bimport\b", r"\b__import__\b",
    r"\bopen\b", r"\bexec\b", r"\beval\b", r"\bcompile\b",
    r"\bgetattr\b", r"\bsetattr\b", r"\bglobals\b", r"\blocals\b",
    r"\b__\w+__\b",                     # 一切 dunder
    r"\bos\.", r"\bsys\.", r"\bsubprocess\.", r"\bshutil\.", r"\bsocket\.",
    r"\bctypes\.", r"\bmultiprocessing\.", r"\bthreading\.",
]
_FORBIDDEN_RE = [re.compile(p) for p in FORBIDDEN]

# 白名单 builtins
SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "filter": filter, "float": float, "int": int,
    "isinstance": isinstance, "len": len, "list": list, "map": map,
    "max": max, "min": min, "print": print, "range": range, "repr": repr,
    "reversed": reversed, "round": round, "set": set, "sorted": sorted,
    "str": str, "sum": sum, "tuple": tuple, "type": type, "zip": zip,
    "True": True, "False": False, "None": None,
    "ValueError": ValueError, "TypeError": TypeError, "KeyError": KeyError,
    "Exception": Exception, "RuntimeError": RuntimeError,
    "StopIteration": StopIteration,
}


class SandboxViolation(RuntimeError):
    """静态检查不通过。"""


def static_check(code: str) -> None:
    """创建前的静态检查：危险 token 直接拒绝。"""
    if len(code) > MAX_CODE_LEN:
        raise SandboxViolation(f"代码过长（>{MAX_CODE_LEN} 字符）")
    for rex in _FORBIDDEN_RE:
        m = rex.search(code)
        if m:
            raise SandboxViolation(f"包含禁止的 token: {m.group(0)!r}")


def http_get(url: str, timeout: int = 10) -> str:
    """安全原语：GET 请求（仅 http/https，限 256KB 响应）。"""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("仅支持 http(s)")
    req = urllib.request.Request(url, headers={"User-Agent": "tg115bot-sandbox/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 已限 scheme
        data = r.read(256 * 1024)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data[:200].hex()


def run_sandbox(code: str, args: Dict[str, Any],
                call_tool=None, allow_net: bool = False) -> str:
    """同步执行（在线程池中跑）：code 为函数体，args 为工具参数。

    call_tool: (name, args) -> str 同步函数（注入为 call_tool，供组合已有工具）。
    """
    static_check(code)
    ns: Dict[str, Any] = {"__builtins__": SAFE_BUILTINS,
                          "json_module": json, "re_module": re}
    # 白名单方式暴露 json/re 的常用子集
    ns["json_loads"] = json.loads
    ns["json_dumps"] = json.dumps
    ns["re_search"] = re.search
    ns["re_findall"] = re.findall
    ns["re_sub"] = re.sub
    ns["http_get"] = http_get if allow_net else _net_disabled
    if call_tool is not None:
        ns["call_tool"] = call_tool

    src = "def _dynamic_tool(args):\n" + "\n".join(
        "    " + line for line in code.splitlines()) + "\n"
    # exec 只编译我们拼接的模板；code 已过 static_check
    exec(compile(src, "<ai-dynamic-tool>", "exec"), ns)  # noqa: S102 受控模板
    result = ns["_dynamic_tool"](args or {})
    out = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return out[:MAX_OUTPUT]


def _net_disabled(*a, **k):
    raise RuntimeError("网络默认禁用；创建工具时需声明 allow_net")


async def run_sandbox_async(code: str, args: Dict[str, Any],
                            call_tool=None, allow_net: bool = False) -> str:
    """异步包装：线程池 + 超时。

    注意：超时后线程可能仍在跑（无法强杀线程），但调用方拿到 TimeoutError、
    结果被丢弃，进程无害；裸 while True 由 static_check 拒绝，正常场景不会触达。
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(run_sandbox, code, args, call_tool, allow_net),
            timeout=EXEC_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"动态工具执行超时(>{EXEC_TIMEOUT}s)，已放弃")
