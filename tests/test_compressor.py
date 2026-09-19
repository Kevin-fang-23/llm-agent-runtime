"""上下文压缩：长轨迹摘要 + 关键数据保留。"""
from __future__ import annotations

from app.core.compressor import compress_messages, inject_key_outputs, needs_compression
from app.core.llm import FakeScriptedLLM, estimate_messages_tokens


def _long_history(n: int) -> list[dict]:
    return [
        {"role": "assistant", "content": f"步骤{i}：执行查询与计算 " + "数据" * 100}
        for i in range(n)
    ]


async def test_compression_kicks_in_and_shrinks():
    llm = FakeScriptedLLM([{"text": "此前完成了 14 步查询，确认了北京 31℃、上海 28℃ 的数据。"}])
    msgs = _long_history(20)
    assert needs_compression(msgs, 2000)
    compressed, summary = await compress_messages(llm, msgs, 2000)
    assert summary != ""
    assert estimate_messages_tokens(compressed) < estimate_messages_tokens(msgs)
    # 摘要消息在最前，最近 6 条原样保留
    assert "先前执行摘要" in compressed[0]["content"]
    assert compressed[1:] == msgs[-6:]


async def test_compression_noop_under_threshold():
    llm = FakeScriptedLLM([])
    msgs = _long_history(2)
    compressed, summary = await compress_messages(llm, msgs, 100000)
    assert compressed == msgs and summary == ""


def test_key_outputs_injection():
    out = inject_key_outputs("系统提示", {"0:web_search": "北京 31℃"})
    assert "关键数据" in out and "北京 31℃" in out
    assert inject_key_outputs("系统提示", {}) == "系统提示"
