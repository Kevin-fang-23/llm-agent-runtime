"""当前时间上下文：注入到每轮现构造的 system，且不进入对话历史。

背景缺陷：LLM 的训练知识有截止日期，问"今天是几号"它会拿记忆里的日期答
（实测答成 2024-06-19，比真实日期早两年多）。时间属于"环境事实"，
必须由运行时在每轮输入里给出。
"""
from __future__ import annotations

from datetime import date, datetime

from app.graph.nodes import _now_context
from tests.conftest import make_engine

MARK = "【当前时间】"


def _system_of(call: dict) -> str:
    for m in call["messages"]:
        if m.get("role") == "system":
            return m.get("content", "")
    return ""


def _injected_date(text: str) -> date:
    """从注入文本里取出日期。"""
    import re
    m = re.search(r"(\d{4})-(\d{2})-(\d{2}) \d{2}:\d{2}:\d{2}", text)
    assert m, f"未找到日期时间: {text[:200]!r}"
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def test_now_context_format():
    """格式含日期时间、星期、UTC 偏移，且日期就是今天。"""
    text = _now_context()
    assert MARK in text
    assert "星期" in text
    assert "UTC" in text
    assert abs((_injected_date(text) - datetime.now().date()).days) <= 1
    # 必须明确告诉模型"不要用记忆中的日期"，否则它可能仍按训练数据作答
    assert "训练知识" in text and "截止" in text


async def test_react_step_system_carries_current_time(settings, registry):
    """react 模式（用户实际会遇到的场景）：每轮 system 都带当前时间。"""
    engine, llm = make_engine(settings, [{"final": "今天是 X 号"}], registry)
    await engine.run_task("time-1", "今天是几号", "react", 60000, 24)

    assert llm.calls, "模型未被调用"
    for call in llm.calls:
        system = _system_of(call)
        assert MARK in system, "system 缺少当前时间上下文"
        assert abs((_injected_date(system) - datetime.now().date()).days) <= 1


async def test_planner_and_finisher_also_carry_time(settings, registry):
    """plan_execute 模式会用到 planner / react_step / finisher 三个节点，三者都要带。"""
    script = [
        {"text": '{"steps": ["给出当前日期"]}'},   # planner
        {"final": "今天是 X 号"},                   # react_step
        {"final": "汇总：今天是 X 号"},             # finisher
        {"final": "备用"},
    ]
    engine, llm = make_engine(settings, script, registry)
    await engine.run_task("time-2", "今天是几号", "plan_execute", 60000, 24)

    assert len(llm.calls) >= 3, f"应有 planner/react/finisher 多次调用，实际 {len(llm.calls)}"
    for i, call in enumerate(llm.calls):
        assert MARK in _system_of(call), f"第 {i + 1} 次调用缺少时间上下文"


async def test_time_never_enters_conversation_history(settings, registry):
    """时间必须留在"每轮重建的输入"通道里，不能写进历史。

    写进历史有两个后果：① 历史里的时间下一刻就是错的；
    ② 重蹈 P0-2 的覆辙（每轮 system+user 入历史 → 上下文 O(n²) 膨胀）。
    """
    engine, llm = make_engine(
        settings,
        [{"tool": {"name": "web_search", "arguments": {"query": "北京时间"}}},
         {"final": "完成"}],
        registry,
    )
    final = await engine.run_task("time-3", "查一下时间", "react", 60000, 24)

    history = [m for m in final["messages"] if m.get("role") != "system"]
    assert history, "历史不应为空"
    for m in history:
        assert MARK not in str(m.get("content", "")), "当前时间被写进了对话历史"
    # 同时确认 system 没有入历史（保持 P0-2 的通道划分）
    assert all(m.get("role") != "system" for m in final["messages"]), \
        "state.messages 里不应出现 system 消息"


async def test_time_context_does_not_break_compression(settings, registry):
    """注入时间后，压缩流程仍要正常工作（system 每轮重建、压缩只作用于历史段）。"""
    tuned = settings.model_copy(update={"compress_threshold_tokens": 800})
    engine, llm = make_engine(
        tuned,
        [{"tool": {"name": "web_search", "arguments": {"query": "q1"}}},
         {"tool": {"name": "web_search", "arguments": {"query": "q2"}}},
         {"tool": {"name": "web_search", "arguments": {"query": "q3"}}},
         {"final": "完成"}],
        registry,
    )
    await engine.run_task("time-4", "连续检索测试", "react", 60000, 24)
    for call in llm.calls:
        msgs = call["messages"]
        # system 只应出现在头部一次（压缩器的切片假设）
        assert sum(1 for m in msgs if m.get("role") == "system") == 1
        assert MARK in _system_of(call)
