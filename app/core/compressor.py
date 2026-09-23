"""上下文压缩：长轨迹摘要，关键工具输出标记为不可压缩。

机制：
  - 消息总 token 估算超过阈值时，保留 system 提示 + 最近 WINDOW 条消息，
    其余由 LLM 压成一条「先前执行摘要」消息。
  - 每个工具的 key_result 输出（查到的数据等）在产生时即写入
    state.key_outputs（截断保存），摘要丢失也不会丢关键状态；
    key_outputs 以独立 system 段注入每一步模型调用。
"""
from __future__ import annotations

from typing import Protocol

from app.core.llm import estimate_messages_tokens

# 压缩时保留的最近消息窗口（含 assistant 思考与 tool 结果）
KEEP_RECENT_WINDOW = 6

SUMMARY_PROMPT = (
    "你是 Agent 执行轨迹的摘要器。以下是一段先前执行记录，"
    "请用不超过 200 字压缩，保留：已完成的动作、使用过的工具与参数要点、"
    "已确认的关键事实/数据、待办事项。直接输出摘要正文。\n\n{trajectory}"
)


class SummarizerLLM(Protocol):
    async def chat(self, messages: list[dict], tools=None, model=None): ...


def needs_compression(messages: list[dict], threshold_tokens: int) -> bool:
    return estimate_messages_tokens(messages) > threshold_tokens


async def compress_messages(
    llm: SummarizerLLM,
    messages: list[dict],
    threshold_tokens: int,
    model: str | None = None,
) -> tuple[list[dict], str, int]:
    """返回 (压缩后消息列表, 摘要文本, 摘要调用消耗的 token)。

    tokens 必须由调用方上卷进 state.tokens_used：压缩是真实出网的 LLM 调用，
    漏记会让预算与 L4 日配额系统性低报（与 finisher 同一上卷纪律）。
    model 透传：已因超预算降级到便宜模型的任务，摘要不该再按主模型计费。

    划分规则（不依赖消息下标假设）：
      - pinned：所有 system 提示，原样保留在头部（压缩后仍由节点每轮重建，这里只是兜底）
      - history：除 system 外的全部历史，末尾 KEEP_RECENT_WINDOW 条原样保留，其余交给 LLM 摘要
      - 窗口左边界必须落在 assistant 上：孤儿 tool 消息（其 tool_calls 父消息被摘要掉）
        会被 OpenAI 兼容接口拒绝，故一并退回到被摘要段
    """
    if not needs_compression(messages, threshold_tokens):
        return messages, "", 0

    pinned = [m for m in messages if m.get("role") == "system"]
    history = [m for m in messages if m.get("role") != "system"]
    recent = history[-KEEP_RECENT_WINDOW:]
    mid = history[: len(history) - len(recent)]
    while recent and recent[0].get("role") == "tool":
        mid.append(recent.pop(0))
    if not mid:
        return messages, "", 0

    trajectory = "\n".join(
        f"[{m.get('role')}] {str(m.get('content') or '')[:400]}" for m in mid
    )
    resp = await llm.chat(
        [{"role": "user", "content": SUMMARY_PROMPT.format(trajectory=trajectory)}],
        model=model)
    summary = resp.text.strip() or "（摘要生成失败，仅保留近期消息）"
    summary_msg = {
        "role": "user",
        "content": f"<先前执行摘要，由上下文压缩自动生成>\n{summary}",
    }
    compressed = [*pinned, summary_msg, *recent]
    return compressed, summary, getattr(resp, "tokens_used", 0)


def inject_key_outputs(system_content: str, key_outputs: dict[str, str]) -> str:
    """把不可压缩的关键数据拼进 system 提示。"""
    if not key_outputs:
        return system_content
    lines = [f"- {k}: {v}" for k, v in key_outputs.items()]
    return (
        system_content
        + "\n\n[关键数据（已确认，不可压缩，直接引用）]\n" + "\n".join(lines)
    )
