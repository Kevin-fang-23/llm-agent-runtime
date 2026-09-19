"""LLM 抽象层。

- OpenAIChatLLM：任意 OpenAI 兼容接口（GLM / vLLM / Ollama / OpenAI）。
- FakeScriptedLLM：脚本化假模型，离线跑通全链路（测试 + 演示）。

统一消息格式为 OpenAI 风格 dict（role/content/tool_calls/tool_call_id），
工具定义使用 OpenAI function schema；与 MCP 描述符的互转见 tools/registry.py。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：CJK 字符近似 1 字 1 token，其他 4 字符 1 token。

    供预算控制与上下文压缩决策使用；不追求与分词器一致，
    只要量级正确且随文本长度单调即可。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + (len(text) - cjk) // 4 + 1


def estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        total += estimate_tokens(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            total += estimate_tokens(json.dumps(tc.get("arguments", {}), ensure_ascii=False))
    return total


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    model: str = ""
    tokens_used: int = 0


class ChatLLM(Protocol):
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
    ) -> LLMResponse: ...


class OpenAIChatLLM:
    """OpenAI 兼容异步客户端。model 参数允许运行时降级切换。"""

    def __init__(self, base_url: str, api_key: str, default_model: str, fallback_model: str = ""):
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key or "EMPTY")
        self.default_model = default_model
        self.fallback_model = fallback_model

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        chosen = model or self.default_model
        kwargs: dict[str, Any] = {"model": chosen, "messages": messages, "temperature": 0.2}
        if tools:
            kwargs["tools"] = tools
        resp = await self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        calls = [
            ToolCallRequest(
                id=tc.id,
                name=tc.function.name,
                arguments=json.loads(tc.function.arguments or "{}"),
            )
            for tc in (msg.tool_calls or [])
        ]
        usage = getattr(resp, "usage", None)
        tokens = getattr(usage, "total_tokens", 0) if usage else estimate_messages_tokens(messages) * 2
        return LLMResponse(text=msg.content or "", tool_calls=calls, model=chosen, tokens_used=tokens)


class FakeScriptedLLM:
    """脚本化模型：按顺序回放响应，用于离线测试与演示。

    脚本项支持：
      - {"thought": "...", "tool": {"name": "...", "arguments": {...}}}
      - {"thought": "...", "tools": [{"name": ..., "arguments": ...}, ...]}   （一轮多工具并行）
      - {"final": "..."}
      - {"text": "..."}                （无工具纯文本，如计划 JSON、摘要）
    回放完毕后默认返回 {"final": "(脚本耗尽)"}，避免测试悬挂。
    """

    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.calls: list[dict] = []

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        self.calls.append({"messages": list(messages), "tools": tools, "model": model})
        item = self.script.pop(0) if self.script else {"final": "(脚本耗尽)"}
        tokens = estimate_messages_tokens(messages) + 50
        raw_calls = []
        if "tool" in item:
            raw_calls = [item["tool"]]
        elif "tools" in item:
            raw_calls = list(item["tools"])
        if raw_calls:
            return LLMResponse(
                text=item.get("thought", ""),
                tool_calls=[
                    ToolCallRequest(id=f"call_{len(self.calls)}_{i}", name=c["name"], arguments=c["arguments"])
                    for i, c in enumerate(raw_calls)
                ],
                model=model or "fake",
                tokens_used=tokens,
            )
        if "final" in item:
            return LLMResponse(text=item["final"], model=model or "fake", tokens_used=tokens)
        return LLMResponse(text=item.get("text", ""), model=model or "fake", tokens_used=tokens)
