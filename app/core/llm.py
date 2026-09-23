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

import asyncio
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


def estimate_call_tokens(messages: list[dict], completion: str = "") -> int:
    """统一的 LLM 调用 token 口径：输入估算 + 输出估算。

    真实模型有 usage 时以 total_tokens 为准，本函数只在 usage 缺失时兜底；
    假模型没有 usage，直接就是唯一口径。旧实现两边一个 `输入*2`、一个
    `输入+50`，离线（测试/演示）与在线的 tokens_used 量级互不可比，
    而 tokens_used 同时喂给预算控制与 L4 配额判定。
    """
    return estimate_messages_tokens(messages) + estimate_tokens(completion)


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any]
    # 模型输出的 arguments 不是合法 JSON 时的错误描述（含原始文本截断）。
    # 空串 = 解析正常。不在解析层抛异常：那会绕过整条自愈契约把任务直接判死，
    # 而截断/带围栏的 arguments 恰是小模型最常见的失败形态，交参数修复循环处理。
    args_parse_error: str = ""


def _parse_tool_arguments(raw: str) -> tuple[dict[str, Any], str]:
    """把模型给出的 arguments 文本解析成 dict。返回 (参数, 解析错误说明)。

    失败时参数退化为空 dict、错误说明回喂自愈修复器（repair prompt 里能看到原文）。
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        return {}, f"模型输出的 tool arguments 不是合法 JSON（{e}）；原始文本: {raw[:500]}"
    if not isinstance(parsed, dict):
        return ({}, ("模型输出的 tool arguments 不是 JSON 对象"
                     f"（实际是 {type(parsed).__name__}）；原始文本: {raw[:500]}"))
    return parsed, ""


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


def _is_retryable_llm_error(exc: BaseException) -> bool:
    """模型调用失败里值得重试的部分：连接/超时类、429、5xx。

    其余（401 鉴权、400 请求非法等）同一 key 换模型也一样失败，重试只是拖延。
    openai 异常惰性导入：判定逻辑不与模块导入绑死。
    """
    try:
        from openai import APIConnectionError, APIStatusError, RateLimitError
    except ImportError:
        return False
    # APITimeoutError 是 APIConnectionError 的子类
    if isinstance(exc, APIConnectionError):
        return True
    if isinstance(exc, RateLimitError):
        return True
    return isinstance(exc, APIStatusError) and exc.status_code >= 500


def _llm_retry_delay(exc: BaseException, attempt: int, base_s: float) -> float:
    """退避时长：上游 Retry-After 优先，但**必须封顶**——上游回 86400 秒
    不能让事件循环睡一天（对齐 H4 的教训：hint 无界是缺陷而非特性）。"""
    from app.core.errors import parse_retry_after

    hint = None
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None:
        hint = parse_retry_after(headers.get("retry-after"))
    delay = hint if hint is not None else base_s * (2 ** max(0, attempt - 1))
    return min(delay, 30.0)


class OpenAIChatLLM:
    """OpenAI 兼容异步客户端。model 参数允许运行时降级切换。

    出网防护（此前整层裸奔：无超时、无重试，一次网络抖动即判死整个任务）：
      - 显式 timeout：openai SDK 默认 600s 对交互式任务形同无限等待；
      - SDK 内置重试关闭（max_retries=0），退避重试收编到本层：SDK 的重试
        不会切降级模型，且把失败尝试藏进成功计时里，LLM_CALLS 的 error 口径失真；
      - 可重试类失败（连接/超时/429/5xx）用尽后，若配了 fallback_model，
        自动换模型再试一轮——fallback 只救瞬时故障，鉴权/参数类直接上抛。
    """

    def __init__(self, base_url: str, api_key: str, default_model: str, fallback_model: str = "",
                 timeout_s: float = 60.0, max_retries: int = 2):
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key or "EMPTY",
                                  timeout=timeout_s, max_retries=0)
        self.default_model = default_model
        self.fallback_model = fallback_model
        self.max_retries = max(0, max_retries)
        # 退避基数与测试钩子对齐（测试里设 0 免等）
        self.retry_base_delay_s = 1.0

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        primary = model or self.default_model
        chain = [primary]
        if self.fallback_model and self.fallback_model != primary:
            chain.append(self.fallback_model)
        for i, chosen in enumerate(chain):
            try:
                return await self._chat_once(chosen, messages, tools)
            except Exception as e:
                if i + 1 >= len(chain) or not _is_retryable_llm_error(e):
                    raise
                log.warning("模型 %s 重试后仍失败，降级到 %s: %s",
                            chosen, chain[i + 1], e)
        raise RuntimeError("unreachable：候选链最后一个必然 return 或 raise")

    async def _chat_once(
        self, chosen: str, messages: list[dict], tools: list[dict] | None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {"model": chosen, "messages": messages, "temperature": 0.2}
        if tools:
            kwargs["tools"] = tools
        # ---- LLM 调用级 span 的起点（唯一真实出网点，见模块 docstring）----
        label = _model_label(chosen)
        started = time.perf_counter()
        attempts = 0
        with obs_spans.span(obs_spans.KIND_LLM, "chat", attributes={"model": label}) as sp:
            while True:
                attempts += 1
                attempt_started = time.perf_counter()
                try:
                    resp = await self.client.chat.completions.create(**kwargs)
                    break
                except BaseException as e:
                    # 每次真实出网失败都计数：失败率（outcome=error）是这层最重要的
                    # 信号，藏在重试背后会让"上游挂了"表现为"指标一切正常、只是没数据"。
                    # CancelledError 等非 Exception 不满足可重试判定，原样上抛。
                    obs_metrics.LLM_CALLS.inc({"model": label, "outcome": "error"})
                    obs_metrics.LLM_DURATION.observe(
                        time.perf_counter() - attempt_started, {"model": label})
                    log.warning("LLM 调用失败 model=%s attempt=%d elapsed_ms=%d", chosen,
                                attempts,
                                int((time.perf_counter() - attempt_started) * 1000),
                                exc_info=True)
                    # CancelledError 等非 Exception 不满足可重试判定，走上面的 raise
                    if not _is_retryable_llm_error(e) or attempts > self.max_retries:
                        sp.set_status("error")
                        sp.set_attribute("error", "upstream")
                        sp.set_attribute("attempts", attempts)
                        raise
                    await asyncio.sleep(
                        _llm_retry_delay(e, attempts, self.retry_base_delay_s))
            msg = resp.choices[0].message
            calls = []
            for tc in (msg.tool_calls or []):
                raw = tc.function.arguments or "{}"
                args, parse_err = _parse_tool_arguments(raw)
                calls.append(ToolCallRequest(id=tc.id, name=tc.function.name,
                                             arguments=args, args_parse_error=parse_err))
            usage = getattr(resp, "usage", None)
            if usage:
                tokens = getattr(usage, "total_tokens", 0)
            else:
                # usage 缺失：与假模型同一口径估算（输入 + 本次输出文本，
                # 输出含工具调用参数；解析失败的原始文本也计入，不漏计）
                completion = (msg.content or "") + "".join(
                    json.dumps(c.arguments, ensure_ascii=False) + c.args_parse_error
                    for c in calls)
                tokens = estimate_call_tokens(messages, completion)
            elapsed = time.perf_counter() - started
            sp.set_attribute("tokens", tokens)
            sp.set_attribute("tool_calls", len(calls))
            sp.set_attribute("attempts", attempts)
            obs_metrics.LLM_CALLS.inc({"model": label, "outcome": "ok"})
            obs_metrics.LLM_DURATION.observe(elapsed, {"model": label})
            obs_metrics.LLM_TOKENS.inc({"model": label}, float(tokens))
            log.info("LLM 调用 model=%s elapsed_ms=%d tokens=%d tool_calls=%d attempts=%d",
                     chosen, int(elapsed * 1000), tokens, len(calls), attempts)
            return LLMResponse(text=msg.content or "", tool_calls=calls, model=chosen,
                               tokens_used=tokens)


def _script_completion_text(item: dict) -> str:
    """假模型脚本项的"输出"文本：正文（回答/最终答案/思考）+ 工具调用参数。

    与真实客户端 usage 缺失时的兜底口径对称（content + arguments JSON），
    两边共用 estimate_call_tokens，估算量级才可比。
    """
    calls = [item["tool"]] if "tool" in item else list(item.get("tools") or [])
    text = item.get("text") or item.get("final") or item.get("thought") or ""
    return text + "".join(json.dumps(c.get("arguments", {}), ensure_ascii=False)
                          for c in calls)


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
        item = self.script.pop(0) if self.script else {"final": "(脚本耗尽)"}
        tokens = estimate_call_tokens(messages, _script_completion_text(item))
        self.calls.append({"messages": list(messages), "tools": tools,
                           "model": model, "tokens": tokens})
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
