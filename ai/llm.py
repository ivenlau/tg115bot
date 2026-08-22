"""OpenAI 兼容 LLM 客户端（aiohttp 直连，零新 SDK 依赖）。

覆盖 DeepSeek / Qwen / OpenAI / Ollama / one-api 网关等一切
OpenAI 兼容端点。请求走 ``telegram.proxy``（国内服务器必需）。

wire format: POST {base_url}/chat/completions
  tools: [{type:"function", function:{name, description, parameters}}]
  响应 message.tool_calls: [{id, function:{name, arguments(JSON 字符串)}}]
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import aiohttp

from core.app import state

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://api.openai.com/v1"
UA = "tg115bot/1.0"


class LLMError(RuntimeError):
    pass


class LLMClient:
    """极薄封装：chat(messages, tools) -> assistant message dict。"""

    def __init__(self):
        cfg = state.config.ai
        self.base_url = (cfg.base_url or DEFAULT_BASE).rstrip("/")
        self.api_key = cfg.api_key
        self.model = cfg.model
        self.temperature = cfg.temperature
        self._proxy: Optional[str] = None
        if state.config and state.config.telegram.proxy:
            self._proxy = state.config.telegram.proxy

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": UA,
        }

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """一轮对话。返回 assistant 消息 dict（含 content / tool_calls）。

        超时 120s（agent 循环里每轮一次）；429/5xx 由调用方退避重试。
        """
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = [
                {"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["parameters"]}}
                for t in tools
            ]

        timeout = aiohttp.ClientTimeout(total=120, connect=15)
        last_err: Optional[Exception] = None
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as s:
                    async with s.post(f"{self.base_url}/chat/completions",
                                      json=payload, headers=self._headers(),
                                      proxy=self._proxy) as r:
                        text = await r.text()
                        if r.status == 429 or r.status >= 500:
                            raise LLMError(f"HTTP {r.status}: {text[:150]}")
                        if r.status != 200:
                            raise LLMError(f"LLM HTTP {r.status}: {text[:300]}")
                        data = json.loads(text)
                choices = data.get("choices") or []
                if not choices or not (choices[0].get("message")):
                    raise LLMError(f"响应无 choices: {str(data)[:200]}")
                msg = choices[0]["message"]
                # 统一 tool_calls 归一（arguments 是 JSON 字符串）
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        try:
                            fn["arguments"] = json.loads(args or "{}")
                        except ValueError:
                            fn["arguments"] = {"_raw": args}
                return msg
            except (aiohttp.ClientError, asyncio.TimeoutError, LLMError) as e:
                last_err = e
                if isinstance(e, LLMError) and "HTTP 4" in str(e) and "429" not in str(e):
                    raise              # 4xx（非429）不重试
                await asyncio.sleep(2 * (attempt + 1))
        raise LLMError(f"LLM 请求失败(重试3次): {last_err}")
