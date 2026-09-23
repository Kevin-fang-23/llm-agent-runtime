"""H1/H2/H3 回归：LLM 出网防护、畸形 arguments 自愈、修复后运行错的原样归类。

对应审查结论：
  H1 自愈循环内运行时错被硬标 validation/INVALID_ARGS → fatal 码被误判为计划缺陷；
  H2 模型输出 arguments 非法 JSON 在解析层抛 ValueError 打死整个任务；
  H3 LLM 侧无超时/重试/降级（fallback_model 是死字段），一次抖动即判死任务。
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError

from app.core.errors import ToolErrorCode
from app.core.llm import FakeScriptedLLM, LLMResponse, OpenAIChatLLM, ToolCallRequest
from app.graph.engine import AgentEngine
from app.graph.state import STATUS_DONE, STATUS_FAILED
from app.observability import metrics as obs_metrics
from app.tools.registry import ToolExecutionError, ToolRegistry, ToolSpec
from tests.conftest import collect_events, make_engine


def _request() -> httpx.Request:
    return httpx.Request("POST", "http://llm.test/v1/chat/completions")


def _status_error(status: int, headers: dict | None = None) -> Exception:
    resp = httpx.Response(status_code=status, request=_request(), headers=headers or {})
    return APIStatusError(f"status {status}", response=resp, body=None)


def _conn_error() -> Exception:
    return APIConnectionError(request=_request())


def _ok_response(arguments: str = "{}"):
    fn = SimpleNamespace(name="web_search", arguments=arguments)
    tc = SimpleNamespace(id="call_1", function=fn)
    msg = SimpleNamespace(content="", tool_calls=[tc])
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg)],
        usage=SimpleNamespace(total_tokens=7))


class _Client:
    """按脚本回放 create() 结果：条目为异常则抛，否则返回。"""

    def __init__(self, script: list):
        self.script = list(script)
        self.n = 0
        self.chat = self
        self.completions = self

    async def create(self, **kw):
        self.n += 1
        item = self.script.pop(0) if self.script else _ok_response()
        if isinstance(item, Exception):
            raise item
        return item


# ---------------- H2：arguments JSON 解析容错 ----------------

async def test_malformed_tool_arguments_folded_into_parse_error():
    """截断的 arguments 不再让 chat() 抛 ValueError，而是带 args_parse_error 交付。"""
    llm = OpenAIChatLLM(base_url="http://x", api_key="k", default_model="m-h2")
    llm.client = _Client([_ok_response('{"query": "北')])
    resp = await llm.chat([{"role": "user", "content": "hi"}])
    call = resp.tool_calls[0]
    assert call.arguments == {}
    assert "不是合法 JSON" in call.args_parse_error
    assert '{"query": "北' in call.args_parse_error


async def test_non_object_tool_arguments_folded_into_parse_error():
    llm = OpenAIChatLLM(base_url="http://x", api_key="k", default_model="m-h2b")
    llm.client = _Client([_ok_response("[1, 2, 3]")])
    resp = await llm.chat([{"role": "user", "content": "hi"}])
    assert "不是 JSON 对象" in resp.tool_calls[0].args_parse_error


async def test_args_parse_error_enters_selfheal_instead_of_crash(registry, settings):
    """节点级：解析失败的工具调用走自愈循环修复成功，任务不再整体崩溃。"""
    class _BadArgsLLM(FakeScriptedLLM):
        async def chat(self, messages, tools=None, model=None):
            if not self.calls:
                self.calls.append({"messages": messages, "tools": tools, "model": model})
                return LLMResponse(
                    text="搜索", model="fake", tokens_used=10,
                    tool_calls=[ToolCallRequest(
                        id="call_0_0", name="web_search", arguments={},
                        args_parse_error='模型输出的 tool arguments 不是合法 JSON（截断）：{"quer')])
            return await super().chat(messages, tools, model)

    events, sink = collect_events()
    llm = _BadArgsLLM([
        {"text": '{"query": "北京 天气"}'},  # 修复器给出合法参数
        {"final": "北京晴。"},
    ])
    engine = AgentEngine(settings=settings, llm=llm, registry=registry, event_sink=sink)
    final = await engine.run_task("t-h2-node", "查北京天气", "react", 60000, 24)

    types = [e["type"] for e in events]
    assert "tool_validation_failed" in types and "selfheal_success" in types
    failed_evt = next(e for e in events if e["type"] == "tool_validation_failed")
    assert "不是合法 JSON" in failed_evt["payload"]["error"]
    assert final["status"] == STATUS_DONE


# ---------------- H3：超时 / 退避重试 / fallback ----------------

async def test_llm_retries_transient_connection_failure(monkeypatch):
    llm = OpenAIChatLLM(base_url="http://x", api_key="k", default_model="m-rt",
                        max_retries=2)
    monkeypatch.setattr(llm, "retry_base_delay_s", 0.0)
    llm.client = _Client([_conn_error(), _conn_error(), _ok_response('{"q": 1}')])
    resp = await llm.chat([{"role": "user", "content": "hi"}])
    assert resp.model == "m-rt"
    assert llm.client.n == 3  # 前两次连接失败已退避重试
    assert obs_metrics.LLM_CALLS.value({"model": "m-rt", "outcome": "error"}) == 2
    assert obs_metrics.LLM_CALLS.value({"model": "m-rt", "outcome": "ok"}) == 1


async def test_llm_retry_gives_up_after_max(monkeypatch):
    llm = OpenAIChatLLM(base_url="http://x", api_key="k", default_model="m-give",
                        max_retries=1)
    monkeypatch.setattr(llm, "retry_base_delay_s", 0.0)
    llm.client = _Client([_conn_error(), _conn_error()])
    with pytest.raises(APIConnectionError):
        await llm.chat([{"role": "user", "content": "hi"}])
    assert llm.client.n == 2  # 1 次初始 + max_retries=1 次重试


async def test_llm_non_retryable_fails_fast():
    """400 类错误：同一 key 重试/换模型都不会更好，立即上抛且不多打一次。"""
    llm = OpenAIChatLLM(base_url="http://x", api_key="k", default_model="m-400",
                        fallback_model="m-fb", max_retries=3)
    llm.client = _Client([_status_error(400)])
    with pytest.raises(APIStatusError):
        await llm.chat([{"role": "user", "content": "hi"}])
    assert llm.client.n == 1  # 无重试、未触碰 fallback


async def test_llm_falls_back_to_second_model():
    """fallback_model 不再是死字段：主模型重试耗尽后自动换模型成功。"""
    ok_calls = []

    class _TwoPhase(_Client):
        async def create(self, **kw):
            ok_calls.append(kw.get("model"))
            return await super().create(**kw)

    llm = OpenAIChatLLM(base_url="http://x", api_key="k", default_model="m-main",
                        fallback_model="m-fb", max_retries=0)
    llm.client = _TwoPhase([_conn_error()])  # 主模型失败；之后脚本耗尽 → fallback 成功
    resp = await llm.chat([{"role": "user", "content": "hi"}])
    assert resp.model == "m-fb"
    assert ok_calls == ["m-main", "m-fb"]
    assert obs_metrics.LLM_CALLS.value({"model": "m-main", "outcome": "error"}) == 1
    assert obs_metrics.LLM_CALLS.value({"model": "m-fb", "outcome": "ok"}) == 1


def test_llm_client_has_explicit_timeout():
    llm = OpenAIChatLLM(base_url="http://x", api_key="k", default_model="m",
                        timeout_s=12.5)
    assert float(llm.client.timeout) == 12.5
    assert llm.client.max_retries == 0  # 重试收编在本层，SDK 内置重试必须关掉


# ---------------- H1：修复后的运行错保持原归类 ----------------

async def test_runtime_error_after_repair_classifies_fatal(settings):
    """H1 回归：自愈修复成功后真执行抛 PERMISSION——观测必须是 runtime/permission，
    critic 判 fatal 直接终止；旧实现冒充 validation/invalid_args，会被误判为
    计划缺陷而去重规划。"""
    reg = ToolRegistry()

    async def handler(args):
        raise ToolExecutionError("上游拒绝：权限不足", code=ToolErrorCode.PERMISSION)

    reg.register(ToolSpec(
        name="probe", description="测试探针",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}},
                      "required": ["query"]},
        handler=handler))
    events, sink = collect_events()
    script = [
        {"tool": {"name": "probe", "arguments": {"bad": 1}}},   # 校验失败 → 自愈
        {"text": '{"query": "ok"}'},                             # 修复成功 → 真执行 → PERMISSION
    ]
    engine, _ = make_engine(settings, script, reg, event_sink=sink)
    final = await engine.run_task("t-h1", "探测", "react", 60000, 24)

    tool_err = next(e for e in events if e["type"] == "tool_error")
    assert tool_err["payload"]["error_type"] == "runtime"
    assert tool_err["payload"]["error_code"] == "permission"
    critic = next(e for e in events if e["type"] == "critic")
    assert critic["payload"]["verdict"] == "fatal"
    assert not any(e["type"] == "replan" for e in events)
    assert final["status"] == STATUS_FAILED


async def test_transient_runtime_error_after_repair_gets_retry(settings):
    """修复后的瞬时运行错同样享有原样退避重试契约（旧实现直接吞成 validation）。"""
    reg = ToolRegistry()
    state_calls = {"n": 0}

    async def handler(args):
        state_calls["n"] += 1
        if state_calls["n"] == 1:
            raise ToolExecutionError("上游 502", code=ToolErrorCode.UPSTREAM_5XX)
        return {"summary": "成功"}

    reg.register(ToolSpec(
        name="probe2", description="探针", retry_transient=True,
        input_schema={"type": "object", "properties": {"query": {"type": "string"}},
                      "required": ["query"]},
        handler=handler))
    events, sink = collect_events()
    script = [
        {"tool": {"name": "probe2", "arguments": {"bad": 1}}},
        {"text": '{"query": "ok"}'},   # 修复 → 首次真执行 502 → 退避重试成功
        {"final": "完成"},
    ]
    engine, _ = make_engine(settings, script, reg, event_sink=sink)
    final = await engine.run_task("t-h1b", "探测", "react", 60000, 24)

    types = [e["type"] for e in events]
    assert "tool_retry_scheduled" in types and "tool_retry_success" in types
    assert final["status"] == STATUS_DONE
