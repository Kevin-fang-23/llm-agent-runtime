"""LLM 抽象层。

- OpenAIChatLLM：任意 OpenAI 兼容接口（GLM / vLLM / Ollama / OpenAI）。
- FakeScriptedLLM：脚本化假模型，离线跑通全链路（测试 + 演示）。

统一消息格式为 OpenAI 风格 dict（role/content/tool_calls/tool_call_id），
工具定义使用 OpenAI function schema；与 MCP 描述符的互转见 tools/registry.py。

**LLM 调用级可观测性**：`OpenAIChatLLM.chat` 是全仓唯一真实出网点，它在调用前后
记录耗时与 token 指标，并输出带 task_id / trace_id 的结构化日志 —— 这一层
（而非每个节点）才是"模型调用"这个 span 的正确落点，因为节点可能一次调用都不发
（如纯透传的审批门），也可能一次发多次（如自愈的 REPAIR 调用）。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.observability import metrics as obs_metrics
from app.observability import spans as obs_spans

log = logging.getLogger("agent.llm")


def _model_label(model: str) -> str:
    """模型名做指标标签：`model` 是**有界**取值（配置里的几个），可安全做标签。

    这是基数纪律下的少见的"动态标签"，用 `settings.llm_model` / `llm_model_cheap`
    两个值 + 测试里的假模型名，集合是有界的；相比之下 task_id 之类绝不能做标签。
    """
    return model or "unknown"


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
        # ---- LLM 调用级 span 的起点（唯一真实出网点，见模块 docstring）----
        label = _model_label(chosen)
        started = time.perf_counter()
        with obs_spans.span(obs_spans.KIND_LLM, "chat", attributes={"model": label}) as sp:
            try:
                resp = await self.client.chat.completions.create(**kwargs)
            except BaseException:
                # 出网失败同样要计数与观测：失败率（outcome=error）是这层最重要的信号，
                # 漏掉异常路径会让"上游挂了"表现为"指标一切正常、只是没有数据"
                sp.set_status("error")
                sp.set_attribute("error", "upstream")
                obs_metrics.LLM_CALLS.inc({"model": label, "outcome": "error"})
                obs_metrics.LLM_DURATION.observe(time.perf_counter() - started, {"model": label})
                log.warning("LLM 调用失败 model=%s elapsed_ms=%d", chosen,
                            int((time.perf_counter() - started) * 1000), exc_info=True)
                raise
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
            elapsed = time.perf_counter() - started
            sp.set_attribute("tokens", tokens)
            sp.set_attribute("tool_calls", len(calls))
            obs_metrics.LLM_CALLS.inc({"model": label, "outcome": "ok"})
            obs_metrics.LLM_DURATION.observe(elapsed, {"model": label})
            obs_metrics.LLM_TOKENS.inc({"model": label}, float(tokens))
            log.info("LLM 调用 model=%s elapsed_ms=%d tokens=%d tool_calls=%d",
                     chosen, int(elapsed * 1000), tokens, len(calls))
            return LLMResponse(text=msg.content or "", tool_calls=calls, model=chosen,
                               tokens_used=tokens)


class FakeScriptedLLM:
    """脚本化模型：按顺序回放响应，用于离线测试与演示。

    脚本项支持：
      - {"thought": "...", "tool": {"name": "...", "arguments": {...}}}
      - {"thought": "...", "tools": [{"name": ..., "arguments": ...}, ...]}   （一轮多工具并行）
      - {"final": "..."}
      - {"text": "..."}                （无工具纯文本，如计划 JSON、摘要）
    回放完毕后默认返回 {"final": "(脚本耗尽)"}，避免测试悬挂。

    **同样上报 LLM 指标**：离线模式（测试 / `demo_cli.py --offline`）是本项目的
    默认运行形态，假模型若不计数，则"LLM 调用级可观测性"只在生产路径上成立 ——
    而那恰恰是最难验证的一条路径。指标口径因此明确为"运行时发起的 LLM 调用"，
    与调用方是真模型还是脚本无关（脚本耗时接近 0 是事实，不做人为缩放）。
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
        started = time.perf_counter()
        self.calls.append({"messages": list(messages), "tools": tools, "model": model})
        item = self.script.pop(0) if self.script else {"final": "(脚本耗尽)"}
        tokens = estimate_messages_tokens(messages) + 50
        raw_calls = []
        if "tool" in item:
            raw_calls = [item["tool"]]
        elif "tools" in item:
            raw_calls = list(item["tools"])
        resp = self._build(item, raw_calls, model, tokens)
        label = _model_label(resp.model)
        # 与真实客户端同样开 span：离线（测试/演示）是本项目默认运行形态，
        # 假模型不产 span 会让"span 树"只在最难验证的生产路径上成立
        with obs_spans.span(obs_spans.KIND_LLM, "chat",
                            attributes={"model": label}) as sp:
            sp.set_attribute("tokens", tokens)
            sp.set_attribute("tool_calls", len(resp.tool_calls))
            sp.set_attribute("fake", True)
        obs_metrics.LLM_CALLS.inc({"model": label, "outcome": "ok"})
        obs_metrics.LLM_DURATION.observe(time.perf_counter() - started, {"model": label})
        obs_metrics.LLM_TOKENS.inc({"model": label}, float(tokens))
        return resp

    def _build(self, item: dict, raw_calls: list[dict], model: str | None,
               tokens: int) -> LLMResponse:
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
